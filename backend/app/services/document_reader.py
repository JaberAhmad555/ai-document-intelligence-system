from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from time import monotonic

from app.core.errors import ApplicationError
from app.services.ocr import TesseractOCR
from app.services.pdf_image_reader import read_image, read_pdf
from app.services.reading_types import (
    DocumentSource,
    OCRFunction,
    ReadingSettings,
    ReadingUnit,
)
from app.services.tabular_reader import read_csv, read_xls, read_xlsx
from app.workers.jobs import ProcessingError


def _check_deadline(started: float, settings: ReadingSettings) -> None:
    if monotonic() - started > settings.max_duration_seconds:
        raise ProcessingError(
            "reading_timeout",
            "Document reading exceeded the configured time limit.",
            retryable=False,
        )


def _verify_source(
    path: Path,
    source: DocumentSource,
    settings: ReadingSettings,
    started: float,
) -> None:
    size = path.stat().st_size
    if size < 1 or size > settings.max_source_bytes:
        raise ProcessingError(
            "source_size_limit",
            "The source file size is outside the configured limits.",
            retryable=False,
        )

    digest = hashlib.sha256()
    bytes_read = 0

    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _check_deadline(started, settings)
            bytes_read += len(chunk)

            if bytes_read > settings.max_source_bytes:
                raise ProcessingError(
                    "source_size_limit",
                    "The source file exceeds the configured limit.",
                    retryable=False,
                )

            digest.update(chunk)

    if digest.hexdigest() != source.sha256:
        raise ProcessingError(
            "source_integrity_failed",
            "The stored document does not match its recorded checksum.",
            retryable=False,
        )


def read_document(
    path: Path,
    source: DocumentSource,
    *,
    settings: ReadingSettings | None = None,
    ocr: OCRFunction | None = None,
) -> Iterator[ReadingUnit]:
    """Yield verified source content without changing database or job state.

    Consume the iterator fully to establish that reading finished.
    Earlier units may have been yielded before a later unit fails.

    A caller that stops early should close the iterator, preferably using
    contextlib.closing. Never treat a partial iteration as successful work.
    """
    options = settings if settings is not None else ReadingSettings()
    recognize = ocr if ocr is not None else TesseractOCR(options)
    started = monotonic()
    path = Path(path)

    try:
        _verify_source(path, source, options, started)

        if source.extension == ".pdf":
            iterator = read_pdf(path, source, options, recognize)
        elif source.extension in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            iterator = read_image(path, source, options, recognize)
        elif source.extension == ".xlsx":
            iterator = read_xlsx(path, source, options)
        elif source.extension == ".xls":
            iterator = read_xls(path, source, options)
        else:
            iterator = read_csv(path, source, options)

        total_characters = 0
        unit_count = 0

        with closing(iterator):
            for unit_count, unit in enumerate(iterator, start=1):
                _check_deadline(started, options)

                if unit_count > options.max_units:
                    raise ProcessingError(
                        "reading_unit_count_limit",
                        "The document exceeds the configured output-unit limit.",
                        retryable=False,
                    )

                # Include the retained native/OCR evidence in the budget.
                characters = (
                    len(unit.text)
                    + len(unit.native_text or "")
                    + len(unit.ocr_text or "")
                )

                if characters > options.max_unit_characters:
                    raise ProcessingError(
                        "reading_unit_limit",
                        "A document unit exceeds the configured text limit.",
                        retryable=False,
                    )

                total_characters += characters
                if total_characters > options.max_total_characters:
                    raise ProcessingError(
                        "reading_text_limit",
                        "The document exceeds the configured total text limit.",
                        retryable=False,
                    )

                yield unit.model_copy(update={"sequence": unit_count})

        _check_deadline(started, options)

        if unit_count == 0:
            raise ProcessingError(
                "no_readable_units",
                "The document contains no readable units.",
                retryable=False,
            )

    except ProcessingError:
        raise
    except ApplicationError as error:
        raise ProcessingError(
            error.code,
            error.message,
            retryable=error.status_code >= 500,
        ) from error
    except FileNotFoundError as error:
        raise ProcessingError(
            "source_file_missing",
            "The original document file is unavailable.",
            retryable=False,
        ) from error
    except MemoryError as error:
        raise ProcessingError(
            "reading_memory_limit",
            "Document reading exceeded available memory.",
            retryable=False,
        ) from error
    except Exception as error:
        raise ProcessingError(
            "document_read_failed",
            "The document is corrupted, unsupported, or could not be read.",
            retryable=False,
        ) from error