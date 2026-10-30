"""Real API/database/worker tests using the existing controlled test provider.

No live Gemini calls are made. Tests create disposable SQLite databases.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from threading import Barrier
from uuid import UUID, uuid4

from alembic import command
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, inspect, select, text

from app.api.review import ReviewBodyLimitMiddleware
from app.models.extraction import DocumentExtraction, ExtractedUnit, InvoiceCorrection, ReprocessingRequest
from app.models.ingestion import Document, DocumentStatus, JobStatus, ProcessingJob
from app.services.invoice_pipeline import decode_invoice
from app.workers.jobs import LeaseLostError, ProcessingError
from app.workers.runtime import RunOutcome

# Reuse the existing synthetic PDF, real upload/migration fixture and fake LLM.
# pytest's default prepend import mode exposes sibling test modules.
from test_invoice_pipeline import (
    ControlledProvider, environment, invoice_response, processor, upload, worker,
)


@pytest.fixture(autouse=True)
def stable_review_tolerance(monkeypatch):
    monkeypatch.setenv("INVOICE_AMOUNT_TOLERANCE", "0.02")


def completed(env, **kwargs):
    document_id = upload(env, **kwargs)
    assert worker(env, processor(ControlledProvider(uncertain=True))).run_once() == RunOutcome.SUCCEEDED
    return document_id, invoice_response(env, document_id)


def correction_body(response, **changes):
    return {
        "request_id": str(uuid4()), "extraction_id": response["extraction_id"],
        "expected_revision": response.get("correction_revision", 0),
        "reviewer_label": "Synthetic reviewer", "reason": "Synthetic source review", "changes": changes,
    }


def correct(env, document_id, body):
    return env["client"].post(f"/api/documents/{document_id}/corrections", json=body)


def failed(env):
    document_id = upload(env)
    claim = env["store"].claim_next("synthetic-failure")
    env["store"].fail(claim, ProcessingError("synthetic_terminal", "Synthetic permanent failure.", retryable=False))
    return document_id, claim


def retry_body(claim):
    return {"request_id": str(uuid4()), "failed_job_id": str(claim.job_id)}


def retry(env, document_id, body):
    return env["client"].post(f"/api/documents/{document_id}/reprocess", json=body)


def count(env, model):
    with env["factory"]() as database:
        return database.scalar(select(func.count()).select_from(model))


def parallel(*operations):
    barrier = Barrier(len(operations))
    def run(operation):
        barrier.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(run, operation) for operation in operations]
        return [future.result(timeout=45) for future in futures]


def test_correction_preserves_source_and_recalculates_validation(environment):
    doc, original = completed(environment, total="120.00", customer=False)
    run_id = UUID(original["extraction_id"])
    with environment["factory"]() as database:
        run = database.get(DocumentExtraction, run_id)
        original_payload = deepcopy(run.invoice_result)
        original_digest = run.invoice_digest
        source_rows = database.execute(select(ExtractedUnit.sequence, ExtractedUnit.payload_sha256).where(ExtractedUnit.extraction_id == run_id)).all()
    response = correct(environment, doc, correction_body(original, total="110.00", customer_name="Reviewed customer"))
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    current = invoice_response(environment, doc)
    assert current["result"]["invoice"]["total"] == "110.00"
    assert current["result"]["invoice"]["customer_name"] == "Reviewed customer"
    assert current["original_result"] == original["result"]
    assert current["corrected_fields"] == ["customer_name", "total"]
    assert current["correction_revision"] == 1
    assert current["result"]["confidence"] == "review_required"
    assert "supplier_name" in current["result"]["invoice"]["uncertain_fields"]
    assert not any(e["field"] == "total" for e in current["result"]["evidence"])
    assert any(e["field"] == "total" for e in current["original_result"]["evidence"])
    assert "storage_key" not in json.dumps(current)
    codes = {issue["code"] for issue in current["result"]["issues"]}
    assert "grand_total_mismatch" not in codes
    assert "customer_name" not in current["result"]["missing_fields"]
    with environment["factory"]() as database:
        run = database.get(DocumentExtraction, run_id)
        assert run.invoice_result == original_payload
        assert run.invoice_digest == original_digest
        assert str(decode_invoice(run).invoice.total) == "120.00"
        assert database.execute(select(ExtractedUnit.sequence, ExtractedUnit.payload_sha256).where(ExtractedUnit.extraction_id == run_id)).all() == source_rows
    validation = environment["client"].get(f"/api/documents/{doc}/validation", params={"extraction_id": str(run_id), "correction_revision": 1})
    assert validation.status_code == 200
    assert validation.json()["correction_revision"] == 1
    assert validation.json()["issues"] == current["result"]["issues"]
    stale = environment["client"].get(f"/api/documents/{doc}/validation", params={"correction_revision": 0})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "correction_changed"


def test_arithmetic_disagreements_are_saved_as_issues_without_repair(environment):
    doc, original = completed(environment)
    body = correction_body(original, total="130.00", line_items=[{
        "description": "Reviewed line", "quantity": "3", "unit_price": "50.00", "line_total": "100.00",
    }], line_items_complete=True)
    assert correct(environment, doc, body).status_code == 200
    current = invoice_response(environment, doc)["result"]
    assert current["invoice"]["total"] == "130.00"
    assert current["invoice"]["line_items"][0]["line_total"] == "100.00"
    assert {issue["code"] for issue in current["issues"]} >= {"grand_total_mismatch", "line_item_mismatch"}
    assert not any(e["field"].startswith("line_items.") for e in current["evidence"])


def test_null_clears_value_and_keeps_original_evidence(environment):
    doc, original = completed(environment)
    assert correct(environment, doc, correction_body(original, currency=None)).status_code == 200
    current = invoice_response(environment, doc)
    assert current["result"]["invoice"]["currency"] is None
    assert "currency" in current["result"]["missing_fields"]
    assert current["original_result"]["invoice"]["currency"] == "USD"


@pytest.mark.parametrize("changes", [
    {}, {"total": 110.0}, {"total": 110}, {"total": True}, {"total": "NaN"},
    {"total": "1e2"}, {"total": "99999999999999999"}, {"total": "1.0000001"},
    {"invoice_date": 0}, {"invoice_date": "2026-02-30"}, {"supplier_name": " "},
    {"currency": "usd"}, {"line_items": None}, {"line_items_complete": None},
    {"line_items_complete": "true"}, {"line_items": []},
    {"line_items": [{"quantity": 1}], "line_items_complete": True},
    {"evidence": []}, {"status": "completed"},
])
def test_invalid_review_input_does_not_mutate_anything(environment, changes):
    doc, original = completed(environment)
    response = correct(environment, doc, correction_body(original, **changes))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert count(environment, InvoiceCorrection) == 0
    assert invoice_response(environment, doc)["correction_revision"] == 0


def test_noop_and_unknown_request_fields_rejected(environment):
    doc, original = completed(environment)
    assert correct(environment, doc, correction_body(original, total="110.000")).json()["error"]["code"] == "no_changes"
    body = correction_body(original, total="111.00")
    body["trusted_reviewer"] = True
    assert correct(environment, doc, body).status_code == 422
    assert count(environment, InvoiceCorrection) == 0


def test_history_is_append_only_paged_and_bound_to_its_document(environment):
    doc, original = completed(environment)
    first = correction_body(original, total="111.00")
    receipt = correct(environment, doc, first).json()
    second = correction_body(invoice_response(environment, doc), total="112.00")
    assert correct(environment, doc, second).status_code == 200
    replay = correct(environment, doc, first)
    assert replay.status_code == 200 and replay.json() == receipt
    assert count(environment, InvoiceCorrection) == 2
    history = environment["client"].get(f"/api/documents/{doc}/corrections", params={"extraction_id": original["extraction_id"], "limit": 1})
    assert history.status_code == 200
    assert history.json()["total"] == 2
    assert history.json()["has_more"] is True
    assert history.json()["items"][0]["revision"] == 2
    assert "result" not in history.json()["items"][0]
    page2 = environment["client"].get(f"/api/documents/{doc}/corrections", params={"extraction_id": original["extraction_id"], "limit": 1, "offset": 1}).json()
    assert page2["items"][0]["revision"] == 1 and page2["has_more"] is False
    detail = environment["client"].get(f"/api/documents/{doc}/corrections/1", params={"extraction_id": original["extraction_id"]})
    assert detail.status_code == 200
    assert detail.json()["before_values"] == {"total": "110.00"}
    assert detail.json()["after_values"] == {"total": "111.00"}
    assert detail.json()["reviewer_label"] == "Synthetic reviewer"
    assert any(issue["code"] == "grand_total_mismatch" for issue in detail.json()["issues_at_save"])
    other = upload(environment)
    assert environment["client"].get(f"/api/documents/{other}/corrections", params={"extraction_id": original["extraction_id"]}).status_code == 404
    assert environment["client"].get(f"/api/documents/{doc}/corrections", params={"extraction_id": original["extraction_id"], "limit": 21}).status_code == 422


def test_conflicts_and_request_id_reuse_are_explicit(environment):
    doc, original = completed(environment)
    body = correction_body(original, total="111")
    assert correct(environment, doc, body).status_code == 200
    changed_request = deepcopy(body)
    changed_request["changes"]["total"] = "112"
    assert correct(environment, doc, changed_request).json()["error"]["code"] == "idempotency_conflict"
    assert correct(environment, doc, correction_body(original, total="113")).json()["error"]["code"] == "correction_changed"
    wrong_attempt = correction_body(original, total="114")
    wrong_attempt["extraction_id"] = str(uuid4())
    assert correct(environment, doc, wrong_attempt).json()["error"]["code"] == "extraction_changed"


def test_correction_identity_drives_duplicate_detection_and_search(environment):
    first, original = completed(environment)
    second, _ = completed(environment)
    assert any(i["code"] == "possible_duplicate" for i in invoice_response(environment, second)["result"]["issues"])
    assert correct(environment, first, correction_body(original, invoice_number="REVIEW-UNIQUE-009", supplier_name="Reviewed corporation")).status_code == 200
    for doc in (first, second):
        assert not any(i["code"] == "possible_duplicate" for i in invoice_response(environment, doc)["result"]["issues"])
    search = environment["client"].get("/api/documents", params={"q": "REVIEW-UNIQUE-009"}).json()
    assert [item["id"] for item in search["items"]] == [str(first)]
    # Clearing a corrected identity must not fall back to the original identity.
    assert correct(environment, first, correction_body(invoice_response(environment, first), invoice_number=None)).status_code == 200
    assert not any(i["code"] == "possible_duplicate" for i in invoice_response(environment, second)["result"]["issues"])


def test_concurrent_corrections_allow_one_revision(environment):
    doc, original = completed(environment)
    requests = [correction_body(original, total="111"), correction_body(original, total="112")]
    results = parallel(*(lambda body=body: correct(environment, doc, body) for body in requests))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert count(environment, InvoiceCorrection) == 1
    assert invoice_response(environment, doc)["correction_revision"] == 1


def test_concurrent_identical_corrections_return_one_receipt(environment):
    doc, original = completed(environment)
    body = correction_body(original, total="111")
    results = parallel(lambda: correct(environment, doc, body), lambda: correct(environment, doc, body))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert count(environment, InvoiceCorrection) == 1


def test_projection_write_failure_rolls_back_audit_and_projection(environment):
    doc, original = completed(environment)
    engine = environment["app"].state.database_engine
    def fail_update(connection, cursor, statement, parameters, context, many):
        if statement.lower().startswith("update document_extractions") and "corrected_result" in statement.lower():
            raise RuntimeError("Synthetic correction persistence failure")
    event.listen(engine, "after_cursor_execute", fail_update)
    try:
        # Use the existing lifespan/engine without starting a second lifespan.
        client = TestClient(environment["app"], raise_server_exceptions=False)
        try:
            response = client.post(f"/api/documents/{doc}/corrections", json=correction_body(original, total="111"))
        finally:
            client.close()
        assert response.status_code == 500
    finally:
        event.remove(engine, "after_cursor_execute", fail_update)
    assert count(environment, InvoiceCorrection) == 0
    assert invoice_response(environment, doc)["correction_revision"] == 0


def test_corrupted_review_projection_is_not_served(environment):
    doc, original = completed(environment)
    assert correct(environment, doc, correction_body(original, total="111")).status_code == 200
    with environment["factory"]() as database:
        run = database.get(DocumentExtraction, UUID(original["extraction_id"]))
        changed = deepcopy(run.corrected_result)
        changed["invoice"]["total"] = "999"
        run.corrected_result = changed
        database.commit()
    response = environment["client"].get(f"/api/documents/{doc}/invoice")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "correction_snapshot_invalid"


def test_retry_uses_existing_queue_and_preserves_failure_and_fencing(environment):
    doc, old_claim = failed(environment)
    metadata = environment["client"].get(f"/api/documents/{doc}").json()
    assert metadata["failed_job_id"] == str(old_claim.job_id)
    body = retry_body(old_claim)
    response = retry(environment, doc, body)
    assert response.status_code == 202, response.text
    receipt = response.json()
    new_id = UUID(receipt["new_job_id"])
    assert new_id != old_claim.job_id
    with environment["factory"]() as database:
        old = database.get(ProcessingJob, old_claim.job_id)
        new = database.get(ProcessingJob, new_id)
        assert old.status == JobStatus.FAILED and old.attempt_count == 1
        assert old.last_error_code == "synthetic_terminal"
        assert old.finished_at is not None
        assert new.status == JobStatus.QUEUED and new.attempt_count == 0
        assert new.max_attempts == environment["settings"].job_max_attempts
        assert new.lease_token is None
        record = database.get(Document, doc)
        assert record.status == DocumentStatus.QUEUED
        assert record.progress_percent == 0 and record.error_code is None
        assert record.processing_finished_at is None
    assert environment["client"].get(f"/api/documents/{doc}/invoice").status_code == 409
    with pytest.raises(LeaseLostError):
        environment["store"].fenced_write(old_claim, lambda database, record: setattr(record, "processing_stage", "stale"))
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    current = invoice_response(environment, doc)
    assert current["result"]["invoice"]["total"] == "110.00"
    assert retry(environment, doc, body).json() == receipt
    assert count(environment, ProcessingJob) == 2
    assert count(environment, ReprocessingRequest) == 1


@pytest.mark.parametrize("state", ["queued", "running", "completed"])
def test_retry_rejects_nonterminal_documents(environment, state):
    doc = upload(environment)
    with environment["factory"]() as database:
        job_id = database.scalar(select(ProcessingJob.id).where(ProcessingJob.document_id == doc))
    if state == "running":
        environment["store"].claim_next("active-worker")
    if state == "completed":
        assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    response = retry(environment, doc, {"request_id": str(uuid4()), "failed_job_id": str(job_id)})
    assert response.status_code == 409
    assert count(environment, ProcessingJob) == 1


def test_retry_rejects_wrong_failure_and_missing_source(environment):
    doc, claim = failed(environment)
    assert retry(environment, doc, {"request_id": str(uuid4()), "failed_job_id": str(uuid4())}).status_code == 409
    with environment["factory"]() as database:
        document = database.get(Document, doc)
        (environment["settings"].local_storage_path / document.storage_key).unlink()
    response = retry(environment, doc, retry_body(claim))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_unavailable"
    assert count(environment, ProcessingJob) == 1


def test_concurrent_retries_enqueue_only_one_new_job(environment):
    doc, claim = failed(environment)
    results = parallel(lambda: retry(environment, doc, retry_body(claim)), lambda: retry(environment, doc, retry_body(claim)))
    assert sorted(r.status_code for r in results) == [202, 409]
    assert count(environment, ProcessingJob) == 2
    assert count(environment, ReprocessingRequest) == 1


def test_identical_retry_requests_are_idempotent_even_after_new_failure(environment):
    doc, claim = failed(environment)
    body = retry_body(claim)
    results = parallel(lambda: retry(environment, doc, body), lambda: retry(environment, doc, body))
    assert [r.status_code for r in results] == [202, 202]
    assert results[0].json() == results[1].json()
    new_claim = environment["store"].claim_next("another-failure")
    environment["store"].fail(new_claim, ProcessingError("synthetic_second", "Second failure.", retryable=False))
    assert retry(environment, doc, body).json() == results[0].json()
    assert retry(environment, doc, retry_body(claim)).status_code == 409
    reused = {**body, "failed_job_id": str(new_claim.job_id)}
    assert retry(environment, doc, reused).json()["error"]["code"] == "idempotency_conflict"
    assert retry(environment, doc, retry_body(new_claim)).status_code == 202
    assert count(environment, ProcessingJob) == 3


def test_retry_receipt_insert_failure_rolls_back_new_job(environment):
    doc, claim = failed(environment)
    engine = environment["app"].state.database_engine
    def fail_insert(connection, cursor, statement, parameters, context, many):
        if statement.lower().startswith("insert into reprocessing_requests"):
            raise RuntimeError("Synthetic retry receipt failure")
    event.listen(engine, "after_cursor_execute", fail_insert)
    try:
        client = TestClient(environment["app"], raise_server_exceptions=False)
        try:
            response = client.post(f"/api/documents/{doc}/reprocess", json=retry_body(claim))
        finally:
            client.close()
        assert response.status_code == 500
    finally:
        event.remove(engine, "after_cursor_execute", fail_insert)
    assert count(environment, ProcessingJob) == 1
    assert count(environment, ReprocessingRequest) == 0
    assert environment["client"].get(f"/api/documents/{doc}").json()["status"] == "failed"


def test_review_migration_preserves_originals_and_matches_metadata(environment):
    doc, original = completed(environment)
    assert correct(environment, doc, correction_body(original, total="111")).status_code == 200
    engine = environment["app"].state.database_engine
    with engine.connect() as connection:
        units = connection.execute(text("SELECT extraction_id, sequence, payload_sha256 FROM extracted_units ORDER BY sequence")).all()
        invoices = connection.execute(text("SELECT id, invoice_digest, invoice_result FROM document_extractions")).all()
    command.check(environment["migration"])
    command.downgrade(environment["migration"], "0003_invoice_results")
    assert "invoice_corrections" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT extraction_id, sequence, payload_sha256 FROM extracted_units ORDER BY sequence")).all() == units
        assert connection.execute(text("SELECT id, invoice_digest, invoice_result FROM document_extractions")).all() == invoices
    command.upgrade(environment["migration"], "head")
    command.check(environment["migration"])
    assert invoice_response(environment, doc)["correction_revision"] == 0


@pytest.mark.parametrize("declared", [None, b"9999999"])
def test_json_body_limit_rejects_before_endpoint_or_json_parsing(declared):
    called = False
    messages = []
    async def downstream(scope, receive, send):
        nonlocal called
        called = True
    async def receive():
        return {"type": "http.request", "body": b"x" * 5000, "more_body": False}
    async def send(message):
        messages.append(message)
    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", declared))
    scope = {"type": "http", "method": "POST", "path": f"/api/documents/{uuid4()}/reprocess", "headers": headers}
    asyncio.run(ReviewBodyLimitMiddleware(downstream)(scope, receive, send))
    assert not called
    assert messages[0]["status"] == 413


def test_commands_have_structured_errors_for_invalid_or_missing_resources(environment):
    doc = upload(environment)
    assert environment["client"].post(f"/api/documents/{doc}/reprocess", json={"request_id": str(uuid4())}).status_code == 422
    assert environment["client"].post(f"/api/documents/{doc}/reprocess", content="bad", headers={"Content-Type": "text/plain"}).status_code == 415
    body = {"request_id": str(uuid4()), "failed_job_id": str(uuid4())}
    response = retry(environment, uuid4(), body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"
    correction = {"request_id": str(uuid4()), "extraction_id": str(uuid4()), "expected_revision": 0, "reviewer_label": "Reviewer", "reason": "Synthetic", "changes": {"total": "1"}}
    assert correct(environment, doc, correction).status_code == 409
