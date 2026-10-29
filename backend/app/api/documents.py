from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.config import Settings, get_settings
from app.core.database import get_db, utc_now
from app.core.errors import ApplicationError
from app.models.extraction import DocumentExtraction, ExtractedUnit
from app.models.ingestion import Document, DocumentStatus
from app.schemas.documents import (
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    ErrorResponse,
)
from app.services.uploads import upload_documents
from app.schemas.invoice import Invoice, ValidationIssue, VerifiedEvidence
from app.services.invoice_pipeline import (
    decode_invoice,
    published_extractions,
    refresh_duplicate_issues,
)
from app.services.reading_types import CellValue, SourcePosition
from app.workers.jobs import ProcessingError


router = APIRouter(
    prefix="/api/documents",
    tags=["documents"],
    responses={
        400: {"model": ErrorResponse, "description": "Malformed request"},
        413: {"model": ErrorResponse, "description": "Upload limit exceeded"},
        415: {"model": ErrorResponse, "description": "Unsupported file or body type"},
        422: {"model": ErrorResponse, "description": "Validation failed"},
        500: {"model": ErrorResponse, "description": "Internal failure"},
        503: {"model": ErrorResponse, "description": "Required service unavailable"},
    },
)

DatabaseDependency = Annotated[Session, Depends(get_db)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload documents and queue processing jobs",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "string",
                                    "format": "binary",
                                },
                            }
                        },
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def upload_document_batch(
    request: Request,
    database: DatabaseDependency,
    settings: SettingsDependency,
) -> DocumentUploadResponse:
    """Accept repeated multipart fields named 'files'.

    A successful response means files were stored and jobs were queued.
    It does not mean document processing has run.
    """
    # Manual parsing allows the configured file-count limit to be applied
    # by the multipart parser itself. Ordinary text form fields are rejected.
    async with request.form(
        max_files=settings.max_upload_files,
        max_fields=0,
    ) as form:
        uploads: list[UploadFile] = []

        for field_name, value in form.multi_items():
            if field_name != "files" or not isinstance(
                value,
                StarletteUploadFile,
            ):
                raise ApplicationError(
                    status_code=422,
                    code="invalid_upload_field",
                    message="Use only file fields named 'files'.",
                )

            # Starlette supplies the compatible underlying UploadFile type.
            uploads.append(cast(UploadFile, value))

        # File parsers and database operations are synchronous. Offload the
        # existing service so they do not block the ASGI event loop.
        documents = await run_in_threadpool(
            upload_documents,
            uploads=uploads,
            database=database,
            settings=settings,
        )

        return DocumentUploadResponse(
            count=len(documents),
            documents=[
                DocumentResponse.model_validate(document)
                for document in documents
            ],
        )


def _search_condition(query: str):
    # Treat %, _ and backslash as literal search characters.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"

    content_match = (
        select(ExtractedUnit.sequence)
        .where(
            ExtractedUnit.extraction_id == DocumentExtraction.id,
            or_(
                ExtractedUnit.text.ilike(pattern, escape="\\"),
                ExtractedUnit.native_text.ilike(pattern, escape="\\"),
                ExtractedUnit.ocr_text.ilike(pattern, escape="\\"),
            ),
        )
        .correlate(DocumentExtraction)
        .exists()
    )
    payload = DocumentExtraction.invoice_result
    published_match = (
        published_extractions()
        .with_only_columns(DocumentExtraction.id, maintain_column_froms=True)
        .where(
            DocumentExtraction.document_id == Document.id,
            or_(
                content_match,
                payload["invoice"]["invoice_number"].as_string().ilike(
                    pattern, escape="\\"
                ),
                payload["invoice"]["supplier_name"].as_string().ilike(
                    pattern, escape="\\"
                ),
                payload["invoice"]["customer_name"].as_string().ilike(
                    pattern, escape="\\"
                ),
            ),
        )
        .correlate(Document)
        .exists()
    )
    return or_(
        Document.original_filename.ilike(pattern, escape="\\"),
        published_match,
    )


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List or search documents and published extracted content",
)
def list_documents(
    database: DatabaseDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    document_status: Annotated[
        DocumentStatus | None, Query(alias="status")
    ] = None,
    extension: Annotated[
        str | None,
        Query(pattern=r"^\.(pdf|jpg|jpeg|png|tif|tiff|xlsx|xls|csv)$"),
    ] = None,
) -> DocumentListResponse:
    filters = []
    if q is not None:
        query = q.strip()
        if not query:
            raise ApplicationError(
                status_code=422,
                code="empty_search_query",
                message="Search text cannot contain only whitespace.",
            )
        filters.append(_search_condition(query))
    if document_status is not None:
        filters.append(Document.status == document_status)
    if extension is not None:
        filters.append(Document.extension == extension)

    total = database.scalar(
        select(func.count()).select_from(Document).where(*filters)
    ) or 0
    documents = database.scalars(
        select(Document)
        .where(*filters)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(item) for item in documents],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(documents) < total,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
    },
    summary="Get document metadata and processing status",
)
def get_document(
    document_id: UUID,
    database: DatabaseDependency,
) -> DocumentResponse:
    document = database.get(Document, document_id)

    if document is None:
        raise ApplicationError(
            status_code=404,
            code="document_not_found",
            message="The requested document does not exist.",
        )

    return DocumentResponse.model_validate(document)

class PublicSource(BaseModel):
    """Source provenance deliberately excludes internal storage keys."""

    document_id: UUID
    original_filename: str
    sha256: str
    extension: str


class PublicInvoiceResult(BaseModel):
    schema_version: Literal["1"]
    source: PublicSource
    provider: Literal["gemini", "test"]
    model: str
    invoice: Invoice
    issues: list[ValidationIssue]
    missing_fields: list[str]
    review_required: bool
    confidence: Literal["source_supported", "review_required"]
    evidence: list[VerifiedEvidence]
    input_units: int
    chunks_processed: int


class InvoiceResponse(BaseModel):
    document_id: UUID
    extraction_id: UUID
    created_at: datetime
    result: PublicInvoiceResult
    duplicates_checked_at: datetime
    duplicates_truncated: bool


class ValidationResponse(BaseModel):
    document_id: UUID
    extraction_id: UUID
    issues: list[ValidationIssue]
    missing_fields: list[str]
    uncertain_fields: list[str]
    review_required: bool
    confidence: Literal["source_supported", "review_required"]
    duplicates_checked_at: datetime
    duplicates_truncated: bool


class ContentUnitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    kind: Literal["page", "image_frame", "sheet", "row"]
    method: Literal["pdf_text", "ocr", "pdf_text_and_ocr", "spreadsheet", "csv"]
    text: str
    native_text: str | None
    ocr_text: str | None
    position: SourcePosition
    cells: list[CellValue]
    warnings: list[str]


class ContentPageResponse(BaseModel):
    document_id: UUID
    extraction_id: UUID
    source: PublicSource
    items: list[ContentUnitResponse]
    total_units: int
    limit: int
    after_sequence: int
    next_after_sequence: int | None
    has_more: bool


def _require_published(
    database: Session,
    document_id: UUID,
    expected_extraction_id: UUID | None,
) -> DocumentExtraction:
    document = database.get(Document, document_id)
    if document is None:
        raise ApplicationError(
            status_code=404,
            code="document_not_found",
            message="The requested document does not exist.",
        )
    if document.status != DocumentStatus.COMPLETED:
        raise ApplicationError(
            status_code=409,
            code="document_not_completed",
            message="The document has no currently published processing result.",
            details={"status": document.status.value},
        )

    extraction = database.scalar(
        published_extractions().where(
            DocumentExtraction.document_id == document_id
        )
    )
    if extraction is None:
        raise ApplicationError(
            status_code=409,
            code="content_not_available",
            message="This document has no published content extraction.",
        )
    if expected_extraction_id is not None and extraction.id != expected_extraction_id:
        raise ApplicationError(
            status_code=409,
            code="extraction_changed",
            message="The processing attempt changed. Restart retrieval from page one.",
        )
    return extraction


def _invoice_response(
    database: Session,
    document_id: UUID,
    expected_extraction_id: UUID | None,
) -> InvoiceResponse:
    extraction = _require_published(database, document_id, expected_extraction_id)
    if extraction.invoice_result is None:
        raise ApplicationError(
            status_code=409,
            code="invoice_not_available",
            message="This document was completed without invoice extraction. "
            "Process it with the invoice worker to obtain an invoice result.",
        )
    try:
        stored = decode_invoice(extraction)
    except ProcessingError:
        raise ApplicationError(
            status_code=500,
            code="invoice_snapshot_invalid",
            message="The stored invoice result failed validation.",
        ) from None

    current, truncated = refresh_duplicate_issues(database, stored)
    values = current.model_dump(mode="json")
    values["source"] = PublicSource.model_validate(values["source"]).model_dump()
    return InvoiceResponse(
        document_id=document_id,
        extraction_id=extraction.id,
        created_at=extraction.invoice_created_at,
        result=PublicInvoiceResult.model_validate(values),
        duplicates_checked_at=utc_now(),
        duplicates_truncated=truncated,
    )


@router.get(
    "/{document_id}/invoice",
    response_model=InvoiceResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Retrieve a published invoice with evidence and current validation",
)
def get_invoice(
    document_id: UUID,
    database: DatabaseDependency,
    extraction_id: UUID | None = None,
) -> InvoiceResponse:
    return _invoice_response(database, document_id, extraction_id)


@router.get(
    "/{document_id}/validation",
    response_model=ValidationResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Retrieve financial, missing-field, uncertainty and duplicate issues",
)
def get_validation(
    document_id: UUID,
    database: DatabaseDependency,
    extraction_id: UUID | None = None,
) -> ValidationResponse:
    response = _invoice_response(database, document_id, extraction_id)
    return ValidationResponse(
        document_id=document_id,
        extraction_id=response.extraction_id,
        issues=response.result.issues,
        missing_fields=response.result.missing_fields,
        uncertain_fields=response.result.invoice.uncertain_fields,
        review_required=response.result.review_required,
        confidence=response.result.confidence,
        duplicates_checked_at=response.duplicates_checked_at,
        duplicates_truncated=response.duplicates_truncated,
    )


@router.get(
    "/{document_id}/content",
    response_model=ContentPageResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Retrieve a bounded page of published document text and provenance",
)
def get_content(
    document_id: UUID,
    database: DatabaseDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    extraction_id: UUID | None = None,
) -> ContentPageResponse:
    if after_sequence > 0 and extraction_id is None:
        raise ApplicationError(
            status_code=422,
            code="extraction_cursor_required",
            message="Supply extraction_id when requesting later content pages.",
        )
    extraction = _require_published(database, document_id, extraction_id)
    statement = (
        select(ExtractedUnit)
        .where(
            ExtractedUnit.extraction_id == extraction.id,
            ExtractedUnit.sequence > after_sequence,
        )
        .order_by(ExtractedUnit.sequence)
        .limit(limit)
        .execution_options(yield_per=1)
    )
    items = []
    payload_bytes = 0
    rows = database.scalars(statement)
    try:
        for row in rows:
            if (
                row.sequence != after_sequence + len(items) + 1
                or row.sequence > extraction.unit_count
            ):
                raise ApplicationError(
                    status_code=500,
                    code="published_content_incomplete",
                    message="Published content is not contiguous.",
                )
            item = ContentUnitResponse.model_validate(row)
            size = len(item.model_dump_json().encode("utf-8"))
            # Keep ordinary pages below 2 MiB. One larger source unit is
            # returned alone so it remains retrievable without truncation.
            if items and payload_bytes + size > 2 * 1024 * 1024:
                break
            items.append(item)
            payload_bytes += size
    finally:
        rows.close()

    if not items and after_sequence < extraction.unit_count:
        raise ApplicationError(
            status_code=500,
            code="published_content_incomplete",
            message="Published content is missing an expected unit.",
        )
    last_sequence = items[-1].sequence if items else after_sequence
    has_more = last_sequence < extraction.unit_count
    return ContentPageResponse(
        document_id=document_id,
        extraction_id=extraction.id,
        source=PublicSource.model_validate(extraction.source),
        items=items,
        total_units=extraction.unit_count,
        limit=limit,
        after_sequence=after_sequence,
        next_after_sequence=last_sequence if has_more else None,
        has_more=has_more,
    )
