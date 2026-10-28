from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from typing import TypeVar

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.ingestion import Document, JobStatus
from app.services.storage import LocalStorage
from app.workers.jobs import (
    JobClaim,
    JobStore,
    LeaseLostError,
    ProcessingError,
    ProcessingResult,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")


class RunOutcome(str, Enum):
    WAITING_FOR_PROCESSOR = "waiting_for_processor"
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


@dataclass
class WorkerContext:
    claim: JobClaim
    settings: Settings
    store: JobStore
    lease_lost: Event

    def ensure_active(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLostError("This attempt has lost its processing lease.")

    @property
    def source_path(self) -> Path:
        self.ensure_active()

        if self.claim.storage_backend != "local":
            raise ProcessingError(
                "unsupported_storage_backend",
                "This worker currently supports local document storage only.",
                retryable=False,
            )

        try:
            path = LocalStorage(self.settings).path_for(self.claim.storage_key)
        except ApplicationError as error:
            raise ProcessingError(
                "invalid_source_reference",
                "The document storage reference is invalid.",
                retryable=False,
            ) from error

        if not path.is_file():
            raise ProcessingError(
                "source_file_missing",
                "The original document file is unavailable.",
                retryable=False,
            )

        return path

    def report_progress(
        self,
        processed_units: int,
        total_units: int,
        *,
        stage: str = "processing",
    ) -> None:
        self.ensure_active()
        self.store.report_progress(
            self.claim,
            processed_units=processed_units,
            total_units=total_units,
            stage=stage,
        )

    def write(
        self,
        operation: Callable[[Session, Document], T],
    ) -> T:
        self.ensure_active()
        return self.store.fenced_write(self.claim, operation)


Processor = Callable[[WorkerContext], ProcessingResult]


class WorkerRuntime:
    def __init__(
        self,
        store: JobStore,
        settings: Settings,
        *,
        worker_id: str,
        processor: Processor | None = None,
        heartbeat_interval: float | None = None,
    ) -> None:
        interval = (
            min(30.0, store.lease_seconds / 3)
            if heartbeat_interval is None
            else heartbeat_interval
        )

        if not 0 < interval < store.lease_seconds:
            raise ValueError("Heartbeat interval must be shorter than the lease.")
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("Worker IDs must contain 1 to 100 characters.")

        self.store = store
        self.settings = settings
        self.worker_id = worker_id
        self.processor = processor
        self.heartbeat_interval = interval

    def _heartbeat_loop(
        self,
        claim: JobClaim,
        stop: Event,
        lost: Event,
    ) -> None:
        while not stop.wait(self.heartbeat_interval):
            try:
                if not self.store.heartbeat(claim):
                    lost.set()
                    logger.warning("Lease lost for job %s.", claim.job_id)
                    return
            except Exception as error:
                # An unavailable database cannot prove continuing ownership.
                # Do not expose raw exception text in public job records.
                lost.set()
                logger.error(
                    "Heartbeat failed for job %s (%s).",
                    claim.job_id,
                    type(error).__name__,
                )
                return

    def run_once(self) -> RunOutcome:
        recovered = self.store.recover_expired()
        if recovered:
            logger.info("Recovered %s expired job lease(s).", recovered)

        if self.processor is None:
            return RunOutcome.WAITING_FOR_PROCESSOR

        claim = self.store.claim_next(self.worker_id)
        if claim is None:
            return RunOutcome.IDLE

        stop_heartbeat = Event()
        lease_lost = Event()

        context = WorkerContext(
            claim=claim,
            settings=self.settings,
            store=self.store,
            lease_lost=lease_lost,
        )

        heartbeat = Thread(
            target=self._heartbeat_loop,
            args=(claim, stop_heartbeat, lease_lost),
            name=f"job-heartbeat-{claim.job_id}",
            daemon=True,
        )
        heartbeat.start()

        result: ProcessingResult | None = None
        failure: ProcessingError | None = None

        try:
            result = self.processor(context)

            if not isinstance(result, ProcessingResult):
                raise ProcessingError(
                    "invalid_processor_result",
                    "The processor did not return a valid completion result.",
                    retryable=False,
                )

        except LeaseLostError:
            lease_lost.set()

        except ProcessingError as error:
            failure = error

        except Exception as error:
            logger.error(
                "Processor failed for job %s (%s).",
                claim.job_id,
                type(error).__name__,
            )
            failure = ProcessingError(
                "processor_exception",
                "Document processing failed unexpectedly.",
                retryable=True,
            )

        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2.0)

            if heartbeat.is_alive():
                # Be conservative if a renewal is stuck in database I/O.
                lease_lost.set()
                logger.error(
                    "Heartbeat shutdown did not finish for job %s.",
                    claim.job_id,
                )

        if lease_lost.is_set():
            # Recovery will decide the next state after lease expiration.
            return RunOutcome.LEASE_LOST

        try:
            if failure is not None:
                status = self.store.fail(claim, failure)
                return (
                    RunOutcome.RETRY_SCHEDULED
                    if status == JobStatus.QUEUED
                    else RunOutcome.FAILED
                )

            if result is None:
                # Defensive check; valid processors return ProcessingResult.
                raise RuntimeError("The processor produced no result.")

            self.store.complete(claim, result)
            return RunOutcome.SUCCEEDED

        except LeaseLostError:
            return RunOutcome.LEASE_LOST