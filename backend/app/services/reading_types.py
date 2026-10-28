from collections.abc import Callable
from typing import Literal
from uuid import UUID

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_DIR


class ReadingSettings(BaseSettings):
    """Reading limits, configurable through READING_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="READING_",
        case_sensitive=False,
        extra="ignore",
    )

    tesseract_cmd: str = Field(default="tesseract", min_length=1)
    ocr_language: str = Field(
        default="eng",
        pattern=r"^[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*$",
    )
    ocr_psm: Literal[3, 4, 6, 11, 12] = 3
    ocr_timeout_seconds: float = Field(default=60, ge=0.1, le=600)
    max_ocr_output_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1,
        le=16 * 1024 * 1024,
    )
    preprocess_images: bool = True

    pdf_dpi: int = Field(default=200, ge=72, le=400)
    min_native_text_characters: int = Field(default=20, ge=1, le=1000)
    image_coverage_ocr_threshold: float = Field(default=0.25, gt=0, le=1)
    max_pdf_render_pixels: int = Field(default=12_000_000, ge=1)
    max_image_pixels: int = Field(default=20_000_000, ge=1)

    max_pages: int = Field(default=10_000, ge=1)
    max_source_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_rows_per_sheet: int = Field(default=200_000, ge=1)
    max_columns: int = Field(default=1024, ge=1, le=16_384)
    max_cell_characters: int = Field(default=65_536, ge=1)
    max_units: int = Field(default=500_000, ge=1)
    max_unit_characters: int = Field(default=1_000_000, ge=1)
    max_total_characters: int = Field(default=20_000_000, ge=1)
    max_duration_seconds: float = Field(default=7200, ge=1, le=86400)


class DocumentSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: UUID
    original_filename: str = Field(min_length=1, max_length=255)
    storage_key: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extension: Literal[
        ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"
    ]


class SourcePosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A part is a PDF page, image frame, spreadsheet sheet, or CSV file.
    part_number: int = Field(ge=1)
    part_count: int = Field(ge=1)

    page_number: int | None = Field(default=None, ge=1)
    frame_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = None
    row_number: int | None = Field(default=None, ge=1)

    # CSV records can span several physical lines.
    physical_line_start: int | None = Field(default=None, ge=1)
    physical_line_end: int | None = Field(default=None, ge=1)


class CellValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    column_number: int = Field(ge=1)
    address: str
    data_type: Literal[
        "text", "number", "date", "boolean", "formula", "error", "blank"
    ]
    value: str | None
    formula: str | None = None


class ReadingUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(default=1, ge=1)
    source: DocumentSource
    position: SourcePosition
    kind: Literal["page", "image_frame", "sheet", "row"]
    method: Literal[
        "pdf_text", "ocr", "pdf_text_and_ocr", "spreadsheet", "csv"
    ]
    text: str
    native_text: str | None = None
    ocr_text: str | None = None
    cells: tuple[CellValue, ...] = ()
    warnings: tuple[str, ...] = ()


# Tests can inject a controlled OCR function. Production uses TesseractOCR.
OCRFunction = Callable[[Image.Image], str]