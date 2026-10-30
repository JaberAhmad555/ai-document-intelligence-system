"""Review commands and a bounded JSON-body guard, without worker duplication."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.http import error_response
from app.schemas.documents import ErrorResponse
from app.schemas.review import (
    CorrectionDetail, CorrectionPage, CorrectionReceipt, CorrectionRequest,
    ReprocessReceipt, ReprocessRequest,
)
from app.services.invoice_provider import InvoiceSettings
from app.services.review import correction_detail, correction_history, reprocess_document, save_correction

router = APIRouter(prefix="/api/documents", tags=["document review"], responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
    413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})
Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]


@router.post("/{document_id}/corrections", response_model=CorrectionReceipt)
def correct_invoice(document_id: UUID, body: CorrectionRequest, database: Database) -> CorrectionReceipt:
    """Append a correction revision. Repeating the same request returns its receipt.

    reviewer_label is self-reported; this endpoint does not authenticate it.
    Amounts are strings. Null explicitly clears a field; omitted fields stay.
    Financial disagreements are persisted as issues, not silently repaired.
    """
    return save_correction(database, document_id, body, InvoiceSettings().tolerance)


@router.get("/{document_id}/corrections", response_model=CorrectionPage)
def list_corrections(
    document_id: UUID, extraction_id: UUID, database: Database,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CorrectionPage:
    return correction_history(database, document_id, extraction_id, limit, offset)


@router.get("/{document_id}/corrections/{revision}", response_model=CorrectionDetail)
def get_correction(
    document_id: UUID, revision: int, extraction_id: UUID, database: Database,
) -> CorrectionDetail:
    return correction_detail(database, document_id, extraction_id, revision)


@router.post("/{document_id}/reprocess", response_model=ReprocessReceipt, status_code=202)
def retry_failed_document(
    document_id: UUID, body: ReprocessRequest, database: Database, settings: Configuration,
) -> ReprocessReceipt:
    """Queue a new job for the specified terminal failure. Never run processing here."""
    return reprocess_document(database, document_id, body, settings)


class ReviewBodyLimitMiddleware:
    """Count actual JSON bytes before FastAPI parses either mutation body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        parts = scope.get("path", "").rstrip("/").split("/")
        guarded = (scope["type"] == "http" and scope.get("method") == "POST"
                   and len(parts) == 5 and parts[1:3] == ["api", "documents"]
                   and parts[-1] in {"corrections", "reprocess"})
        if not guarded:
            await self.app(scope, receive, send)
            return
        limit = 4 * 1024 * 1024 if parts[-1] == "corrections" else 4096
        headers = scope.get("headers", [])

        async def reject(status: int, code: str, message: str) -> None:
            await error_response(status_code=status, code=code, message=message)(scope, receive, send)

        lengths = [v.strip() for k, v in headers if k.lower() == b"content-length"]
        declared = None
        if lengths:
            if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isdigit():
                await reject(400, "invalid_content_length", "Content-Length must be one nonnegative integer.")
                return
            declared = int(lengths[0])
            if declared > limit:
                await reject(413, "request_too_large", "The review request exceeds its size limit.")
                return
        encodings = [v.strip().lower() for k, v in headers if k.lower() == b"content-encoding"]
        if encodings and encodings != [b"identity"]:
            await reject(415, "unsupported_content_encoding", "Compressed review requests are not supported.")
            return
        content_types = [v.split(b";", 1)[0].strip().lower() for k, v in headers if k.lower() == b"content-type"]
        if content_types != [b"application/json"]:
            await reject(415, "json_required", "Send review commands as application/json.")
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await reject(413, "request_too_large", "The review request exceeds its size limit.")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if declared is not None and declared != len(body):
            await reject(400, "content_length_mismatch", "The body does not match Content-Length.")
            return
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
