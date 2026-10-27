from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.errors import ApplicationError
from app.models.ingestion import Document
from app.schemas.documents import (
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    ErrorResponse,
)
from app.services.uploads import upload_documents


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


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List uploaded documents",
)
def list_documents(
    database: DatabaseDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    total = database.scalar(
        select(func.count()).select_from(Document)
    ) or 0

    documents = database.scalars(
        select(Document)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    return DocumentListResponse(
        items=[
            DocumentResponse.model_validate(document)
            for document in documents
        ],
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