from __future__ import annotations

import math
import warnings
from collections.abc import Iterator
from pathlib import Path

import pymupdf
from PIL import Image

from app.services.reading_types import (
    DocumentSource,
    OCRFunction,
    ReadingSettings,
    ReadingUnit,
    SourcePosition,
)
from app.workers.jobs import ProcessingError


IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def _render_dpi(page: pymupdf.Page, settings: ReadingSettings) -> int:
    width = page.rect.width
    height = page.rect.height

    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
    ):
        raise ProcessingError(
            "invalid_pdf_page",
            "The PDF contains invalid page dimensions.",
            retryable=False,
        )

    maximum_dpi = math.floor(
        72 * math.sqrt(settings.max_pdf_render_pixels / (width * height))
    )
    dpi = min(settings.pdf_dpi, maximum_dpi)

    while dpi >= 72:
        pixels = math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72)
        if pixels <= settings.max_pdf_render_pixels:
            return dpi
        dpi -= 1

    raise ProcessingError(
        "pdf_render_limit",
        "A PDF page cannot be rendered within the configured pixel limit.",
        retryable=False,
    )


def _contains_large_image(
    page: pymupdf.Page,
    settings: ReadingSettings,
) -> bool:
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return False

    for information in page.get_image_info():
        rectangle = pymupdf.Rect(information["bbox"]) & page.rect
        image_area = max(0, rectangle.width) * max(0, rectangle.height)

        if image_area / page_area >= settings.image_coverage_ocr_threshold:
            return True

    return False


def read_pdf(
    path: Path,
    source: DocumentSource,
    settings: ReadingSettings,
    ocr: OCRFunction,
) -> Iterator[ReadingUnit]:
    with pymupdf.open(str(path)) as document:
        if not document.is_pdf or document.is_repaired:
            raise ProcessingError(
                "invalid_pdf",
                "The PDF is invalid or requires structural repair.",
                retryable=False,
            )

        if document.needs_pass or document.is_encrypted:
            raise ProcessingError(
                "encrypted_pdf",
                "Password-protected PDFs are not supported.",
                retryable=False,
            )

        page_count = document.page_count
        if not 1 <= page_count <= settings.max_pages:
            raise ProcessingError(
                "pdf_page_limit",
                "The PDF page count is outside the configured limits.",
                retryable=False,
            )

        for index in range(page_count):
            page = document.load_page(index)
            native_text = page.get_text("text", sort=True).strip()

            if len(native_text) > settings.max_unit_characters:
                raise ProcessingError(
                    "reading_unit_limit",
                    "A PDF page contains too much extracted text.",
                    retryable=False,
                )

            native_characters = sum(
                character.isalnum()
                for character in native_text
            )
            requires_ocr = (
                native_characters < settings.min_native_text_characters
                or _contains_large_image(page, settings)
            )

            recognized_text: str | None = None
            flags: list[str] = []

            if requires_ocr:
                dpi = _render_dpi(page, settings)
                if dpi < settings.pdf_dpi:
                    flags.append("render_dpi_reduced")

                pixmap = page.get_pixmap(
                    dpi=dpi,
                    colorspace=pymupdf.csGRAY,
                    alpha=False,
                )
                image = Image.frombytes(
                    "L",
                    (pixmap.width, pixmap.height),
                    pixmap.samples,
                )
                del pixmap

                try:
                    recognized_text = ocr(image).strip()
                finally:
                    image.close()

                flags.append("ocr_output_requires_review")

            if recognized_text is None:
                method = "pdf_text"
                text = native_text
            elif native_text:
                method = "pdf_text_and_ocr"
                text = "\n\n".join(
                    value for value in (native_text, recognized_text) if value
                )
                flags.append("native_and_ocr_may_overlap")
            else:
                method = "ocr"
                text = recognized_text

            if not text:
                flags.append("no_text_detected")

            unit = ReadingUnit(
                source=source,
                position=SourcePosition(
                    part_number=index + 1,
                    part_count=page_count,
                    page_number=index + 1,
                ),
                kind="page",
                method=method,
                text=text,
                native_text=native_text,
                ocr_text=recognized_text,
                warnings=tuple(flags),
            )
            del page
            yield unit


def read_image(
    path: Path,
    source: DocumentSource,
    settings: ReadingSettings,
    ocr: OCRFunction,
) -> Iterator[ReadingUnit]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)

        with Image.open(path) as image:
            if image.format != IMAGE_FORMATS[source.extension]:
                raise ProcessingError(
                    "image_type_mismatch",
                    "The image contents do not match its extension.",
                    retryable=False,
                )

            frame_count = getattr(image, "n_frames", 1)
            if not 1 <= frame_count <= settings.max_pages:
                raise ProcessingError(
                    "image_frame_limit",
                    "The image frame count exceeds the configured limit.",
                    retryable=False,
                )

            for index in range(frame_count):
                image.seek(index)
                width, height = image.size

                if (
                    width < 1
                    or height < 1
                    or width * height > settings.max_image_pixels
                ):
                    raise ProcessingError(
                        "image_pixel_limit",
                        "An image frame exceeds the configured pixel limit.",
                        retryable=False,
                    )

                image.load()

                # Retain only the current frame while OCR runs.
                with image.copy() as frame:
                    text = ocr(frame).strip()

                flags = ["ocr_output_requires_review"]
                if not text:
                    flags.append("no_text_detected")

                yield ReadingUnit(
                    source=source,
                    position=SourcePosition(
                        part_number=index + 1,
                        part_count=frame_count,
                        frame_number=index + 1,
                    ),
                    kind="image_frame",
                    method="ocr",
                    text=text,
                    ocr_text=text,
                    warnings=tuple(flags),
                )