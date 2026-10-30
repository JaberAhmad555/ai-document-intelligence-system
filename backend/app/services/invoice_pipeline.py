"""Compose document reading and invoice intelligence inside the durable worker.

No queue, background thread, HTTP task, or completion transaction is added.
WorkerRuntime remains responsible for heartbeat, retries and final completion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from contextlib import closing
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, case, select
from sqlalchemy.orm import Session, aliased

from app.core.database import utc_now
from app.models.extraction import (
    DocumentExtraction,
    DocumentExtractionHead,
    ExtractedUnit,
)
from app.models.ingestion import Document, DocumentStatus, JobStatus, ProcessingJob
from app.schemas.invoice import Invoice, InvoiceResult, ValidationIssue
from app.services.document_processor import DocumentProcessor
from app.services.invoice_extraction import extract_invoice
from app.services.invoice_provider import (
    GeminiInvoiceProvider,
    InvoiceProvider,
    InvoiceSettings,
)
from app.services.invoice_validation import duplicate_key, find_duplicate_issues
from app.services.reading_types import ReadingUnit
from app.workers.jobs import ProcessingError, ProcessingResult
from app.workers.runtime import WorkerContext


MAX_INVOICE_BYTES = 8 * 1024 * 1024
DUPLICATE_LIMIT = 100
DUPLICATE_CODES = {
    "possible_duplicate",
    "duplicate_currency_mismatch",
    "duplicate_search_truncated",
}


def _failure(code: str, message: str) -> ProcessingError:
    return ProcessingError(code, message, retryable=False)


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def invoice_identity(invoice: Invoice) -> str | None:
    key = duplicate_key(invoice)
    if key is None:
        return None
    encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def published_extractions():
    """Return a composable SELECT with the established publication gates.

    Alias the document and job so callers can correlate this statement with
    an outer Document query, for example during keyword search.
    """
    document = aliased(Document)
    job = aliased(ProcessingJob)
    return (
        select(DocumentExtraction)
        .join(
            DocumentExtractionHead,
            and_(
                DocumentExtractionHead.extraction_id == DocumentExtraction.id,
                DocumentExtractionHead.document_id
                == DocumentExtraction.document_id,
            ),
        )
        .join(document, document.id == DocumentExtraction.document_id)
        .join(job, job.id == DocumentExtraction.job_id)
        .where(
            document.status == DocumentStatus.COMPLETED,
            job.document_id == document.id,
            job.status == JobStatus.SUCCEEDED,
            job.attempt_count == DocumentExtraction.attempt_number,
            DocumentExtraction.ready_at.is_not(None),
            DocumentExtraction.source_sha256 == document.sha256,
        )
    )


def decode_invoice(extraction: DocumentExtraction) -> InvoiceResult:
    """Validate a stored snapshot and its attempt/source binding."""
    payload = extraction.invoice_result
    if (
        payload is None
        or extraction.invoice_digest is None
        or extraction.invoice_created_at is None
    ):
        raise _failure("invoice_snapshot_missing", "No invoice snapshot exists.")

    actual_digest = hashlib.sha256(_canonical(payload)).hexdigest()
    if not hmac.compare_digest(actual_digest, extraction.invoice_digest):
        raise _failure(
            "invoice_snapshot_invalid",
            "The stored invoice snapshot failed its integrity check.",
        )

    try:
        result = InvoiceResult.model_validate(payload)
    except ValidationError:
        raise _failure(
            "invoice_snapshot_invalid",
            "The stored invoice snapshot does not match its schema.",
        ) from None

    if (
        result.source.model_dump(mode="json") != extraction.source
        or result.source.document_id != extraction.document_id
        or result.source.sha256 != extraction.source_sha256
        or result.input_units != extraction.unit_count
    ):
        raise _failure(
            "invoice_snapshot_invalid",
            "The invoice snapshot does not match its source extraction.",
        )
    return result


def effective_invoice_payload():
    """Select the review projection only when a correction revision exists."""
    return case(
        (DocumentExtraction.correction_revision > 0, DocumentExtraction.corrected_result),
        else_=DocumentExtraction.invoice_result,
    )


def effective_invoice_identity():
    # A human clearing an identity field must not fall back to the original.
    return case(
        (DocumentExtraction.correction_revision > 0, DocumentExtraction.corrected_identity),
        else_=DocumentExtraction.invoice_identity,
    )


def refresh_duplicate_issues(
    session: Session,
    result: InvoiceResult,
) -> tuple[InvoiceResult, bool]:
    """Compute current duplicate candidates without mutating stored JSON.

    Read only the three small invoice fields needed for comparison. Do not
    load every candidate's line items and evidence into memory.
    """
    current = result.model_copy(deep=True)
    current.issues = [
        issue for issue in current.issues if issue.code not in DUPLICATE_CODES
    ]
    identity = invoice_identity(current.invoice)
    truncated = False

    if identity is not None:
        payload = effective_invoice_payload()
        statement = (
            published_extractions()
            .with_only_columns(
                DocumentExtraction.document_id,
                payload["invoice"]["invoice_number"].as_string(),
                payload["invoice"]["supplier_name"].as_string(),
                payload["invoice"]["currency"].as_string(),
                maintain_column_froms=True,
            )
            .where(
                effective_invoice_identity() == identity,
                DocumentExtraction.document_id != current.source.document_id,
                DocumentExtraction.invoice_result.is_not(None),
                DocumentExtraction.invoice_digest.is_not(None),
                DocumentExtraction.invoice_created_at.is_not(None),
            )
            .order_by(DocumentExtraction.document_id)
            .limit(DUPLICATE_LIMIT + 1)
        )
        rows = session.execute(statement).all()
        truncated = len(rows) > DUPLICATE_LIMIT
        candidates = [
            (
                document_id,
                Invoice(
                    invoice_number=number,
                    supplier_name=supplier,
                    currency=currency,
                ),
            )
            for document_id, number, supplier, currency
            in rows[:DUPLICATE_LIMIT]
        ]
        current.issues.extend(
            find_duplicate_issues(
                current.invoice,
                candidates,
                current_document_id=current.source.document_id,
            )
        )
        if truncated:
            current.issues.append(
                ValidationIssue(
                    code="duplicate_search_truncated",
                    severity="warning",
                    field="invoice_number",
                    message="More than 100 duplicate candidates exist; "
                    "only the first 100 are included.",
                )
            )

    current.review_required = bool(current.issues)
    current.confidence = (
        "review_required" if current.review_required else "source_supported"
    )
    return current, truncated


class _FencedProvider:
    """Check database ownership on both sides of a network request."""

    def __init__(self, context: WorkerContext, provider: InvoiceProvider):
        self.context = context
        self.provider = provider
        self.name = provider.name
        self.model = provider.model

    def extract(self, contents: str) -> Invoice:
        self.context.write(lambda session, document: None)
        result = self.provider.extract(contents)
        self.context.write(lambda session, document: None)
        return result


class InvoiceProcessor:
    """Read, persist, extract, validate and stage one complete invoice attempt."""

    def __init__(
        self,
        *,
        document_processor: DocumentProcessor | None = None,
        invoice_settings: InvoiceSettings | None = None,
        provider: InvoiceProvider | None = None,
        read_batch_units: int = 8,
    ) -> None:
        if not 1 <= read_batch_units <= 32:
            raise ValueError("read_batch_units must be between 1 and 32.")
        self.document_processor = document_processor or DocumentProcessor()
        self.invoice_settings = invoice_settings
        self.provider = provider
        self.read_batch_units = read_batch_units

    @staticmethod
    def _attempt(session: Session, context: WorkerContext) -> DocumentExtraction:
        extraction = session.get(DocumentExtraction, context.claim.lease_token)
        head = session.get(DocumentExtractionHead, context.claim.document_id)
        if (
            extraction is None
            or extraction.document_id != context.claim.document_id
            or extraction.job_id != context.claim.job_id
            or extraction.attempt_number != context.claim.attempt_number
            or extraction.source_sha256 != context.claim.sha256
            or extraction.ready_at is None
            or head is None
            or head.extraction_id != extraction.id
        ):
            raise _failure(
                "invoice_attempt_invalid",
                "Invoice processing requires the sealed current reading attempt.",
            )
        return extraction

    def _units(self, context: WorkerContext) -> Iterator[ReadingUnit]:
        cursor = 0
        while True:
            def fetch(session: Session, document: Document):
                extraction = self._attempt(session, context)
                rows = session.scalars(
                    select(ExtractedUnit)
                    .where(
                        ExtractedUnit.extraction_id == extraction.id,
                        ExtractedUnit.sequence > cursor,
                    )
                    .order_by(ExtractedUnit.sequence)
                    .limit(self.read_batch_units)
                ).all()
                units = [
                    ReadingUnit(
                        sequence=row.sequence,
                        source=extraction.source,
                        position=row.position,
                        kind=row.kind,
                        method=row.method,
                        text=row.text,
                        native_text=row.native_text,
                        ocr_text=row.ocr_text,
                        cells=row.cells,
                        warnings=row.warnings,
                    )
                    for row in rows
                ]
                return extraction.unit_count, units

            total, units = context.write(fetch)
            if not units:
                if cursor != total:
                    raise _failure(
                        "invoice_content_incomplete",
                        "Persisted reading units are incomplete.",
                    )
                return

            for unit in units:
                context.ensure_active()
                if unit.sequence != cursor + 1 or unit.sequence > total:
                    raise _failure(
                        "invoice_content_incomplete",
                        "Persisted reading units are not contiguous.",
                    )
                cursor = unit.sequence
                yield unit

    def __call__(self, context: WorkerContext) -> ProcessingResult:
        # This is the existing reader/storage processor, not another queue.
        reading_result = self.document_processor(context)

        def start(session: Session, document: Document):
            extraction = self._attempt(session, context)
            if extraction.invoice_result is not None:
                cached = decode_invoice(extraction)
                if cached.provider == "test" and context.settings.app_env != "test":
                    raise _failure(
                        "test_invoice_not_allowed",
                        "Test-provider invoices are restricted to the test environment.",
                    )
                return True
            document.processing_stage = "extracting_invoice"
            return False

        if context.write(start):
            # Replaying a completed staging operation under the same lease
            # must not spend another LLM request or overwrite its result.
            return reading_result

        settings = self.invoice_settings or InvoiceSettings()
        provider = self.provider or GeminiInvoiceProvider(settings)
        if provider.name == "test" and context.settings.app_env != "test":
            raise _failure(
                "test_invoice_not_allowed",
                "Test-provider invoices are restricted to the test environment.",
            )

        # Each DB read finishes before a unit is yielded. No SQL transaction
        # remains open across Gemini calls; the normal heartbeat can continue.
        with closing(self._units(context)) as units:
            result = extract_invoice(
                units,
                settings=settings,
                provider=_FencedProvider(context, provider),
                checkpoint=context.ensure_active,
            )

        def save(session: Session, document: Document) -> None:
            extraction = self._attempt(session, context)
            if (
                result.source.model_dump(mode="json") != extraction.source
                or result.input_units != extraction.unit_count
                or result.provider != provider.name
                or result.model != provider.model
            ):
                raise _failure(
                    "invoice_result_mismatch",
                    "The invoice result does not match this processing attempt.",
                )

            current, _ = refresh_duplicate_issues(session, result)
            payload = current.model_dump(mode="json")
            encoded = _canonical(payload)
            if len(encoded) > MAX_INVOICE_BYTES:
                raise _failure(
                    "invoice_storage_limit",
                    "The structured invoice exceeds the 8 MiB storage limit.",
                )
            digest = hashlib.sha256(encoded).hexdigest()

            if extraction.invoice_result is not None:
                if extraction.invoice_digest != digest:
                    raise _failure(
                        "invoice_replay_conflict",
                        "The same attempt produced conflicting invoice snapshots.",
                    )
                return

            extraction.invoice_result = payload
            extraction.invoice_identity = invoice_identity(current.invoice)
            extraction.invoice_digest = digest
            extraction.invoice_created_at = utc_now()
            document.processing_stage = "finalizing"
            # Progress stays below 100 until JobStore.complete commits.
            document.progress_percent = 99

        context.write(save)
        return reading_result


def process_document(context: WorkerContext) -> ProcessingResult:
    """Production CLI entry point: real readers followed by real Gemini."""
    return InvoiceProcessor()(context)
