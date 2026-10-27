from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core import config as config_module
from app.core.config import BACKEND_DIR, Settings, get_settings
from app.core.database import Base, build_engine, get_database_url
from app.core.errors import ApplicationError
from app.main import create_app
from app.models.ingestion import Document, ProcessingJob
from app.services import file_validation as validation_module
from app.services import storage as storage_module
from app.services import uploads as uploads_module
from app.services.storage import LocalStorage
from app.services.uploads import upload_documents


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch):
    """Prevent shell settings from changing these tests' expectations."""
    setting_names = {
        field_name.casefold()
        for field_name in Settings.model_fields
    }

    for environment_name in list(os.environ):
        if environment_name.casefold() in setting_names:
            monkeypatch.delenv(environment_name, raising=False)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=(
            f"sqlite+pysqlite:///{(tmp_path / 'failure-tests.db').as_posix()}"
        ),
        local_storage_path=tmp_path / "uploads",
        max_upload_files=2,
        max_file_size_mb=1,
        max_upload_total_mb=2,
        upload_chunk_size_bytes=64 * 1024,
        cors_origins=[],
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Session]:
    engine = build_engine(settings)

    # This is an isolated service-test database.
    # Application schema creation continues to use Alembic.
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def upload_factory():
    uploads: list[UploadFile] = []

    def make(filename: str, content: bytes) -> UploadFile:
        upload = UploadFile(
            filename=filename,
            file=io.BytesIO(content),
        )
        uploads.append(upload)
        return upload

    yield make

    for upload in uploads:
        upload.file.close()


def stored_paths(settings: Settings) -> list[Path]:
    return list(settings.local_storage_path.glob("*"))


def assert_no_records(database: Session) -> None:
    assert database.scalar(
        select(func.count()).select_from(Document)
    ) == 0
    assert database.scalar(
        select(func.count()).select_from(ProcessingJob)
    ) == 0


def test_environment_variables_are_loaded(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Synthetic Test API")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MAX_UPLOAD_FILES", "4")
    monkeypatch.setenv(
        "CORS_ORIGINS",
        json.dumps(["https://dashboard.example.test"]),
    )

    settings = Settings(_env_file=None)

    assert settings.app_name == "Synthetic Test API"
    assert settings.app_env == "test"
    assert settings.max_upload_files == 4
    assert settings.cors_origins == ["https://dashboard.example.test"]


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    dotenv_path = tmp_path / "test.env"
    dotenv_path.write_text(
        "APP_NAME=Name From File\n"
        "APP_ENV=test\n"
        "MAX_UPLOAD_FILES=2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_NAME", "Name From Environment")

    settings = Settings(_env_file=dotenv_path)

    assert settings.app_name == "Name From Environment"
    assert settings.max_upload_files == 2


def test_explicit_settings_override_environment(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Environment Name")

    settings = Settings(
        _env_file=None,
        app_name="Explicit Name",
    )

    assert settings.app_name == "Explicit Name"


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_name": ""},
        {"app_env": "unknown"},
        {"log_level": "VERBOSE"},
        {"database_url": ""},
        {"database_url": "mysql://localhost/example"},
        {"storage_backend": "s3"},
        {"max_upload_files": 0},
        {"max_file_size_mb": 0},
        {"max_file_size_mb": 2, "max_upload_total_mb": 1},
        {
            "max_file_size_mb": 1,
            "max_upload_total_mb": 1,
            "upload_chunk_size_bytes": 2 * 1024 * 1024,
        },
        {"max_archive_entries": 0},
        {"max_archive_compression_ratio": 0},
        {"max_image_pixels": 0},
        {"job_max_attempts": 0},
        {"job_lease_seconds": 1},
        {"port": 0},
        {"port": 65536},
        {"app_env": "production", "debug": True},
    ],
)
def test_invalid_settings_are_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "example.test",
        "ftp://example.test",
        "https://user:password@example.test",
        "https://example.test/private",
        "https://example.test?token=synthetic",
        "https://example.test#fragment",
        "https://example.test:invalid",
    ],
)
def test_unsafe_or_invalid_cors_origins_are_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            cors_origins=[origin],
        )


def test_cors_origins_are_normalized_and_deduplicated():
    settings = Settings(
        _env_file=None,
        cors_origins=[
            "https://dashboard.example.test/",
            "https://dashboard.example.test",
        ],
    )

    assert settings.cors_origins == ["https://dashboard.example.test"]


def test_empty_cors_allowlist_is_supported():
    settings = Settings(_env_file=None, cors_origins=[])

    assert settings.cors_origins == []


def test_relative_storage_path_is_anchored_to_backend():
    settings = Settings(
        _env_file=None,
        local_storage_path=Path("data") / "synthetic-storage",
    )

    assert settings.local_storage_path == (
        BACKEND_DIR / "data" / "synthetic-storage"
    ).resolve()


def test_relative_sqlite_path_is_anchored_to_backend():
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///./data/synthetic.db",
    )

    url = get_database_url(settings)

    assert url.get_backend_name() == "sqlite"
    assert Path(url.database) == (
        BACKEND_DIR / "data" / "synthetic.db"
    ).resolve()


def test_absolute_sqlite_path_is_preserved(tmp_path):
    database_path = (tmp_path / "absolute.db").resolve()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
    )

    url = get_database_url(settings)

    assert Path(url.database) == database_path


def test_in_memory_sqlite_url_is_preserved():
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
    )

    assert get_database_url(settings).database == ":memory:"


@pytest.mark.parametrize(
    "prefix",
    [
        "postgres",
        "postgresql",
        "postgresql+psycopg",
    ],
)
def test_postgresql_urls_use_psycopg(prefix):
    settings = Settings(
        _env_file=None,
        database_url=(
            f"{prefix}://test_user:synthetic%25password"
            "@localhost/example_db"
        ),
    )

    url = get_database_url(settings)

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "test_user"
    assert url.password == "synthetic%password"
    assert url.database == "example_db"


def test_settings_repr_does_not_expose_database_password():
    password = "synthetic-only-password"
    settings = Settings(
        _env_file=None,
        database_url=(
            f"postgresql+psycopg://test_user:{password}"
            "@localhost/example_db"
        ),
    )

    assert password not in repr(settings)
    assert "database_url=" not in repr(settings)


def test_settings_cache_requires_explicit_refresh(monkeypatch):
    # Prevent this cache test from reading the real backend/.env.
    monkeypatch.setattr(
        config_module,
        "Settings",
        lambda: Settings(_env_file=None),
    )

    monkeypatch.setenv("APP_NAME", "First Name")
    first = get_settings()

    monkeypatch.setenv("APP_NAME", "Second Name")
    assert get_settings() is first
    assert get_settings().app_name == "First Name"

    get_settings.cache_clear()

    assert get_settings().app_name == "Second Name"


def test_development_debug_does_not_enable_traceback_pages():
    application = create_app(
        Settings(
            _env_file=None,
            app_env="development",
            debug=True,
        )
    )

    assert application.debug is False


class FailingReadStream(io.BytesIO):
    """Supply one chunk, then simulate failure while reading an upload."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self.read_calls > 1:
            raise OSError("Synthetic stream failure with private details")
        return super().read(size)


def test_stream_read_failure_removes_partial_file(settings, database):
    with FailingReadStream(b"id,total\nSYN-001,125\n") as stream:
        upload = UploadFile(
            filename="synthetic.csv",
            file=stream,
        )

        with pytest.raises(ApplicationError) as caught:
            upload_documents(
                uploads=[upload],
                database=database,
                settings=settings,
            )

    assert caught.value.status_code == 503
    assert caught.value.code == "storage_unavailable"
    assert "private details" not in caught.value.message
    assert stored_paths(settings) == []
    assert_no_records(database)


def test_fsync_failure_removes_partial_file(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    def fail_fsync(file_descriptor):
        raise OSError("Synthetic disk failure")

    monkeypatch.setattr(storage_module.os, "fsync", fail_fsync)

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory("synthetic.csv", b"id,total\nSYN-001,125\n")
            ],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "storage_unavailable"
    assert stored_paths(settings) == []
    assert_no_records(database)


def test_flush_failure_cleans_files_before_commit(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    def fail_flush(*args, **kwargs):
        raise IntegrityError(
            statement=None,
            params=None,
            orig=RuntimeError("Synthetic constraint failure"),
        )

    monkeypatch.setattr(database, "flush", fail_flush)

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory("synthetic.csv", b"id,total\nSYN-001,125\n")
            ],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "database_unavailable"
    assert stored_paths(settings) == []
    assert_no_records(database)


def test_unknown_commit_outcome_retains_file_for_recovery(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    def fail_commit():
        raise OperationalError(
            statement=None,
            params=None,
            orig=RuntimeError("Synthetic lost commit acknowledgement"),
        )

    def fail_commit_probe(*args, **kwargs):
        raise OperationalError(
            statement=None,
            params=None,
            orig=RuntimeError("Synthetic database unavailable"),
        )

    monkeypatch.setattr(database, "commit", fail_commit)
    monkeypatch.setattr(
        uploads_module,
        "_find_committed_documents",
        fail_commit_probe,
    )

    payload = b"id,total\nSYN-001,125\n"

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[upload_factory("synthetic.csv", payload)],
            database=database,
            settings=settings,
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "upload_outcome_unknown"
    assert caught.value.details["files_retained_for_recovery"] is True

    retained_files = stored_paths(settings)
    assert len(retained_files) == 1
    assert retained_files[0].read_bytes() == payload

    # This injected failure rolls back, but the upload service could not
    # know that because its independent verification was also unavailable.
    assert_no_records(database)


def test_cleanup_failure_preserves_original_validation_error(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    storage = LocalStorage(settings)

    def fail_delete(storage_key):
        raise ApplicationError(
            status_code=503,
            code="storage_cleanup_failed",
            message="Synthetic cleanup failure.",
        )

    monkeypatch.setattr(storage, "delete", fail_delete)

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory("valid.csv", b"id,total\nSYN-001,125\n"),
                upload_factory("invalid.pdf", b"not a PDF"),
            ],
            database=database,
            settings=settings,
            storage=storage,
        )

    assert caught.value.code == "file_type_mismatch"
    assert caught.value.details["cleanup_pending"] is True
    assert len(stored_paths(settings)) == 2
    assert_no_records(database)


def test_validation_memory_failure_is_explicit_and_cleans_storage(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    def fail_validation(path):
        raise MemoryError("Synthetic parser memory failure")

    monkeypatch.setattr(
        validation_module,
        "_validate_csv",
        fail_validation,
    )

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory("synthetic.csv", b"id,total\nSYN-001,125\n")
            ],
            database=database,
            settings=settings,
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "validation_resource_limit"
    assert stored_paths(settings) == []
    assert_no_records(database)


def test_upload_service_does_not_take_over_existing_transaction(
    settings,
    database,
    upload_factory,
):
    database.begin()

    with pytest.raises(RuntimeError, match="active transaction"):
        upload_documents(
            uploads=[
                upload_factory("synthetic.csv", b"id,total\nSYN-001,125\n")
            ],
            database=database,
            settings=settings,
        )

    assert database.in_transaction()
    assert stored_paths(settings) == []
    database.rollback()


def test_content_length_mismatch_is_rejected(settings):
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.post(
            "/api/documents/upload",
            content=b"abc",
            headers={
                "Content-Type": "multipart/form-data; boundary=synthetic",
                "Content-Length": "2",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "content_length_mismatch"
    assert stored_paths(settings) == []


def test_compressed_http_upload_body_is_rejected(settings):
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.post(
            "/api/documents/upload",
            content=b"synthetic",
            headers={
                "Content-Type": "multipart/form-data; boundary=synthetic",
                "Content-Encoding": "gzip",
            },
        )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_content_encoding"
    assert stored_paths(settings) == []