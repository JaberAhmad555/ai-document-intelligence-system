from __future__ import annotations

import hashlib
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base, build_engine, utc_now
from app.models.ingestion import (
    Document,
    DocumentStatus,
    JobStatus,
    ProcessingJob,
)
from app.services.storage import LocalStorage
from app.workers.jobs import JobStore, ProcessingError, ProcessingResult
from app.workers.runtime import RunOutcome, WorkerRuntime


@pytest.fixture
def environment(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            f"sqlite+pysqlite:///{(tmp_path / 'worker-runtime.db').as_posix()}"
        ),
        local_storage_path=tmp_path / "uploads",
        job_lease_seconds=30,
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    store = JobStore(
        factory,
        lease_seconds=settings.job_lease_seconds,
    )

    try:
        yield settings, factory, store
    finally:
        engine.dispose()


def enqueue(settings, factory):
    payload = b"invoice,total\nSYN-WORKER-001,125\n"
    storage_key = f"{uuid4().hex}.csv"

    source = LocalStorage(settings).path_for(storage_key)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)

    with factory() as session:
        document = Document(
            original_filename="synthetic.csv",
            storage_key=storage_key,
            extension=".csv",
            media_type="text/csv",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        job = ProcessingJob(
            document=document,
            max_attempts=3,
            available_at=utc_now(),
        )
        session.add_all([document, job])
        session.commit()

        return document.id, job.id, source


def test_missing_processor_leaves_job_queued(environment):
    settings, factory, store = environment
    document_id, job_id, _ = enqueue(settings, factory)

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
    )

    assert runtime.run_once() == RunOutcome.WAITING_FOR_PROCESSOR

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        assert document.status == DocumentStatus.QUEUED
        assert document.progress_percent == 0
        assert job.status == JobStatus.QUEUED
        assert job.attempt_count == 0


def test_processor_reads_input_and_writes_output_before_success(
    environment,
    tmp_path,
):
    settings, factory, store = environment
    document_id, job_id, source = enqueue(settings, factory)
    outputs = []

    def synthetic_processor(context):
        # This is a small test processor, not the future CSV extraction pipeline.
        text_content = context.source_path.read_text(encoding="utf-8")
        output = tmp_path / f"{context.claim.lease_token}.txt"
        output.write_text(text_content.upper(), encoding="utf-8")
        outputs.append(output)

        context.report_progress(1, 1, stage="synthetic_processing")
        return ProcessingResult(1, 1)

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=synthetic_processor,
    )

    assert runtime.run_once() == RunOutcome.SUCCEEDED
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == (
        source.read_text(encoding="utf-8").upper()
    )

    with factory() as session:
        assert session.get(Document, document_id).status == (
            DocumentStatus.COMPLETED
        )
        assert session.get(ProcessingJob, job_id).status == JobStatus.SUCCEEDED

    assert runtime.run_once() == RunOutcome.IDLE


def test_none_result_does_not_mark_document_completed(environment):
    settings, factory, store = environment
    document_id, job_id, _ = enqueue(settings, factory)

    def invalid_processor(context):
        return None

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=invalid_processor,
    )

    assert runtime.run_once() == RunOutcome.FAILED

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        assert document.status == DocumentStatus.FAILED
        assert document.progress_percent == 0
        assert job.last_error_code == "invalid_processor_result"


def test_unexpected_processor_error_is_sanitized_and_retried(environment):
    settings, factory, store = environment
    document_id, job_id, _ = enqueue(settings, factory)

    def failing_processor(context):
        raise RuntimeError("synthetic-private-document-content")

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=failing_processor,
    )

    assert runtime.run_once() == RunOutcome.RETRY_SCHEDULED

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        assert document.status == DocumentStatus.QUEUED
        assert document.processing_stage == "retry_wait"
        assert job.attempt_count == 1
        assert job.last_error_code == "processor_exception"
        assert "synthetic-private" not in job.last_error_message


def test_explicit_nonretryable_error_is_terminal(environment):
    settings, factory, store = environment
    _, job_id, _ = enqueue(settings, factory)

    def failing_processor(context):
        raise ProcessingError(
            "unsupported_document",
            "This document cannot be processed.",
            retryable=False,
        )

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=failing_processor,
    )

    assert runtime.run_once() == RunOutcome.FAILED

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.attempt_count == 1


def test_missing_original_file_is_reported(environment):
    settings, factory, store = environment
    document_id, _, source = enqueue(settings, factory)
    source.unlink()

    def processor(context):
        context.source_path.read_bytes()
        return ProcessingResult(1, 1)

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=processor,
    )

    assert runtime.run_once() == RunOutcome.FAILED

    with factory() as session:
        document = session.get(Document, document_id)
        assert document.error_code == "source_file_missing"


def test_background_heartbeat_runs_during_processing(
    environment,
    monkeypatch,
):
    settings, factory, store = environment
    enqueue(settings, factory)

    renewed = Event()
    actual_heartbeat = store.heartbeat

    def observed_heartbeat(claim):
        result = actual_heartbeat(claim)
        if result:
            renewed.set()
        return result

    monkeypatch.setattr(store, "heartbeat", observed_heartbeat)

    def processor(context):
        context.source_path.read_bytes()
        if not renewed.wait(timeout=5):
            raise RuntimeError("Expected a heartbeat during the test.")
        return ProcessingResult(1, 1)

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=processor,
        heartbeat_interval=0.01,
    )

    assert runtime.run_once() == RunOutcome.SUCCEEDED
    assert renewed.is_set()


def test_heartbeat_loss_prevents_completion(
    environment,
    monkeypatch,
):
    settings, factory, store = environment
    document_id, job_id, _ = enqueue(settings, factory)
    renewal_attempted = Event()

    def rejected_heartbeat(claim):
        renewal_attempted.set()
        return False

    monkeypatch.setattr(store, "heartbeat", rejected_heartbeat)

    def processor(context):
        context.source_path.read_bytes()
        if not renewal_attempted.wait(timeout=5):
            raise RuntimeError("Expected a heartbeat attempt during the test.")
        return ProcessingResult(1, 1)

    runtime = WorkerRuntime(
        store,
        settings,
        worker_id="test-worker",
        processor=processor,
        heartbeat_interval=0.01,
    )

    assert runtime.run_once() == RunOutcome.LEASE_LOST

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        # The runtime deliberately leaves recovery to the lease mechanism.
        assert document.status == DocumentStatus.PROCESSING
        assert document.progress_percent == 0
        assert job.status == JobStatus.RUNNING