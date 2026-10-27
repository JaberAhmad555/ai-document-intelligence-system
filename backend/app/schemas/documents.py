from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.ingestion import DocumentStatus


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    check: str


class DocumentResponse(BaseModel):
    """Public document metadata.

    Internal storage keys and filesystem paths are intentionally excluded.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str

    status: DocumentStatus
    processing_stage: str
    progress_percent: int = Field(ge=0, le=100)
    total_units: int | None
    processed_units: int = Field(ge=0)

    error_code: str | None
    error_message: str | None

    created_at: datetime
    updated_at: datetime
    processing_started_at: datetime | None
    processing_finished_at: datetime | None


class DocumentUploadResponse(BaseModel):
    count: int = Field(ge=1)
    documents: list[DocumentResponse]


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    has_more: bool