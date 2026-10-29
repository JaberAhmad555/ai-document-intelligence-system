from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import closing
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.database import utc_now
from app.models.extraction import (
    DocumentExtraction,
    DocumentExtractionHead,
    ExtractedUnit,
)
from app.models.ingestion import Document, DocumentStatus, JobStatus, ProcessingJob
from app.services.document_reader import read_document
from app.services.reading_types import (
    DocumentSource,
    OCRFunction,
    ReadingSettings,
    ReadingUnit,
)
from app.workers.jobs import ProcessingError, ProcessingResult
from app.workers.runtime import WorkerContext


Reader = Callable[..., Iterator[ReadingUnit]]


def _invalid(message: str) -> ProcessingError:
    return ProcessingError(
        "invalid_extraction_output",
        message,
        retryable=False,
    )


def _has_content(unit: ReadingUnit) -> bool:
    if unit.kind == "sheet":
        return False

    if unit.kind == "row":
        return any(
            bool((cell.value or "").strip())
            or bool((cell.formula or "").strip())
            for cell in unit.cells
        )

    return bool(
        unit.text.strip()
        or (unit.native_text or "").strip()
        or (unit.ocr_text or "").strip()
    )


class DocumentProcessor:
    """Connect a streaming reader to the existing fenced worker context."""

    def __init__(
        self,
        *,
        reading_settings: ReadingSettings | None = None,
        reader: Reader | None = None,
        ocr: OCRFunction | None = None,
        batch_units: int = 32,
        batch_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not 1 <= batch_units <= 128:
            raise ValueError("batch_units must be between 1 and 128.")
        if not 1 <= batch_bytes <= 8 * 1024 * 1024:
            raise ValueError("batch_bytes must be between 1 byte and 8 MiB.")

        self.reading_settings = reading_settings
        self.reader = reader if reader is not None else read_document
        self.ocr = ocr
        self.batch_units = batch_units
        self.batch_bytes = batch_bytes

    @staticmethod
    def _source(context: WorkerContext) -> DocumentSource:
        claim = context.claim
        return DocumentSource(
            document_id=claim.document_id,
            original_filename=claim.original_filename,
            storage_key=claim.storage_key,
            sha256=claim.sha256,
            extension=claim.extension,
        )

    @staticmethod
    def _start(
        context: WorkerContext,
        source: DocumentSource,
    ) -> None:
        snapshot = source.model_dump(mode="json")

        def write(session: Session, document: Document) -> None:
            if (
                document.sha256 != source.sha256
                or document.storage_key != source.storage_key
            ):
                raise _invalid("The source changed after the job was claimed.")

            existing = session.get(
                DocumentExtraction,
                context.claim.lease_token,
            )

            if existing is None:
                session.add(
                    DocumentExtraction(
                        id=context.claim.lease_token,
                        document_id=context.claim.document_id,
                        job_id=context.claim.job_id,
                        attempt_number=context.claim.attempt_number,
                        source=snapshot,
                        source_sha256=source.sha256,
                        unit_count=0,
                    )
                )
            elif (
                existing.document_id != context.claim.document_id
                or existing.job_id != context.claim.job_id
                or existing.attempt_number != context.claim.attempt_number
                or existing.source != snapshot
            ):
                raise _invalid("The extraction attempt identity does not match.")

            document.processing_stage = "reading"

        context.write(write)

    @staticmethod
    def _write_batch(
        context: WorkerContext,
        batch: list[dict],
        total_parts: int,
        completed_parts: int,
    ) -> None:
        def write(session: Session, document: Document) -> None:
            extraction = session.get(
                DocumentExtraction,
                context.claim.lease_token,
            )
            if extraction is None:
                raise _invalid("The extraction attempt does not exist.")

            if extraction.part_count not in (None, total_parts):
                raise _invalid("The reader changed its total part count.")

            extraction.part_count = total_parts
            sequences = [item["sequence"] for item in batch]

            existing = {
                item.sequence: item
                for item in session.scalars(
                    select(ExtractedUnit).where(
                        ExtractedUnit.extraction_id == extraction.id,
                        ExtractedUnit.sequence.in_(sequences),
                    )
                ).all()
            }

            for item in batch:
                previous = existing.get(item["sequence"])

                if previous is not None:
                    if previous.payload_sha256 != item["payload_sha256"]:
                        raise ProcessingError(
                            "extraction_replay_conflict",
                            "A repeated extraction unit contains different data.",
                            retryable=False,
                        )
                    continue

                if extraction.ready_at is not None:
                    raise _invalid("A sealed extraction cannot receive new units.")

                if item["sequence"] != extraction.unit_count + 1:
                    raise _invalid("Extraction units are not contiguous.")

                session.add(
                    ExtractedUnit(
                        extraction_id=extraction.id,
                        **item,
                    )
                )
                extraction.unit_count += 1

            if document.total_units not in (None, total_parts):
                raise _invalid("Document progress disagrees with the reader.")

            # Progress and its corresponding stored rows commit together.
            document.total_units = total_parts
            document.processed_units = max(
                document.processed_units,
                completed_parts,
            )
            document.progress_percent = min(
                99,
                document.processed_units * 100 // total_parts,
            )

        context.write(write)

    @staticmethod
    def _seal(
        context: WorkerContext,
        expected_units: int,
        total_parts: int,
    ) -> None:
        def write(session: Session, document: Document) -> None:
            extraction = session.get(
                DocumentExtraction,
                context.claim.lease_token,
            )
            if extraction is None:
                raise _invalid("The extraction attempt does not exist.")

            count, first, last = session.execute(
                select(
                    func.count(),
                    func.min(ExtractedUnit.sequence),
                    func.max(ExtractedUnit.sequence),
                ).where(
                    ExtractedUnit.extraction_id == extraction.id
                )
            ).one()

            if (
                extraction.unit_count != expected_units
                or extraction.part_count != total_parts
                or (count, first, last) != (expected_units, 1, expected_units)
            ):
                raise _invalid("The persisted extraction is incomplete.")

            if extraction.ready_at is None:
                extraction.ready_at = utc_now()

            head = session.get(
                DocumentExtractionHead,
                context.claim.document_id,
            )
            if head is None:
                session.add(
                    DocumentExtractionHead(
                        document_id=context.claim.document_id,
                        extraction_id=extraction.id,
                    )
                )
            else:
                head.extraction_id = extraction.id

            document.total_units = total_parts
            document.processed_units = total_parts
            document.progress_percent = 99
            document.processing_stage = "finalizing"

        context.write(write)

    def __call__(self, context: WorkerContext) -> ProcessingResult:
        context.ensure_active()
        source = self._source(context)
        path = context.source_path
        options = (
            self.reading_settings
            if self.reading_settings is not None
            else ReadingSettings()
        )

        self._start(context, source)

        allowed_kinds = (
            {"page"}
            if source.extension == ".pdf"
            else {"image_frame"}
            if source.extension in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
            else {"row"}
            if source.extension == ".csv"
            else {"sheet", "row"}
        )

        count = 0
        total_parts = None
        last_part = 0
        completed_parts = 0
        has_content = False
        batch: list[dict] = []
        buffered_bytes = 0

        iterator = self.reader(
            path,
            source,
            settings=options,
            ocr=self.ocr,
        )

        with closing(iterator):
            for unit in iterator:
                context.ensure_active()

                if not isinstance(unit, ReadingUnit):
                    raise _invalid("The reader returned an invalid unit.")

                if unit.source != source or unit.sequence != count + 1:
                    raise _invalid("The reader returned inconsistent provenance.")

                if unit.kind not in allowed_kinds:
                    raise _invalid("The unit kind does not match the document format.")

                part = unit.position.part_number
                parts = unit.position.part_count

                if total_parts is None:
                    total_parts = parts

                if (
                    parts != total_parts
                    or part > parts
                    or part < last_part
                    or part > last_part + 1
                ):
                    raise _invalid("The reader returned inconsistent part ordering.")

                values = unit.model_dump(
                    mode="json",
                    exclude={"source"},
                )
                encoded = json.dumps(
                    values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")

                if len(encoded) > self.batch_bytes:
                    raise ProcessingError(
                        "extraction_unit_storage_limit",
                        "An extracted unit exceeds the configured storage batch limit.",
                        retryable=False,
                    )

                if batch and (
                    len(batch) >= self.batch_units
                    or buffered_bytes + len(encoded) > self.batch_bytes
                ):
                    self._write_batch(
                        context,
                        batch,
                        total_parts,
                        completed_parts,
                    )
                    batch = []
                    buffered_bytes = 0

                values["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
                batch.append(values)
                buffered_bytes += len(encoded)

                count += 1
                last_part = part
                has_content = has_content or _has_content(unit)

                # Pages and frames are complete units. A sheet/CSV part is
                # complete only when the next part starts or reading ends.
                completed_parts = max(
                    completed_parts,
                    part if unit.kind in {"page", "image_frame"} else part - 1,
                )

                if len(batch) >= self.batch_units:
                    self._write_batch(
                        context,
                        batch,
                        total_parts,
                        completed_parts,
                    )
                    batch = []
                    buffered_bytes = 0

        context.ensure_active()

        if count == 0 or total_parts is None or last_part != total_parts:
            raise _invalid("The reader did not finish every source part.")

        if not has_content:
            raise ProcessingError(
                "no_extracted_content",
                "No readable text or cell content was found in the document.",
                retryable=False,
            )

        if batch:
            self._write_batch(
                context,
                batch,
                total_parts,
                completed_parts,
            )

        self._seal(context, count, total_parts)

        # WorkerRuntime will call the existing JobStore.complete().
        # Only that transaction establishes job/document success.
        return ProcessingResult(
            processed_units=total_parts,
            total_units=total_parts,
        )


def process_document(context: WorkerContext) -> ProcessingResult:
    """CLI processor entry point using environment-driven reader settings."""
    return DocumentProcessor()(context)


def read_published_page(
    session: Session,
    document_id: UUID,
    *,
    after_sequence: int = 0,
    limit: int = 20,
    expected_extraction_id: UUID | None = None,
) -> tuple[UUID | None, list[ReadingUnit]]:
    """Read a bounded page of successfully committed extraction output.

    Use the returned extraction ID as expected_extraction_id on later pages
    to prevent combining pages from different processing attempts.
    """
    if after_sequence < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid extracted-content pagination.")

    statement = (
        select(ExtractedUnit, DocumentExtraction.source)
        .join(
            DocumentExtraction,
            DocumentExtraction.id == ExtractedUnit.extraction_id,
        )
        .join(
            DocumentExtractionHead,
            and_(
                DocumentExtractionHead.extraction_id == DocumentExtraction.id,
                DocumentExtractionHead.document_id
                == DocumentExtraction.document_id,
            ),
        )
        .join(
            ProcessingJob,
            ProcessingJob.id == DocumentExtraction.job_id,
        )
        .join(
            Document,
            Document.id == DocumentExtraction.document_id,
        )
        .where(
            Document.id == document_id,
            Document.status == DocumentStatus.COMPLETED,
            ProcessingJob.document_id == Document.id,
            ProcessingJob.status == JobStatus.SUCCEEDED,
            ProcessingJob.attempt_count == DocumentExtraction.attempt_number,
            DocumentExtraction.ready_at.is_not(None),
            DocumentExtraction.source_sha256 == Document.sha256,
            ExtractedUnit.sequence > after_sequence,
        )
        .order_by(ExtractedUnit.sequence)
        .limit(limit)
    )

    if expected_extraction_id is not None:
        statement = statement.where(
            DocumentExtraction.id == expected_extraction_id
        )

    rows = session.execute(statement).all()
    if not rows:
        return None, []

    units = [
        ReadingUnit(
            sequence=stored.sequence,
            source=source,
            position=stored.position,
            kind=stored.kind,
            method=stored.method,
            text=stored.text,
            native_text=stored.native_text,
            ocr_text=stored.ocr_text,
            cells=stored.cells,
            warnings=stored.warnings,
        )
        for stored, source in rows
    ]

    return rows[0][0].extraction_id, units
