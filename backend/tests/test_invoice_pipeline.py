"""Upload -> durable worker -> invoice persistence -> real API integration.

ControlledProvider is explicitly identified as test output. The separately
opted-in live test performs a real Gemini call with synthetic input only.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import pymupdf
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text

from app.core.config import BACKEND_DIR, Settings
from app.core.database import utc_now
from app.main import create_app
from app.models.extraction import DocumentExtraction, ExtractedUnit
from app.models.ingestion import Document, JobStatus, ProcessingJob
from app.schemas.invoice import Invoice, LineItem, SourceEvidence
from app.services.document_processor import DocumentProcessor
from app.services.invoice_pipeline import InvoiceProcessor, decode_invoice
from app.services.invoice_provider import InvoiceSettings
from app.services.reading_types import ReadingSettings
from app.workers.jobs import JobStore, ProcessingError
from app.workers.runtime import RunOutcome, WorkerContext, WorkerRuntime


class Clock:
    def __init__(self):
        self.value = utc_now() + timedelta(minutes=1)

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
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'invoice.db').as_posix()}",
        local_storage_path=tmp_path / "uploads",
        max_file_size_mb=10,
        max_upload_total_mb=20,
        job_max_attempts=3,
        job_lease_seconds=30,
        cors_origins=[],
    )
    migration = Config(str(BACKEND_DIR / "alembic.ini"))
    migration.attributes["settings"] = settings
    command.upgrade(migration, "head")
    application = create_app(settings)
    with TestClient(application) as client:
        factory = application.state.session_factory
        clock = Clock()
        store = JobStore(
            factory,
            lease_seconds=30,
            retry_base_seconds=5,
            clock=clock,
        )
        yield {
            "settings": settings,
            "migration": migration,
            "app": application,
            "client": client,
            "factory": factory,
            "clock": clock,
            "store": store,
        }


def synthetic_pdf(*, total="110.00", currency="USD", customer=True):
    lines = [
        "Invoice number: SYN-INTEGRATION-001",
        "Invoice date: 2026-10-30",
        "Supplier: Synthetic Supplies Ltd",
    ]
    if customer:
        lines.append("Customer: Example Customer Ltd")
    lines.extend([
        f"Currency: {currency}",
        "Description: Consulting",
        "Quantity: 2",
        "Unit price: 50.00",
        "Line total: 100.00",
        "Subtotal: 100.00",
        "Tax: 10.00",
        f"Total: {total}",
    ])
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 50), "\n".join(lines), fontsize=10)
        page = document.new_page()
        page.insert_text(
            (40, 50),
            "Supporting document page. Synthetic consulting delivery record.",
            fontsize=10,
        )
        return document.tobytes()


def upload(environment, *, filename="synthetic.pdf", **kwargs):
    response = environment["client"].post(
        "/api/documents/upload",
        files=[("files", (filename, synthetic_pdf(**kwargs), "application/pdf"))],
    )
    assert response.status_code == 202, response.text
    assert response.json()["documents"][0]["status"] == "queued"
    return UUID(response.json()["documents"][0]["id"])


class ControlledProvider:
    name = "test"
    model = "controlled-integration-provider"

    def __init__(self, *, fail_once=False, callback=None, uncertain=False):
        self.calls = 0
        self.fail_once = fail_once
        self.callback = callback
        self.uncertain = uncertain

    def extract(self, contents):
        self.calls += 1
        if self.callback is not None:
            self.callback()
        if self.fail_once and self.calls == 1:
            raise ProcessingError(
                "synthetic_gemini_unavailable",
                "Synthetic retryable provider failure.",
                retryable=True,
            )

        records = json.loads(contents)["document_units"]
        evidence = []
        labels = {
            "invoice_number": "Invoice number",
            "invoice_date": "Invoice date",
            "supplier_name": "Supplier",
            "customer_name": "Customer",
            "currency": "Currency",
            "subtotal": "Subtotal",
            "tax": "Tax",
            "total": "Total",
            "line_items.0.description": "Description",
            "line_items.0.quantity": "Quantity",
            "line_items.0.unit_price": "Unit price",
            "line_items.0.line_total": "Line total",
        }
        values = {}
        for field, label in labels.items():
            for record in records:
                match = re.search(
                    rf"^{re.escape(label)}: ([^\r\n]+)$",
                    record["text"],
                    flags=re.MULTILINE,
                )
                if match:
                    values[field] = match.group(1).strip()
                    evidence.append(SourceEvidence(
                        field=field,
                        sequence=record["sequence"],
                        quote=match.group(0),
                    ))
                    break

        headers = {
            field: value for field, value in values.items()
            if not field.startswith("line_items.")
        }
        line = {
            field.rsplit(".", 1)[1]: value
            for field, value in values.items()
            if field.startswith("line_items.")
        }
        return Invoice(
            **headers,
            line_items=[LineItem(**line)] if line else [],
            line_items_complete=True,
            uncertain_fields=["supplier_name"] if self.uncertain else [],
            evidence=evidence,
        )


def processor(provider, **kwargs):
    return InvoiceProcessor(
        document_processor=DocumentProcessor(
            reading_settings=ReadingSettings(_env_file=None),
            batch_units=1,
        ),
        invoice_settings=InvoiceSettings(
            _env_file=None,
            gemini_api_key=None,
            gemini_model="controlled-model",
            tolerance="0.02",
        ),
        provider=provider,
        read_batch_units=1,
        **kwargs,
    )


def worker(environment, handler):
    return WorkerRuntime(
        environment["store"],
        environment["settings"],
        worker_id="invoice-integration-worker",
        processor=handler,
        heartbeat_interval=29,
    )


def context_for(environment, claim):
    return WorkerContext(
        claim=claim,
        settings=environment["settings"],
        store=environment["store"],
        lease_lost=Event(),
    )


def invoice_response(environment, document_id):
    response = environment["client"].get(f"/api/documents/{document_id}/invoice")
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_worker_invoice_and_content_api(environment):
    document_id = upload(environment)
    adapter = ControlledProvider()
    assert worker(environment, processor(adapter)).run_once() == RunOutcome.SUCCEEDED
    assert adapter.calls == 1

    response = invoice_response(environment, document_id)
    result = response["result"]
    assert result["provider"] == "test"
    assert result["invoice"]["invoice_number"] == "SYN-INTEGRATION-001"
    assert result["invoice"]["total"] == "110.00"
    assert result["invoice"]["line_items"][0]["quantity"] == "2"
    assert result["issues"] == []
    assert result["review_required"] is False
    assert result["evidence"][0]["position"]["page_number"] == 1
    assert "storage_key" not in json.dumps(response)

    client = environment["client"]
    metadata = client.get(f"/api/documents/{document_id}").json()
    assert metadata["status"] == "completed"
    assert metadata["progress_percent"] == 100

    first_response = client.get(
        f"/api/documents/{document_id}/content", params={"limit": 1}
    )
    assert first_response.status_code == 200
    first = first_response.json()
    assert first["total_units"] == 2
    assert first["items"][0]["sequence"] == 1
    assert first["items"][0]["position"]["page_number"] == 1
    assert first["next_after_sequence"] == 1
    assert first["has_more"] is True
    assert "storage_key" not in json.dumps(first)

    second_response = client.get(
        f"/api/documents/{document_id}/content",
        params={
            "limit": 1,
            "after_sequence": first["next_after_sequence"],
            "extraction_id": first["extraction_id"],
        },
    )
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["items"][0]["sequence"] == 2
    assert second["has_more"] is False
    assert second["next_after_sequence"] is None

    with environment["factory"]() as session:
        extraction = session.get(DocumentExtraction, UUID(response["extraction_id"]))
        assert decode_invoice(extraction).invoice.total == Decimal("110.00")
        assert extraction.invoice_identity is not None
        assert extraction.invoice_digest is not None
        assert extraction.invoice_created_at is not None
        assert session.scalar(select(func.count()).select_from(ExtractedUnit)) == 2


def test_bad_total_missing_field_and_uncertainty_survive_persistence(environment):
    document_id = upload(environment, total="120.00", customer=False)
    handler = processor(ControlledProvider(uncertain=True))
    assert worker(environment, handler).run_once() == RunOutcome.SUCCEEDED

    response = invoice_response(environment, document_id)
    assert response["result"]["invoice"]["total"] == "120.00"
    assert response["result"]["invoice"]["customer_name"] is None
    validation = environment["client"].get(
        f"/api/documents/{document_id}/validation"
    )
    assert validation.status_code == 200
    body = validation.json()
    assert body["review_required"] is True
    assert "customer_name" in body["missing_fields"]
    assert body["uncertain_fields"] == ["supplier_name"]
    assert {item["code"] for item in body["issues"]} >= {
        "grand_total_mismatch", "missing_field", "uncertain_field"
    }
    mismatch = next(i for i in body["issues"] if i["code"] == "grand_total_mismatch")
    assert mismatch["expected"] == "110.00"
    assert mismatch["actual"] == "120.00"


def test_staged_invoice_is_hidden_until_existing_completion_commits(environment):
    document_id = upload(environment)
    store = environment["store"]
    claim = store.claim_next("manual-worker")
    adapter = ControlledProvider()
    handler = processor(adapter)
    context = context_for(environment, claim)
    result = handler(context)

    for suffix in ("invoice", "validation", "content"):
        response = environment["client"].get(f"/api/documents/{document_id}/{suffix}")
        assert response.status_code == 409

    with environment["factory"]() as session:
        extraction = session.get(DocumentExtraction, claim.lease_token)
        digest = extraction.invoice_digest
        assert extraction.invoice_result is not None
        assert session.get(Document, document_id).progress_percent == 99

    # Same-token replay must use the staged invoice, not spend another call.
    assert handler(context) == result
    assert adapter.calls == 1
    with environment["factory"]() as session:
        assert session.get(DocumentExtraction, claim.lease_token).invoice_digest == digest

    store.complete(claim, result)
    assert invoice_response(environment, document_id)["extraction_id"] == str(claim.lease_token)


def test_provider_runs_without_a_database_transaction_lock(environment):
    document_id = upload(environment)
    claim = environment["store"].claim_next("manual-worker")

    def during_provider():
        # A second connection can renew the same lease during the provider call.
        assert environment["store"].heartbeat(claim)
        response = environment["client"].get(f"/api/documents/{document_id}")
        assert response.json()["processing_stage"] == "extracting_invoice"
        assert response.json()["status"] == "processing"

    result = processor(ControlledProvider(callback=during_provider))(
        context_for(environment, claim)
    )
    environment["store"].complete(claim, result)
    invoice_response(environment, document_id)


def test_transient_provider_failure_retries_without_publishing_partial_data(environment):
    document_id = upload(environment)
    adapter = ControlledProvider(fail_once=True)
    runtime = worker(environment, processor(adapter))
    assert runtime.run_once() == RunOutcome.RETRY_SCHEDULED

    with environment["factory"]() as session:
        extraction = session.scalar(select(DocumentExtraction))
        assert extraction.ready_at is not None
        assert extraction.invoice_result is None
    assert environment["client"].get(
        f"/api/documents/{document_id}/content"
    ).status_code == 409
    assert environment["client"].get(
        "/api/documents", params={"q": "Synthetic Supplies"}
    ).json()["total"] == 0

    environment["clock"].advance(5)
    assert runtime.run_once() == RunOutcome.SUCCEEDED
    assert adapter.calls == 2
    invoice_response(environment, document_id)
    with environment["factory"]() as session:
        attempts = session.scalars(
            select(DocumentExtraction).order_by(DocumentExtraction.attempt_number)
        ).all()
        assert len(attempts) == 2
        assert attempts[0].invoice_result is None
        assert attempts[1].invoice_result is not None
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.document_id == document_id)
        )
        assert job.status == JobStatus.SUCCEEDED
        assert job.attempt_count == 2


def test_failure_after_invoice_staging_does_not_publish_old_attempt(environment):
    document_id = upload(environment)
    adapter = ControlledProvider()
    handler = processor(adapter)

    def fail_after_staging(context):
        handler(context)
        raise ProcessingError(
            "synthetic_after_staging", "Synthetic post-staging failure.", retryable=True
        )

    assert worker(environment, fail_after_staging).run_once() == RunOutcome.RETRY_SCHEDULED
    with environment["factory"]() as session:
        old = session.scalar(select(DocumentExtraction))
        old_id = old.id
        assert old.invoice_result is not None
    assert environment["client"].get(
        f"/api/documents/{document_id}/invoice"
    ).status_code == 409

    environment["clock"].advance(5)
    assert worker(environment, handler).run_once() == RunOutcome.SUCCEEDED
    response = invoice_response(environment, document_id)
    assert response["extraction_id"] != str(old_id)
    stale = environment["client"].get(
        f"/api/documents/{document_id}/invoice",
        params={"extraction_id": str(old_id)},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "extraction_changed"


def test_expired_lease_after_llm_call_cannot_save_invoice(environment):
    document_id = upload(environment)
    replacement = []

    def expire():
        environment["clock"].advance(31)
        assert environment["store"].recover_expired() == 1
        environment["clock"].advance(5)
        claim = environment["store"].claim_next("replacement-worker")
        assert claim is not None
        replacement.append(claim)

    outcome = worker(
        environment, processor(ControlledProvider(callback=expire))
    ).run_once()
    assert outcome == RunOutcome.LEASE_LOST
    with environment["factory"]() as session:
        assert session.scalar(select(DocumentExtraction)).invoice_result is None

    claim = replacement[0]
    result = processor(ControlledProvider())(context_for(environment, claim))
    environment["store"].complete(claim, result)
    assert invoice_response(environment, document_id)["extraction_id"] == str(claim.lease_token)


def test_invoice_update_failure_rolls_back_all_invoice_columns(environment):
    document_id = upload(environment)
    engine = environment["app"].state.database_engine

    def fail_after_update(connection, cursor, statement, parameters, context, many):
        normalized = statement.strip().lower()
        if normalized.startswith("update document_extractions") and "invoice_result=" in normalized:
            raise RuntimeError("Synthetic database failure after invoice UPDATE")

    event.listen(engine, "after_cursor_execute", fail_after_update)
    try:
        outcome = worker(environment, processor(ControlledProvider())).run_once()
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_update)
    assert outcome == RunOutcome.RETRY_SCHEDULED
    with environment["factory"]() as session:
        extraction = session.scalar(select(DocumentExtraction))
        assert extraction.invoice_result is None
        assert extraction.invoice_identity is None
        assert extraction.invoice_digest is None
        assert extraction.invoice_created_at is None
    assert environment["client"].get(
        f"/api/documents/{document_id}/invoice"
    ).status_code == 409


def test_missing_gemini_credentials_fail_without_fallback(environment):
    document_id = upload(environment)
    assert worker(environment, processor(None)).run_once() == RunOutcome.FAILED
    metadata = environment["client"].get(f"/api/documents/{document_id}").json()
    assert metadata["error_code"] == "gemini_credentials_missing"
    assert metadata["status"] == "failed"
    assert environment["client"].get(
        f"/api/documents/{document_id}/invoice"
    ).status_code == 409


def test_duplicate_snapshot_and_symmetric_current_detection(environment):
    first = upload(environment)
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    second = upload(environment, currency="EUR")
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED

    for document_id, other in ((first, second), (second, first)):
        response = invoice_response(environment, document_id)
        duplicates = [
            issue for issue in response["result"]["issues"]
            if issue["code"] == "possible_duplicate"
        ]
        assert len(duplicates) == 1
        assert duplicates[0]["related_document_id"] == str(other)
        assert any(
            issue["code"] == "duplicate_currency_mismatch"
            for issue in response["result"]["issues"]
        )

    with environment["factory"]() as session:
        rows = session.scalars(select(DocumentExtraction)).all()
        snapshots = {row.document_id: decode_invoice(row) for row in rows}
        assert "possible_duplicate" not in {i.code for i in snapshots[first].issues}
        assert "possible_duplicate" in {i.code for i in snapshots[second].issues}


def test_duplicates_are_visible_when_both_attempts_staged_before_completion(environment):
    first = upload(environment)
    second = upload(environment)
    store = environment["store"]
    claims = [store.claim_next("worker-one"), store.claim_next("worker-two")]
    completed = []
    for claim in claims:
        result = processor(ControlledProvider())(context_for(environment, claim))
        completed.append((claim, result))
    # Neither snapshot can see the other as published at staging time.
    with environment["factory"]() as session:
        for row in session.scalars(select(DocumentExtraction)):
            assert "possible_duplicate" not in {i.code for i in decode_invoice(row).issues}
    for claim, result in completed:
        store.complete(claim, result)
    for document_id in (first, second):
        response = invoice_response(environment, document_id)
        assert "possible_duplicate" in {i["code"] for i in response["result"]["issues"]}


def test_search_filters_and_literal_wildcards(environment):
    first = upload(environment, filename="percent-100%.pdf")
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    upload(environment, filename="ordinary.pdf")
    client = environment["client"]
    for query in ("Synthetic Supplies", "SYN-INTEGRATION-001", "delivery record", "%"):
        response = client.get("/api/documents", params={"q": query})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["id"] == str(first)
    assert client.get(
        "/api/documents", params={"status": "queued", "extension": ".pdf"}
    ).json()["total"] == 1
    assert client.get(
        "/api/documents", params={"q": "Synthetic Supplies", "status": "queued"}
    ).json()["total"] == 0
    assert client.get("/api/documents", params={"q": "   "}).status_code == 422
    assert client.get("/api/documents", params={"status": "invalid"}).status_code == 422


def test_endpoint_errors_and_versioned_content_cursor(environment):
    client = environment["client"]
    for suffix in ("invoice", "validation", "content"):
        response = client.get(f"/api/documents/{uuid4()}/{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "document_not_found"
    document_id = upload(environment)
    assert client.get(f"/api/documents/{document_id}/invoice").status_code == 409
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    assert client.get(
        f"/api/documents/{document_id}/content", params={"after_sequence": 1}
    ).status_code == 422
    assert client.get(
        f"/api/documents/{document_id}/content", params={"extraction_id": str(uuid4())}
    ).status_code == 409
    assert client.get(
        f"/api/documents/{document_id}/content", params={"limit": 51}
    ).status_code == 422


def test_existing_reading_only_processor_remains_supported(environment):
    document_id = upload(environment)
    handler = DocumentProcessor(reading_settings=ReadingSettings(_env_file=None))
    assert worker(environment, handler).run_once() == RunOutcome.SUCCEEDED
    client = environment["client"]
    assert client.get(f"/api/documents/{document_id}/content").status_code == 200
    response = client.get(f"/api/documents/{document_id}/invoice")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invoice_not_available"


def test_modified_snapshot_is_not_returned_as_valid_invoice(environment):
    document_id = upload(environment)
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    with environment["factory"]() as session:
        extraction = session.scalar(select(DocumentExtraction))
        changed = json.loads(json.dumps(extraction.invoice_result))
        changed["invoice"]["total"] = "999.00"
        extraction.invoice_result = changed
        session.commit()
    response = environment["client"].get(f"/api/documents/{document_id}/invoice")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "invoice_snapshot_invalid"


def test_migration_round_trip_preserves_original_extracted_units(environment):
    document_id = upload(environment)
    assert worker(environment, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    engine = environment["app"].state.database_engine
    migration = environment["migration"]
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT extraction_id, sequence, text FROM extracted_units ORDER BY sequence")
        ).all()
    command.downgrade(migration, "0002_extracted_content")
    assert "invoice_result" not in {
        column["name"] for column in inspect(engine).get_columns("document_extractions")
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT extraction_id, sequence, text FROM extracted_units ORDER BY sequence")
        ).all() == before
    command.upgrade(migration, "head")
    assert environment["client"].get(
        f"/api/documents/{document_id}/content"
    ).status_code == 200
    assert environment["client"].get(
        f"/api/documents/{document_id}/invoice"
    ).json()["error"]["code"] == "invoice_not_available"


@pytest.mark.skipif(
    os.getenv("INVOICE_RUN_LIVE_TESTS") != "1",
    reason="Opt in explicitly to a real billable Gemini integration smoke test.",
)
def test_live_gemini_upload_worker_persistence_and_retrieval(environment):
    document_id = upload(environment)
    # Use the real clock and regular heartbeat for the live network call.
    store = JobStore(environment["factory"], lease_seconds=180)
    handler = InvoiceProcessor(
        document_processor=DocumentProcessor(
            reading_settings=ReadingSettings(_env_file=None)
        ),
        invoice_settings=InvoiceSettings(),
    )
    runtime = WorkerRuntime(
        store,
        environment["settings"],
        worker_id="live-gemini-smoke-worker",
        processor=handler,
    )
    assert runtime.run_once() == RunOutcome.SUCCEEDED
    response = invoice_response(environment, document_id)
    assert response["result"]["provider"] == "gemini"
    assert response["result"]["invoice"]["invoice_number"] == "SYN-INTEGRATION-001"
    assert Decimal(response["result"]["invoice"]["total"]) == Decimal("110.00")
    assert response["result"]["evidence"]
