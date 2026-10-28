from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base, build_engine
from app.models.ingestion import (
    Document,
    DocumentStatus,
    JobStatus,
    ProcessingJob,
)
from app.workers.jobs import (
    JobStore,
    LeaseLostError,
    ProcessingError,
    ProcessingResult,
)


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 29, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def environment(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            f"sqlite+pysqlite:///{(tmp_path / 'worker-jobs.db').as_posix()}"
        ),
        local_storage_path=tmp_path / "uploads",
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    clock = TestClock()
    store = JobStore(
        factory,
        lease_seconds=30,
        retry_base_seconds=5,
        retry_max_seconds=20,
        clock=clock,
    )

    try:
        yield factory, clock, store
    finally:
        engine.dispose()


def enqueue(factory, clock, *, max_attempts=3):
    with factory() as session:
        document = Document(
            original_filename="synthetic.csv",
            storage_key=f"{uuid4().hex}.csv",
            extension=".csv",
            media_type="text/csv",
            size_bytes=20,
            sha256="a" * 64,
        )
        job = ProcessingJob(
            document=document,
            max_attempts=max_attempts,
            available_at=clock(),
        )
        session.add_all([document, job])
        session.commit()
        return document.id, job.id


def test_claim_sets_running_state_and_cannot_be_claimed_twice(environment):
    factory, clock, store = environment
    document_id, job_id = enqueue(factory, clock)

    claim = store.claim_next("worker-one")
    assert claim is not None
    assert claim.job_id == job_id
    assert claim.attempt_number == 1
    assert store.claim_next("worker-two") is None

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        document = session.get(Document, document_id)

        assert job.status == JobStatus.RUNNING
        assert job.lease_token == claim.lease_token
        assert job.locked_by == "worker-one"
        assert job.lease_expires_at == clock() + timedelta(seconds=30)
        assert document.status == DocumentStatus.PROCESSING
        assert document.progress_percent == 0


def test_two_claims_select_different_jobs(environment):
    factory, clock, store = environment
    enqueue(factory, clock)
    enqueue(factory, clock)

    first = store.claim_next("worker-one")
    second = store.claim_next("worker-two")

    assert first is not None
    assert second is not None
    assert first.job_id != second.job_id


def test_heartbeat_extends_live_lease_but_not_expired_lease(environment):
    factory, clock, store = environment
    _, job_id = enqueue(factory, clock)
    claim = store.claim_next("worker-one")
    assert claim is not None

    clock.advance(10)
    assert store.heartbeat(claim) is True

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        assert job.heartbeat_at == clock()
        assert job.lease_expires_at == clock() + timedelta(seconds=30)

    clock.advance(31)
    assert store.heartbeat(claim) is False


def test_progress_reserves_100_percent_for_committed_success(environment):
    factory, clock, store = environment
    document_id, job_id = enqueue(factory, clock)
    claim = store.claim_next("worker-one")
    assert claim is not None

    store.report_progress(
        claim,
        processed_units=2,
        total_units=2,
        stage="extracting",
    )

    with factory() as session:
        document = session.get(Document, document_id)
        assert document.status == DocumentStatus.PROCESSING
        assert document.progress_percent == 99

    store.complete(claim, ProcessingResult(2, 2))

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        assert document.status == DocumentStatus.COMPLETED
        assert document.progress_percent == 100
        assert document.processing_finished_at == clock()
        assert job.status == JobStatus.SUCCEEDED
        assert job.lease_token is None
        assert job.locked_by is None
        assert job.lease_expires_at is None


def test_retry_backoff_and_attempt_exhaustion(environment):
    factory, clock, store = environment
    document_id, job_id = enqueue(factory, clock, max_attempts=2)
    error = ProcessingError(
        "temporary_failure",
        "A temporary processing failure occurred.",
        retryable=True,
    )

    first = store.claim_next("worker-one")
    assert first is not None
    assert store.fail(first, error) == JobStatus.QUEUED
    assert store.claim_next("worker-one") is None

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        assert job.available_at == clock() + timedelta(seconds=5)

    clock.advance(5)
    second = store.claim_next("worker-one")
    assert second is not None
    assert second.attempt_number == 2
    assert second.lease_token != first.lease_token

    assert store.fail(second, error) == JobStatus.FAILED

    with factory() as session:
        document = session.get(Document, document_id)
        job = session.get(ProcessingJob, job_id)

        assert job.attempt_count == 2
        assert job.finished_at == clock()
        assert document.status == DocumentStatus.FAILED
        assert document.error_code == "temporary_failure"

    clock.advance(100)
    assert store.claim_next("worker-one") is None


def test_retry_delay_increases_and_is_capped(environment):
    factory, clock, store = environment
    _, job_id = enqueue(factory, clock, max_attempts=5)
    error = ProcessingError(
        "temporary_failure",
        "A temporary processing failure occurred.",
        retryable=True,
    )

    for delay in (5, 10, 20, 20):
        claim = store.claim_next("worker-one")
        assert claim is not None
        assert store.fail(claim, error) == JobStatus.QUEUED

        with factory() as session:
            job = session.get(ProcessingJob, job_id)
            assert job.available_at == clock() + timedelta(seconds=delay)

        clock.advance(delay)


def test_nonretryable_failure_is_terminal(environment):
    factory, clock, store = environment
    document_id, _ = enqueue(factory, clock)
    claim = store.claim_next("worker-one")
    assert claim is not None

    result = store.fail(
        claim,
        ProcessingError(
            "unsupported_document",
            "The document cannot be processed.",
            retryable=False,
        ),
    )

    assert result == JobStatus.FAILED

    with factory() as session:
        document = session.get(Document, document_id)
        assert document.status == DocumentStatus.FAILED


def test_expired_job_is_recovered_and_old_attempt_is_fenced(environment):
    factory, clock, store = environment
    _, job_id = enqueue(factory, clock)

    old_claim = store.claim_next("old-worker")
    assert old_claim is not None

    clock.advance(31)
    assert store.recover_expired() == 1
    assert store.recover_expired() == 0

    clock.advance(5)
    new_claim = store.claim_next("new-worker")
    assert new_claim is not None
    assert new_claim.lease_token != old_claim.lease_token

    with pytest.raises(LeaseLostError):
        store.complete(old_claim, ProcessingResult(1, 1))

    with pytest.raises(LeaseLostError):
        store.report_progress(
            old_claim,
            processed_units=1,
            total_units=1,
            stage="extracting",
        )

    with pytest.raises(LeaseLostError):
        store.fail(
            old_claim,
            ProcessingError(
                "stale_failure",
                "This attempt is no longer current.",
                retryable=False,
            ),
        )

    called = []

    def stale_write(session, document):
        called.append(True)

    with pytest.raises(LeaseLostError):
        store.fenced_write(old_claim, stale_write)

    assert called == []
    assert store.heartbeat(old_claim) is False

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        assert job.status == JobStatus.RUNNING
        assert job.lease_token == new_claim.lease_token


def test_abandoned_final_attempt_becomes_failed(environment):
    factory, clock, store = environment
    document_id, job_id = enqueue(factory, clock, max_attempts=1)
    assert store.claim_next("worker-one") is not None

    clock.advance(31)
    assert store.recover_expired() == 1

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        document = session.get(Document, document_id)

        assert job.status == JobStatus.FAILED
        assert job.last_error_code == "worker_lease_expired"
        assert document.status == DocumentStatus.FAILED
        assert document.progress_percent == 0


def test_fenced_write_rolls_back_if_lease_expires_during_callback(environment):
    factory, clock, store = environment
    document_id, _ = enqueue(factory, clock)
    claim = store.claim_next("worker-one")
    assert claim is not None

    def slow_write(session, document):
        document.processing_stage = "should_not_persist"
        clock.advance(31)

    with pytest.raises(LeaseLostError):
        store.fenced_write(claim, slow_write)

    with factory() as session:
        document = session.get(Document, document_id)
        assert document.processing_stage == "processing"


def test_invalid_completion_result_cannot_be_constructed():
    with pytest.raises(ValueError):
        ProcessingResult(0, 0)

    with pytest.raises(ValueError):
        ProcessingResult(1, 2)