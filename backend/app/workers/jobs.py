from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import utc_now
from app.models.ingestion import (
    Document,
    DocumentStatus,
    JobKind,
    JobStatus,
    ProcessingJob,
)


T = TypeVar("T")

CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,99}")
STAGE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,49}")


class LeaseLostError(RuntimeError):
    """The attempt no longer has permission to modify its job or document."""


class ProcessingError(Exception):
    """A processor failure with an explicitly safe public description.

    Do not include raw parser exceptions, credentials, document contents,
    or filesystem paths in the public message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        if CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("Processing error codes must be safe identifiers.")
        if not message.strip() or len(message) > 500:
            raise ValueError(
                "Processing error messages must contain 1 to 500 characters."
            )

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: UUID
    document_id: UUID
    lease_token: UUID
    worker_id: str
    attempt_number: int
    original_filename: str
    storage_key: str
    storage_backend: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Explicit completion result returned by an actual processor.

    A processor must finish and persist its output before returning this
    object. It must not return success merely because an input was opened.
    """

    processed_units: int
    total_units: int

    def __post_init__(self) -> None:
        for value in (self.processed_units, self.total_units):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Processing unit counts must be integers.")

        if self.total_units < 1:
            raise ValueError("Successful processing must contain at least one unit.")

        if self.processed_units != self.total_units:
            raise ValueError("All units must be processed before completion.")


class JobStore:
    """Short, transactional operations for durable worker lifecycle state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_seconds < 3:
            raise ValueError("Lease duration must be at least three seconds.")
        if retry_base_seconds < 1:
            raise ValueError("Retry base delay must be positive.")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError("Maximum retry delay must not be below the base delay.")

        self.session_factory = session_factory
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("The worker clock must return timezone-aware values.")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self.session_factory() as session:
            try:
                if session.get_bind().dialect.name == "sqlite":
                    # Acquire SQLite's writer reservation before reading a
                    # candidate. Transactions contain no OCR or file parsing.
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")

                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def check_database(self) -> None:
        """Check that migrated tables are accessible without creating them."""
        with self.session_factory() as session:
            session.execute(select(Document.id).limit(1)).all()
            session.execute(select(ProcessingJob).limit(1)).all()

    def claim_next(self, worker_id: str) -> JobClaim | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("Worker IDs must contain 1 to 100 characters.")

        with self._transaction() as session:
            statement = (
                select(ProcessingJob)
                .where(
                    ProcessingJob.kind == JobKind.PROCESS_DOCUMENT,
                    ProcessingJob.status == JobStatus.QUEUED,
                    ProcessingJob.available_at <= self._now(),
                    ProcessingJob.attempt_count < ProcessingJob.max_attempts,
                )
                .order_by(
                    ProcessingJob.available_at,
                    ProcessingJob.created_at,
                    ProcessingJob.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = session.scalar(statement)

            if job is None:
                return None

            document = session.get(Document, job.document_id)
            if document is None:
                raise RuntimeError("A processing job has no corresponding document.")

            now = self._now()
            token = uuid4()

            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.locked_by = worker_id
            job.lease_token = token
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.started_at = now
            job.finished_at = None

            document.status = DocumentStatus.PROCESSING
            document.processing_stage = "processing"
            document.progress_percent = 0
            document.processed_units = 0
            document.total_units = None
            document.processing_started_at = now
            document.processing_finished_at = None
            document.error_code = None
            document.error_message = None

            return JobClaim(
                job_id=job.id,
                document_id=document.id,
                lease_token=token,
                worker_id=worker_id,
                attempt_number=job.attempt_count,
                original_filename=document.original_filename,
                storage_key=document.storage_key,
                storage_backend=document.storage_backend,
                extension=document.extension,
                media_type=document.media_type,
                size_bytes=document.size_bytes,
                sha256=document.sha256,
            )

    def _owned_job(
        self,
        session: Session,
        claim: JobClaim,
    ) -> tuple[ProcessingJob, datetime]:
        job = session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.id == claim.job_id,
                ProcessingJob.document_id == claim.document_id,
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.lease_token == claim.lease_token,
                ProcessingJob.locked_by == claim.worker_id,
            )
            .with_for_update()
        )

        # Read the clock after acquiring the row lock.
        now = self._now()

        if (
            job is None
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            raise LeaseLostError("The processing lease is no longer valid.")

        return job, now

    def heartbeat(self, claim: JobClaim) -> bool:
        try:
            with self._transaction() as session:
                job, now = self._owned_job(session, claim)
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            return True
        except LeaseLostError:
            return False

    def report_progress(
        self,
        claim: JobClaim,
        *,
        processed_units: int,
        total_units: int,
        stage: str,
    ) -> None:
        if (
            isinstance(processed_units, bool)
            or isinstance(total_units, bool)
            or not isinstance(processed_units, int)
            or not isinstance(total_units, int)
            or total_units < 1
            or processed_units < 0
            or processed_units > total_units
        ):
            raise ValueError("Invalid processing progress.")

        if STAGE_PATTERN.fullmatch(stage) is None:
            raise ValueError("Processing stages must be safe identifiers.")

        with self._transaction() as session:
            self._owned_job(session, claim)
            document = session.get(Document, claim.document_id)

            if document is None:
                raise LeaseLostError("The document no longer exists.")

            if processed_units < document.processed_units:
                raise ValueError("Progress cannot move backwards within an attempt.")

            if (
                document.total_units is not None
                and document.total_units != total_units
            ):
                raise ValueError("The unit total cannot change within an attempt.")

            document.total_units = total_units
            document.processed_units = processed_units
            document.processing_stage = stage

            # 100% is reserved for a successfully committed completion.
            document.progress_percent = min(
                99,
                processed_units * 100 // total_units,
            )

    def fenced_write(
        self,
        claim: JobClaim,
        operation: Callable[[Session, Document], T],
    ) -> T:
        """Persist processor data while holding a valid job lease.

        The callback must perform only short database operations. It must
        not commit, roll back, perform OCR, or make network requests.

        Future page/sheet result persistence will use this boundary.
        """
        with self._transaction() as session:
            job, _ = self._owned_job(session, claim)
            document = session.get(Document, claim.document_id)

            if document is None:
                raise LeaseLostError("The document no longer exists.")

            result = operation(session, document)

            if (
                job.lease_expires_at is None
                or job.lease_expires_at <= self._now()
            ):
                raise LeaseLostError("The lease expired during the database write.")

            return result

    @staticmethod
    def _clear_lease(job: ProcessingJob) -> None:
        job.locked_by = None
        job.lease_token = None
        job.lease_expires_at = None

    def complete(
        self,
        claim: JobClaim,
        result: ProcessingResult,
    ) -> None:
        if not isinstance(result, ProcessingResult):
            raise TypeError("Completion requires an explicit ProcessingResult.")

        with self._transaction() as session:
            job, now = self._owned_job(session, claim)
            document = session.get(Document, claim.document_id)

            if document is None:
                raise LeaseLostError("The document no longer exists.")

            if (
                document.total_units is not None
                and document.total_units != result.total_units
            ):
                raise ValueError("Completion totals disagree with reported progress.")

            job.status = JobStatus.SUCCEEDED
            job.finished_at = now
            self._clear_lease(job)

            document.status = DocumentStatus.COMPLETED
            document.processing_stage = "completed"
            document.processed_units = result.processed_units
            document.total_units = result.total_units
            document.progress_percent = 100
            document.processing_finished_at = now
            document.error_code = None
            document.error_message = None

    def _record_failure(
        self,
        job: ProcessingJob,
        document: Document,
        error: ProcessingError,
        now: datetime,
    ) -> JobStatus:
        job.last_error_code = error.code
        job.last_error_message = error.message
        self._clear_lease(job)

        document.error_code = error.code
        document.error_message = error.message

        if error.retryable and job.attempt_count < job.max_attempts:
            exponent = min(max(job.attempt_count - 1, 0), 20)
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** exponent),
            )

            job.status = JobStatus.QUEUED
            job.available_at = now + timedelta(seconds=delay)
            job.finished_at = None

            document.status = DocumentStatus.QUEUED
            document.processing_stage = "retry_wait"
            document.progress_percent = 0
            document.processed_units = 0
            document.total_units = None
            document.processing_finished_at = None
        else:
            job.status = JobStatus.FAILED
            job.finished_at = now

            document.status = DocumentStatus.FAILED
            document.processing_stage = "failed"
            document.processing_finished_at = now

        return job.status

    def fail(
        self,
        claim: JobClaim,
        error: ProcessingError,
    ) -> JobStatus:
        with self._transaction() as session:
            job, now = self._owned_job(session, claim)
            document = session.get(Document, claim.document_id)

            if document is None:
                raise LeaseLostError("The document no longer exists.")

            return self._record_failure(job, document, error, now)

    def recover_expired(self, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("Recovery limit must be between 1 and 1000.")

        with self._transaction() as session:
            jobs = session.scalars(
                select(ProcessingJob)
                .where(
                    ProcessingJob.status == JobStatus.RUNNING,
                    ProcessingJob.lease_expires_at <= self._now(),
                )
                .order_by(
                    ProcessingJob.lease_expires_at,
                    ProcessingJob.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()

            error = ProcessingError(
                "worker_lease_expired",
                "The previous processing attempt stopped renewing its lease.",
                retryable=True,
            )

            for job in jobs:
                document = session.get(Document, job.document_id)
                if document is None:
                    raise RuntimeError(
                        "A processing job has no corresponding document."
                    )

                self._record_failure(job, document, error, self._now())

            return len(jobs)