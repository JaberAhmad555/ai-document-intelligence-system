from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.core.config import BACKEND_DIR, Settings
from app.core.database import Base, build_engine
from app.main import create_app

# Ensure model metadata is registered before schema comparison.
from app.models import ingestion  # noqa: F401


REVISION = "0001_initial_ingestion"


@pytest.fixture
def migration_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            f"sqlite+pysqlite:///{(tmp_path / 'migration-test.db').as_posix()}"
        ),
        local_storage_path=tmp_path / "uploads",
        max_upload_files=2,
        max_file_size_mb=1,
        max_upload_total_mb=2,
        upload_chunk_size_bytes=64 * 1024,
        cors_origins=[],
    )


@pytest.fixture
def alembic_config(migration_settings: Settings) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    return config


@pytest.fixture
def migrated_engine(
    alembic_config: Config,
    migration_settings: Settings,
) -> Iterator[Engine]:
    # Schema creation happens only through Alembic in this test module.
    command.upgrade(alembic_config, "head")
    engine = build_engine(migration_settings)

    try:
        yield engine
    finally:
        engine.dispose()


def insert_document(
    connection: Connection,
    *,
    storage_key: str | None = None,
) -> UUID:
    document_id = uuid4()
    timestamp = datetime.now(timezone.utc).isoformat()

    # Use raw SQL to exercise database defaults, rather than ORM defaults.
    connection.execute(
        text(
            """
            INSERT INTO documents (
                id,
                original_filename,
                storage_key,
                extension,
                media_type,
                size_bytes,
                sha256,
                created_at,
                updated_at
            )
            VALUES (
                :id,
                :original_filename,
                :storage_key,
                :extension,
                :media_type,
                :size_bytes,
                :sha256,
                :created_at,
                :updated_at
            )
            """
        ),
        {
            "id": document_id.hex,
            "original_filename": "synthetic.csv",
            "storage_key": storage_key or f"{document_id.hex}.csv",
            "extension": ".csv",
            "media_type": "text/csv",
            "size_bytes": 32,
            "sha256": "a" * 64,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    return document_id


def insert_job(
    connection: Connection,
    document_id: UUID,
) -> UUID:
    job_id = uuid4()
    timestamp = datetime.now(timezone.utc).isoformat()

    connection.execute(
        text(
            """
            INSERT INTO processing_jobs (
                id,
                document_id,
                max_attempts,
                available_at,
                created_at,
                updated_at
            )
            VALUES (
                :id,
                :document_id,
                :max_attempts,
                :available_at,
                :created_at,
                :updated_at
            )
            """
        ),
        {
            "id": job_id.hex,
            "document_id": document_id.hex,
            "max_attempts": 3,
            "available_at": timestamp,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    return job_id


def test_upgrade_matches_existing_model_metadata(migrated_engine):
    with migrated_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        differences = compare_metadata(migration_context, Base.metadata)

        assert differences == []
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == REVISION


def test_named_constraints_and_indexes_exist(migrated_engine):
    inspector = inspect(migrated_engine)

    document_checks = {
        item["name"]
        for item in inspector.get_check_constraints("documents")
    }
    assert document_checks == {
        "ck_documents_document_status",
        "ck_documents_positive_size",
        "ck_documents_valid_progress",
        "ck_documents_nonnegative_processed_units",
        "ck_documents_nonnegative_total_units",
        "ck_documents_processed_units_within_total",
    }

    job_checks = {
        item["name"]
        for item in inspector.get_check_constraints("processing_jobs")
    }
    assert job_checks == {
        "ck_processing_jobs_processing_job_kind",
        "ck_processing_jobs_processing_job_status",
        "ck_processing_jobs_nonnegative_attempt_count",
        "ck_processing_jobs_positive_max_attempts",
        "ck_processing_jobs_attempt_count_within_limit",
        "ck_processing_jobs_running_job_has_lease",
    }

    document_indexes = {
        item["name"]
        for item in inspector.get_indexes("documents")
    }
    assert document_indexes == {
        "ix_documents_sha256",
        "ix_documents_created_at_id",
        "ix_documents_status_created_at",
    }

    job_indexes = {
        item["name"]: item
        for item in inspector.get_indexes("processing_jobs")
    }
    assert set(job_indexes) == {
        "ix_processing_jobs_document_id",
        "ix_processing_jobs_claim",
        "ix_processing_jobs_expired_lease",
        "uq_processing_jobs_active_document",
    }

    active_index = job_indexes["uq_processing_jobs_active_document"]
    assert active_index["unique"]
    predicate = str(active_index["dialect_options"]["sqlite_where"])
    assert "queued" in predicate
    assert "running" in predicate

    unique_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("documents")
    }
    assert "uq_documents_storage_key" in unique_constraints

    foreign_keys = inspector.get_foreign_keys("processing_jobs")
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "documents"
    assert foreign_keys[0]["options"]["ondelete"].upper() == "CASCADE"


def test_database_defaults_are_applied(migrated_engine):
    with migrated_engine.begin() as connection:
        document_id = insert_document(connection)
        job_id = insert_job(connection, document_id)

        document = connection.execute(
            text("SELECT * FROM documents WHERE id = :id"),
            {"id": document_id.hex},
        ).mappings().one()

        assert document["storage_backend"] == "local"
        assert document["status"] == "queued"
        assert document["processing_stage"] == "queued"
        assert document["progress_percent"] == 0
        assert document["processed_units"] == 0
        assert document["total_units"] is None

        job = connection.execute(
            text("SELECT * FROM processing_jobs WHERE id = :id"),
            {"id": job_id.hex},
        ).mappings().one()

        assert job["kind"] == "process_document"
        assert job["status"] == "queued"
        assert job["attempt_count"] == 0
        assert job["lease_token"] is None


@pytest.mark.parametrize(
    "updates",
    [
        {"size_bytes": 0},
        {"progress_percent": -1},
        {"progress_percent": 101},
        {"processed_units": -1},
        {"total_units": -1},
        {"total_units": 1, "processed_units": 2},
        {"status": "invalid"},
    ],
)
def test_document_constraints_reject_invalid_values(migrated_engine, updates):
    with migrated_engine.begin() as connection:
        document_id = insert_document(connection)

    # Column names below come exclusively from the fixed test cases.
    assignments = ", ".join(f"{column} = :{column}" for column in updates)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(f"UPDATE documents SET {assignments} WHERE id = :id"),
                {"id": document_id.hex, **updates},
            )


@pytest.mark.parametrize(
    "updates",
    [
        {"attempt_count": -1},
        {"max_attempts": 0},
        {"attempt_count": 4},
        {"status": "invalid"},
        {"kind": "invalid"},
        {"status": "running"},
    ],
)
def test_job_constraints_reject_invalid_values(migrated_engine, updates):
    with migrated_engine.begin() as connection:
        document_id = insert_document(connection)
        job_id = insert_job(connection, document_id)

    assignments = ", ".join(f"{column} = :{column}" for column in updates)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE processing_jobs SET {assignments} "
                    "WHERE id = :id"
                ),
                {"id": job_id.hex, **updates},
            )


def test_only_one_active_job_per_document(migrated_engine):
    with migrated_engine.begin() as connection:
        document_id = insert_document(connection)
        first_job_id = insert_job(connection, document_id)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            insert_job(connection, document_id)

    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE processing_jobs SET status = 'failed' "
                "WHERE id = :id"
            ),
            {"id": first_job_id.hex},
        )
        insert_job(connection, document_id)

        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM processing_jobs "
                "WHERE document_id = :document_id"
            ),
            {"document_id": document_id.hex},
        ) == 2


def test_storage_keys_must_be_unique(migrated_engine):
    storage_key = f"{uuid4().hex}.csv"

    with migrated_engine.begin() as connection:
        insert_document(connection, storage_key=storage_key)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            insert_document(connection, storage_key=storage_key)


def test_job_requires_an_existing_document(migrated_engine):
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            insert_job(connection, uuid4())


def test_document_deletion_cascades_to_jobs(migrated_engine):
    with migrated_engine.begin() as connection:
        document_id = insert_document(connection)
        insert_job(connection, document_id)

        connection.execute(
            text("DELETE FROM documents WHERE id = :id"),
            {"id": document_id.hex},
        )

        assert connection.scalar(
            text("SELECT COUNT(*) FROM processing_jobs")
        ) == 0


def test_upgrade_downgrade_upgrade(
    alembic_config,
    migration_settings,
):
    command.upgrade(alembic_config, "head")
    engine = build_engine(migration_settings)

    try:
        assert {"documents", "processing_jobs"}.issubset(
            inspect(engine).get_table_names()
        )

        command.downgrade(alembic_config, "base")

        remaining_tables = inspect(engine).get_table_names()
        assert "documents" not in remaining_tables
        assert "processing_jobs" not in remaining_tables

        command.upgrade(alembic_config, "head")

        assert {"documents", "processing_jobs"}.issubset(
            inspect(engine).get_table_names()
        )

        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == REVISION
    finally:
        engine.dispose()


def test_postgresql_offline_sql_generation(
    alembic_config,
    tmp_path,
):
    # This is SQL compilation coverage, not a live PostgreSQL test.
    alembic_config.attributes["settings"] = Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            "postgresql+psycopg://migration_test@localhost/"
            "document_intelligence_test"
        ),
        local_storage_path=tmp_path / "unused-storage",
    )

    output = io.StringIO()
    alembic_config.output_buffer = output

    command.upgrade(alembic_config, "head", sql=True)
    generated_sql = output.getvalue()

    assert "CREATE TABLE documents" in generated_sql
    assert "CREATE TABLE processing_jobs" in generated_sql
    assert "UUID" in generated_sql
    assert "TIMESTAMP WITH TIME ZONE" in generated_sql
    assert "uq_processing_jobs_active_document" in generated_sql
    assert "WHERE status IN ('queued', 'running')" in generated_sql


def test_api_upload_and_listing_use_migrated_database(
    alembic_config,
    migration_settings,
):
    command.upgrade(alembic_config, "head")
    application = create_app(migration_settings)

    with TestClient(application) as client:
        upload_response = client.post(
            "/api/documents/upload",
            files=[
                (
                    "files",
                    (
                        "synthetic-invoice.csv",
                        b"invoice_number,total\nSYN-MIGRATION-001,125.00\n",
                        "text/csv",
                    ),
                ),
            ],
        )

        assert upload_response.status_code == 202
        document = upload_response.json()["documents"][0]
        assert document["status"] == "queued"
        assert document["progress_percent"] == 0

        listing_response = client.get("/api/documents")
        assert listing_response.status_code == 200
        assert listing_response.json()["total"] == 1
        assert listing_response.json()["items"][0]["id"] == document["id"]

        detail_response = client.get(f"/api/documents/{document['id']}")
        assert detail_response.status_code == 200
        assert detail_response.json()["id"] == document["id"]

        with application.state.session_factory() as session:
            job = session.execute(
                text(
                    "SELECT status, attempt_count FROM processing_jobs "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": UUID(document["id"]).hex},
            ).mappings().one()

            assert job["status"] == "queued"
            assert job["attempt_count"] == 0