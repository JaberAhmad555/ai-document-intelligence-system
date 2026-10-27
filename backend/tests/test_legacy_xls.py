from __future__ import annotations

import hashlib
import io
from pathlib import Path
from uuid import UUID

import pytest
import xlrd
import xlwt
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import BACKEND_DIR, Settings
from app.main import create_app
from app.models.ingestion import Document, JobStatus, ProcessingJob
from app.services.storage import LocalStorage


OLE_COMPOUND_FILE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def make_synthetic_xls() -> bytes:
    """Create a real BIFF XLS workbook using only synthetic data.

    No external files, Microsoft Excel installation, business documents,
    formulas, or macros are required.
    """
    workbook = xlwt.Workbook(encoding="utf-8")

    summary = workbook.add_sheet("Invoice")
    summary_rows = [
        ("field", "value"),
        ("invoice_number", "SYN-XLS-001"),
        ("supplier", "Synthetic Supplier"),
        ("customer", "Synthetic Customer"),
        ("currency", "USD"),
        ("subtotal", 125),
        ("tax", 25),
        ("total", 150),
    ]

    for row_number, row in enumerate(summary_rows):
        for column_number, value in enumerate(row):
            summary.write(row_number, column_number, value)

    lines = workbook.add_sheet("Line Items")
    line_rows = [
        ("description", "quantity", "unit_price", "line_total"),
        ("Synthetic widget", 2, 50, 100),
        ("Synthetic service", 1, 25, 25),
    ]

    for row_number, row in enumerate(line_rows):
        for column_number, value in enumerate(row):
            lines.write(row_number, column_number, value)

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


@pytest.fixture
def xls_payload() -> bytes:
    return make_synthetic_xls()


@pytest.fixture
def xls_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            f"sqlite+pysqlite:///{(tmp_path / 'legacy-xls.db').as_posix()}"
        ),
        local_storage_path=tmp_path / "uploads",
        max_upload_files=2,
        max_file_size_mb=1,
        max_upload_total_mb=2,
        upload_chunk_size_bytes=64 * 1024,
        job_max_attempts=3,
        cors_origins=[],
    )


def test_synthetic_fixture_is_a_readable_legacy_xls(xls_payload):
    assert xls_payload.startswith(OLE_COMPOUND_FILE_SIGNATURE)
    assert not xls_payload.startswith(b"PK")

    workbook = xlrd.open_workbook(
        file_contents=xls_payload,
        on_demand=True,
    )

    try:
        assert workbook.sheet_names() == ["Invoice", "Line Items"]

        summary = workbook.sheet_by_name("Invoice")
        assert summary.cell_value(1, 1) == "SYN-XLS-001"
        assert summary.cell_value(7, 1) == 150

        lines = workbook.sheet_by_name("Line Items")
        assert lines.nrows == 3
        assert lines.cell_value(1, 0) == "Synthetic widget"
        assert lines.cell_value(1, 1) == 2
        assert lines.cell_value(1, 3) == 100
    finally:
        workbook.release_resources()


def test_legacy_xls_upload_against_migrated_database(
    xls_payload,
    xls_settings,
):
    migration_config = Config(str(BACKEND_DIR / "alembic.ini"))
    migration_config.attributes["settings"] = xls_settings
    command.upgrade(migration_config, "head")

    application = create_app(xls_settings)

    with TestClient(application) as client:
        response = client.post(
            "/api/documents/upload",
            files=[
                (
                    "files",
                    (
                        "synthetic-invoice.xls",
                        xls_payload,
                        # Validation must inspect contents rather than
                        # trust the browser-provided MIME type.
                        "application/octet-stream",
                    ),
                ),
            ],
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["count"] == 1

        document_response = payload["documents"][0]
        document_id = UUID(document_response["id"])

        assert document_response["original_filename"] == "synthetic-invoice.xls"
        assert document_response["extension"] == ".xls"
        assert document_response["media_type"] == "application/vnd.ms-excel"
        assert document_response["size_bytes"] == len(xls_payload)
        assert document_response["sha256"] == hashlib.sha256(
            xls_payload
        ).hexdigest()
        assert document_response["status"] == "queued"
        assert document_response["progress_percent"] == 0
        assert document_response["total_units"] is None

        listing = client.get("/api/documents")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1

        detail = client.get(f"/api/documents/{document_id}")
        assert detail.status_code == 200
        assert detail.json() == document_response

        with application.state.session_factory() as session:
            document = session.get(Document, document_id)
            assert document is not None

            job = session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.document_id == document_id
                )
            )
            assert job is not None
            assert job.status == JobStatus.QUEUED
            assert job.attempt_count == 0
            assert job.max_attempts == 3

            stored_path = LocalStorage(xls_settings).path_for(
                document.storage_key
            )
            assert stored_path.read_bytes() == xls_payload