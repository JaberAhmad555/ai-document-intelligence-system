"""Transactional human review and retry commands over the existing job queue."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import utc_now
from app.core.errors import ApplicationError
from app.models.extraction import DocumentExtraction, InvoiceCorrection, ReprocessingRequest
from app.models.ingestion import Document, DocumentStatus, JobKind, JobStatus, ProcessingJob
from app.schemas.invoice import Invoice, InvoiceResult
from app.schemas.review import (
    CorrectionDetail, CorrectionPage, CorrectionReceipt, CorrectionRequest,
    CorrectionSummary, ReprocessReceipt, ReprocessRequest,
)
from app.services.invoice_pipeline import (
    DUPLICATE_CODES, MAX_INVOICE_BYTES, _canonical, decode_invoice,
    invoice_identity, published_extractions, refresh_duplicate_issues,
)
from app.services.invoice_validation import missing_invoice_fields, validate_invoice
from app.services.storage import LocalStorage
from app.workers.jobs import ProcessingError

FINANCIAL_CODES = {
    "missing_field", "uncertain_field", "grand_total_mismatch",
    "missing_line_item_field", "line_item_mismatch", "negative_value",
    "line_items_missing", "line_items_incomplete", "subtotal_mismatch",
}


def reject(code: str, message: str, status: int = 409) -> ApplicationError:
    return ApplicationError(status_code=status, code=code, message=message)


@contextmanager
def write_transaction(database: Session) -> Iterator[None]:
    """Start before any reads; SQLite has one writer, PostgreSQL locks a document.

    Never call this with a reused/read transaction or hold it across a provider
    call. All endpoints supply a fresh request session.
    """
    if database.in_transaction():
        raise RuntimeError("Review commands require a fresh database session.")
    try:
        if database.get_bind().dialect.name == "sqlite":
            database.execute(text("BEGIN IMMEDIATE"))
        yield
        database.commit()
    except IntegrityError:
        database.rollback()
        raise reject("concurrent_change", "Another request changed this document. Refresh and try again.") from None
    except Exception:
        database.rollback()
        raise


def locked_document(database: Session, document_id: UUID) -> Document:
    document = database.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        raise reject("document_not_found", "The requested document does not exist.", 404)
    return document


def current_review(
    database: Session, extraction: DocumentExtraction,
) -> tuple[InvoiceResult, InvoiceCorrection | None]:
    """Validate original plus the exact revision read in the extraction row.

    Binding to that revision avoids mixing snapshots under READ COMMITTED.
    """
    try:
        original = decode_invoice(extraction)
    except ProcessingError:
        raise reject("invoice_snapshot_invalid", "The original invoice failed validation.", 500) from None
    if extraction.correction_revision == 0:
        if extraction.corrected_result is not None or extraction.corrected_identity is not None:
            raise reject("correction_snapshot_invalid", "The stored review projection is inconsistent.", 500)
        return original, None
    audit = database.scalar(select(InvoiceCorrection).where(
        InvoiceCorrection.extraction_id == extraction.id,
        InvoiceCorrection.document_id == extraction.document_id,
        InvoiceCorrection.revision == extraction.correction_revision,
    ))
    try:
        if audit is None or extraction.corrected_result is None:
            raise ValueError("Missing revision")
        digest = hashlib.sha256(_canonical(audit.result)).hexdigest()
        projection_digest = hashlib.sha256(_canonical(extraction.corrected_result)).hexdigest()
        if (not hmac.compare_digest(digest, audit.result_digest)
                or not hmac.compare_digest(digest, projection_digest)
                or audit.original_digest != extraction.invoice_digest):
            raise ValueError("Invalid revision digest")
        result = InvoiceResult.model_validate(audit.result)
        if (result.source != original.source or result.provider != original.provider
                or result.model != original.model or result.input_units != original.input_units
                or result.chunks_processed != original.chunks_processed
                or invoice_identity(result.invoice) != extraction.corrected_identity):
            raise ValueError("Invalid revision binding")
    except (ValueError, TypeError, ValidationError):
        raise reject("correction_snapshot_invalid", "The stored review revision failed validation.", 500) from None
    return result, audit


def _affected(field: str, changed: set[str]) -> bool:
    return field in changed or ("line_items" in changed and field.startswith("line_items."))


def _corrected_result(current: InvoiceResult, request: CorrectionRequest, tolerance: Decimal):
    before = current.invoice.model_dump(mode="json")
    supplied = request.changes.model_dump(mode="json", exclude_unset=True)
    # Decimal('1.0') and Decimal('1.00') are the same value, not a new revision.
    candidate = Invoice.model_validate({**before, **supplied})
    changed = {name for name in supplied if getattr(candidate, name) != getattr(current.invoice, name)}
    if not changed:
        raise reject("no_changes", "The submitted values match the current invoice.", 422)
    candidate.uncertain_fields = [name for name in current.invoice.uncertain_fields if not _affected(name, changed)]
    # Evidence proves the original extraction. Do not claim it supports a new value.
    candidate.evidence = [item for item in current.invoice.evidence if not _affected(item.field, changed)]
    result = current.model_copy(deep=True)
    result.invoice = candidate
    result.evidence = [item for item in current.evidence if not _affected(item.field, changed)]
    result.issues = [
        issue for issue in current.issues
        if issue.code not in FINANCIAL_CODES | DUPLICATE_CODES and not _affected(issue.field, changed)
    ] + validate_invoice(candidate, tolerance)
    result.missing_fields = missing_invoice_fields(candidate)
    # A self-reported correction is not source verification or formal approval.
    result.review_required = True
    result.confidence = "review_required"
    after = candidate.model_dump(mode="json")
    return result, sorted(changed), {k: before[k] for k in sorted(changed)}, {k: after[k] for k in sorted(changed)}


def save_correction(
    database: Session, document_id: UUID, request: CorrectionRequest, tolerance: Decimal,
) -> CorrectionReceipt:
    digest = hashlib.sha256(_canonical(request.model_dump(mode="json", exclude_unset=True))).hexdigest()
    with write_transaction(database):
        document = locked_document(database, document_id)
        previous = database.scalar(select(InvoiceCorrection).where(
            InvoiceCorrection.document_id == document_id, InvoiceCorrection.request_id == request.request_id,
        ))
        if previous is not None:
            if not hmac.compare_digest(previous.request_digest, digest):
                raise reject("idempotency_conflict", "This request ID was already used for different changes.")
            return CorrectionReceipt.model_validate(previous)
        if document.status != DocumentStatus.COMPLETED:
            raise reject("document_not_completed", "Only a published, completed invoice can be corrected.")
        extraction = database.scalar(published_extractions().where(DocumentExtraction.document_id == document_id))
        if extraction is None or extraction.id != request.extraction_id:
            raise reject("extraction_changed", "The processing attempt changed. Reload before editing.")
        if extraction.invoice_result is None:
            raise reject("invoice_not_available", "This processing attempt has no invoice result.")
        if extraction.correction_revision != request.expected_revision:
            raise reject("correction_changed", "Another reviewer saved changes. Reload before editing.")
        current, previous_revision = current_review(database, extraction)
        result, changed, before, after = _corrected_result(current, request, tolerance)
        result, _ = refresh_duplicate_issues(database, result)
        result.review_required = True
        result.confidence = "review_required"
        payload = result.model_dump(mode="json")
        encoded = _canonical(payload)
        if len(encoded) > MAX_INVOICE_BYTES:
            raise reject("correction_too_large", "The corrected invoice exceeds the supported size.", 413)
        now = utc_now()
        revision = extraction.correction_revision + 1
        audit = InvoiceCorrection(
            id=uuid4(), document_id=document_id, extraction_id=extraction.id,
            request_id=request.request_id, request_digest=digest, revision=revision,
            original_digest=extraction.invoice_digest, reviewer_label=request.reviewer_label,
            reason=request.reason, changed_fields=changed,
            corrected_fields=sorted(set(previous_revision.corrected_fields if previous_revision else []) | set(changed)),
            before_values=before, after_values=after, result=payload,
            result_digest=hashlib.sha256(encoded).hexdigest(), tolerance=str(tolerance), created_at=now,
        )
        database.add(audit)
        extraction.correction_revision = revision
        extraction.corrected_result = payload
        extraction.corrected_identity = invoice_identity(result.invoice)
        document.updated_at = now
        database.flush()
        receipt = CorrectionReceipt.model_validate(audit)
    return receipt


def _history_extraction(database: Session, document_id: UUID, extraction_id: UUID) -> DocumentExtraction:
    if database.get(Document, document_id) is None:
        raise reject("document_not_found", "The requested document does not exist.", 404)
    extraction = database.get(DocumentExtraction, extraction_id)
    if extraction is None or extraction.document_id != document_id:
        raise reject("extraction_not_found", "This document does not have that extraction.", 404)
    return extraction


def correction_history(database: Session, document_id: UUID, extraction_id: UUID, limit: int, offset: int) -> CorrectionPage:
    extraction = _history_extraction(database, document_id, extraction_id)
    filters = (InvoiceCorrection.document_id == document_id, InvoiceCorrection.extraction_id == extraction_id,
               InvoiceCorrection.revision <= extraction.correction_revision)
    total = database.scalar(select(func.count()).select_from(InvoiceCorrection).where(*filters)) or 0
    # Do not load all historical line-item/evidence snapshots into memory.
    columns = [getattr(InvoiceCorrection, name) for name in CorrectionSummary.model_fields]
    rows = database.execute(select(*columns).where(*filters).order_by(InvoiceCorrection.revision.desc()).offset(offset).limit(limit)).mappings()
    items = [CorrectionSummary.model_validate(dict(row)) for row in rows]
    return CorrectionPage(document_id=document_id, extraction_id=extraction_id, items=items, total=total,
                          limit=limit, offset=offset, has_more=offset + len(items) < total)


def correction_detail(database: Session, document_id: UUID, extraction_id: UUID, revision: int) -> CorrectionDetail:
    _history_extraction(database, document_id, extraction_id)
    row = database.scalar(select(InvoiceCorrection).where(
        InvoiceCorrection.document_id == document_id, InvoiceCorrection.extraction_id == extraction_id,
        InvoiceCorrection.revision == revision,
    ))
    if row is None:
        raise reject("correction_not_found", "That correction revision does not exist.", 404)
    if hashlib.sha256(_canonical(row.result)).hexdigest() != row.result_digest:
        raise reject("correction_snapshot_invalid", "The stored correction failed validation.", 500)
    return CorrectionDetail(
        **CorrectionSummary.model_validate(row).model_dump(),
        corrected_fields=row.corrected_fields, before_values=row.before_values, after_values=row.after_values,
        issues_at_save=InvoiceResult.model_validate(row.result).issues,
    )


def failed_job_for(database: Session, document: Document) -> ProcessingJob | None:
    if document.status != DocumentStatus.FAILED:
        return None
    # An already retried failure must never be offered a second time.
    consumed = select(ReprocessingRequest.request_id).where(
        ReprocessingRequest.failed_job_id == ProcessingJob.id
    ).exists()
    return database.scalar(select(ProcessingJob).where(
        ProcessingJob.document_id == document.id, ProcessingJob.status == JobStatus.FAILED,
        ProcessingJob.finished_at == document.processing_finished_at, ~consumed,
    ).order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc()).limit(1))


def reprocess_document(database: Session, document_id: UUID, request: ReprocessRequest, settings: Settings) -> ReprocessReceipt:
    with write_transaction(database):
        document = locked_document(database, document_id)
        previous = database.get(ReprocessingRequest, request.request_id)
        if previous is not None:
            if previous.document_id != document_id or previous.failed_job_id != request.failed_job_id:
                raise reject("idempotency_conflict", "This request ID already refers to a different retry.")
            return ReprocessReceipt.model_validate(previous)
        failed = failed_job_for(database, document)
        if failed is None or failed.id != request.failed_job_id:
            raise reject("job_not_retryable", "This failed job is no longer eligible. Refresh the document.")
        # No job locks here: the worker locks job -> document. Locking an active
        # job after the document would invert that order. The partial unique
        # active-job index is an additional database-level safeguard.
        active = database.scalar(select(ProcessingJob.id).where(
            ProcessingJob.document_id == document_id, ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        ).limit(1))
        if active is not None:
            raise reject("job_already_active", "This document already has an active processing job.")
        if document.storage_backend == "local":
            try:
                exists = LocalStorage(settings).path_for(document.storage_key).is_file()
            except (OSError, ApplicationError):
                exists = False
            if not exists:
                raise reject("source_unavailable", "The stored original file is unavailable. Restore it or upload it again.")
        now = utc_now()
        job = ProcessingJob(
            id=uuid4(), document_id=document_id, kind=JobKind.PROCESS_DOCUMENT, status=JobStatus.QUEUED,
            attempt_count=0, max_attempts=settings.job_max_attempts, available_at=now, created_at=now, updated_at=now,
        )
        database.add(job)
        database.flush()
        receipt_row = ReprocessingRequest(request_id=request.request_id, document_id=document_id,
            failed_job_id=failed.id, new_job_id=job.id, created_at=now)
        database.add(receipt_row)
        document.status = DocumentStatus.QUEUED
        document.processing_stage = "queued"
        document.progress_percent = 0
        document.processed_units = 0
        document.total_units = None
        document.error_code = None
        document.error_message = None
        document.processing_started_at = None
        document.processing_finished_at = None
        document.updated_at = now
        database.flush()
        receipt = ReprocessReceipt.model_validate(receipt_row)
    return receipt
