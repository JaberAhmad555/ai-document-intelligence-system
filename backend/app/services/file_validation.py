from __future__ import annotations

import codecs
import csv
import io
import math
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.parsers import expat

import openpyxl
import pymupdf
import xlrd
from PIL import Image

from app.core.config import Settings
from app.core.errors import ApplicationError


READ_CHUNK_SIZE = 64 * 1024
CSV_SAMPLE_SIZE = 8192
MAX_CSV_COLUMNS = 2048

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
}

IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    media_type: str


def reject(code: str, message: str) -> ApplicationError:
    return ApplicationError(
        status_code=422,
        code=code,
        message=message,
    )


def validate_file(
    path: Path,
    extension: str,
    settings: Settings,
) -> ValidatedFile:
    """Perform bounded structural validation without OCR or AI extraction.

    Browser-provided content types are intentionally ignored.
    Validation does not imply successful downstream document extraction.
    """
    if extension not in MEDIA_TYPES:
        raise ApplicationError(
            status_code=415,
            code="unsupported_file_type",
            message="The file extension is not supported.",
        )

    try:
        if extension == ".pdf":
            _validate_pdf(path)
        elif extension in IMAGE_FORMATS:
            _validate_image(path, extension, settings)
        elif extension == ".xlsx":
            _validate_xlsx(path, settings)
        elif extension == ".xls":
            _validate_xls(path)
        else:
            _validate_csv(path)

    except ApplicationError:
        raise
    except MemoryError as error:
        raise ApplicationError(
            status_code=503,
            code="validation_resource_limit",
            message="The file requires more memory than is currently available.",
        ) from error
    except Exception as error:
        # Parser messages may expose paths or untrusted document contents.
        raise reject(
            "invalid_file",
            "The file is corrupted, malformed, or does not match its extension.",
        ) from error

    return ValidatedFile(media_type=MEDIA_TYPES[extension])


def _validate_pdf(path: Path) -> None:
    with path.open("rb") as source:
        if not source.read(8).startswith(b"%PDF-"):
            raise reject(
                "file_type_mismatch",
                "The uploaded file does not contain a PDF header.",
            )

    with pymupdf.open(str(path)) as document:
        if not document.is_pdf:
            raise reject("file_type_mismatch", "The file is not a PDF.")

        if document.needs_pass or document.is_encrypted:
            raise reject(
                "encrypted_pdf",
                "Password-protected PDFs are not supported.",
            )

        # Do not silently accept a PDF that the parser had to repair.
        if document.is_repaired:
            raise reject(
                "damaged_pdf",
                "The PDF is damaged. Export a fresh copy and upload it again.",
            )

        if document.page_count < 1:
            raise reject("empty_pdf", "The PDF contains no pages.")

        # Inspect one page at a time without rasterizing or extracting text.
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            rectangle = page.rect

            if (
                not math.isfinite(rectangle.width)
                or not math.isfinite(rectangle.height)
                or rectangle.width <= 0
                or rectangle.height <= 0
            ):
                raise reject(
                    "invalid_pdf_page",
                    "The PDF contains an invalid page.",
                )

            del page


def _validate_image(
    path: Path,
    extension: str,
    settings: Settings,
) -> None:
    expected_format = IMAGE_FORMATS[extension]

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)

        # verify() performs format-specific structural checks.
        with Image.open(path) as image:
            if image.format != expected_format:
                raise reject(
                    "file_type_mismatch",
                    "The image contents do not match the filename extension.",
                )

            _check_image_dimensions(image.size, settings)
            image.verify()

        # Reopen after verify(), then decode one frame at a time.
        # The pixel limit applies to the entire image, including TIFF frames.
        total_pixels = 0

        with Image.open(path) as image:
            frame_number = 0

            while True:
                try:
                    image.seek(frame_number)
                except EOFError:
                    break

                width, height = image.size
                _check_image_dimensions((width, height), settings)
                total_pixels += width * height

                if total_pixels > settings.max_image_pixels:
                    raise reject(
                        "image_pixel_limit",
                        "The image exceeds the configured total pixel limit.",
                    )

                image.load()
                frame_number += 1

            if frame_number == 0:
                raise reject("empty_image", "The image contains no frames.")


def _check_image_dimensions(
    size: tuple[int, int],
    settings: Settings,
) -> None:
    width, height = size

    if width <= 0 or height <= 0:
        raise reject("invalid_image_dimensions", "The image size is invalid.")

    if width * height > settings.max_image_pixels:
        raise reject(
            "image_pixel_limit",
            "The image exceeds the configured pixel limit.",
        )


def _reject_xml_doctype(*args: object) -> None:
    raise reject(
        "unsafe_spreadsheet_xml",
        "Spreadsheet XML must not contain document type declarations.",
    )


def _validate_xlsx_archive(path: Path, settings: Settings) -> None:
    if not zipfile.is_zipfile(path):
        raise reject(
            "file_type_mismatch",
            "The uploaded file is not an XLSX archive.",
        )

    required_entries = {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    }

    with zipfile.ZipFile(path, "r") as archive:
        entries = archive.infolist()

        if len(entries) > settings.max_archive_entries:
            raise reject(
                "archive_entry_limit",
                "The spreadsheet contains too many archive entries.",
            )

        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise reject(
                "duplicate_archive_entries",
                "The spreadsheet contains duplicate archive entries.",
            )

        if not required_entries.issubset(set(names)):
            raise reject(
                "invalid_xlsx",
                "The archive does not contain the required XLSX structure.",
            )

        declared_total = 0
        actual_total = 0

        for entry in entries:
            name = entry.filename
            parts = PurePosixPath(name).parts

            if (
                "\\" in name
                or "\x00" in name
                or name.startswith("/")
                or ".." in parts
                or any(":" in part for part in parts)
            ):
                raise reject(
                    "unsafe_archive_path",
                    "The spreadsheet contains an invalid archive path.",
                )

            if entry.flag_bits & 0x1:
                raise reject(
                    "encrypted_spreadsheet",
                    "Encrypted spreadsheets are not supported.",
                )

            if name.lower().endswith("vbaproject.bin"):
                raise reject(
                    "macro_enabled_xlsx",
                    "Macro-enabled workbooks must not be uploaded as XLSX.",
                )

            declared_total += entry.file_size

            if declared_total > settings.max_archive_uncompressed_bytes:
                raise reject(
                    "archive_size_limit",
                    "The decompressed spreadsheet exceeds the configured limit.",
                )

            if (
                entry.file_size > 0
                and entry.file_size / max(entry.compress_size, 1)
                > settings.max_archive_compression_ratio
            ):
                raise reject(
                    "archive_compression_limit",
                    "The spreadsheet exceeds the allowed compression ratio.",
                )

            if entry.is_dir():
                continue

            parser = None
            if name.lower().endswith((".xml", ".rels")):
                # Expat handles XML encodings, including UTF-16.
                # Rejecting DTDs prevents entity declarations and expansion.
                parser = expat.ParserCreate()
                parser.StartDoctypeDeclHandler = _reject_xml_doctype
                parser.ExternalEntityRefHandler = lambda *args: 0

            entry_bytes = 0

            # Read every member to validate decompression and ZIP CRCs.
            # No archive member is extracted to the filesystem.
            with archive.open(entry, "r") as member:
                while True:
                    chunk = member.read(READ_CHUNK_SIZE)
                    if not chunk:
                        break

                    entry_bytes += len(chunk)
                    actual_total += len(chunk)

                    if actual_total > settings.max_archive_uncompressed_bytes:
                        raise reject(
                            "archive_size_limit",
                            "The decompressed spreadsheet exceeds the limit.",
                        )

                    if parser is not None:
                        parser.Parse(chunk, False)

                if parser is not None:
                    parser.Parse(b"", True)

            if entry_bytes != entry.file_size:
                raise reject(
                    "invalid_archive_size",
                    "The spreadsheet contains an inconsistent archive entry.",
                )


def _validate_xlsx(path: Path, settings: Settings) -> None:
    _validate_xlsx_archive(path, settings)

    workbook = openpyxl.load_workbook(
        filename=path,
        read_only=True,
        data_only=False,
        keep_links=False,
    )
    try:
        if not workbook.worksheets:
            raise reject(
                "empty_workbook",
                "The workbook must contain at least one worksheet.",
            )
    finally:
        workbook.close()


def _validate_xls(path: Path) -> None:
    with path.open("rb") as source:
        signature = source.read(8)

    # Require the compound-file container used by Excel 97-2003 XLS.
    if signature != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise reject(
            "file_type_mismatch",
            "The file is not an Excel 97-2003 XLS workbook.",
        )

    workbook = xlrd.open_workbook(
        filename=str(path),
        on_demand=True,
        logfile=io.StringIO(),
    )
    try:
        if workbook.nsheets < 1:
            raise reject(
                "empty_workbook",
                "The workbook must contain at least one worksheet.",
            )

        # Load and release one sheet at a time.
        for sheet_index in range(workbook.nsheets):
            workbook.sheet_by_index(sheet_index)
            workbook.unload_sheet(sheet_index)
    finally:
        workbook.release_resources()


def _validate_csv(path: Path) -> None:
    with path.open("rb") as source:
        prefix = source.read(4)

    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise reject(
            "unsupported_csv_encoding",
            "Use UTF-8 or BOM-marked UTF-16 for CSV files.",
        )

    encoding = (
        "utf-16"
        if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        else "utf-8-sig"
    )

    try:
        with path.open("r", encoding=encoding, errors="strict", newline="") as source:
            sample = source.read(CSV_SAMPLE_SIZE)
            if not sample.strip():
                raise reject("empty_csv", "The CSV contains no readable records.")

            _check_csv_text(sample)
            source.seek(0)

            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                # A single-column CSV is valid; comma is the fallback.
                dialect = csv.excel

            reader = csv.reader(source, dialect=dialect, strict=True)
            expected_columns: int | None = None
            record_count = 0

            for row in reader:
                if not row or all(not value.strip() for value in row):
                    continue

                if len(row) > MAX_CSV_COLUMNS:
                    raise reject(
                        "csv_column_limit",
                        "The CSV contains too many columns.",
                    )

                for value in row:
                    _check_csv_text(value)

                if expected_columns is None:
                    expected_columns = len(row)
                elif len(row) != expected_columns:
                    raise reject(
                        "inconsistent_csv_columns",
                        "CSV records must contain a consistent number of columns.",
                    )

                record_count += 1

            if record_count == 0:
                raise reject("empty_csv", "The CSV contains no readable records.")

    except UnicodeError as error:
        raise reject(
            "unsupported_csv_encoding",
            "The CSV must use UTF-8 or BOM-marked UTF-16 encoding.",
        ) from error
    except csv.Error as error:
        raise reject(
            "invalid_csv",
            "The CSV is malformed or contains an excessively large field.",
        ) from error


def _check_csv_text(value: str) -> None:
    if any(
        (ord(character) < 32 and character not in "\t\r\n")
        or ord(character) == 127
        for character in value
    ):
        raise reject(
            "binary_csv",
            "The CSV contains binary or unsupported control characters.",
        )
