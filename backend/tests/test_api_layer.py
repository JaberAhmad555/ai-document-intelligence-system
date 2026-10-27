from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.database import Base
from app.core.http import upload_request_limit_bytes
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}",
        local_storage_path=tmp_path / "uploads",
        max_upload_files=2,
        max_file_size_mb=1,
        max_upload_total_mb=1,
        upload_chunk_size_bytes=64 * 1024,
        cors_origins=["http://localhost:5173"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    application = create_app(settings)

    # The context manager runs application startup and shutdown.
    with TestClient(application) as test_client:
        # Only isolated tests use create_all.
        # The deployed and local application must use Alembic.
        Base.metadata.create_all(application.state.database_engine)
        yield test_client


def upload_csv(
    client: TestClient,
    filename: str = "invoice.csv",
):
    return client.post(
        "/api/documents/upload",
        files=[
            (
                "files",
                (
                    filename,
                    b"invoice_number,total\nSYN-001,125.00\n",
                    "text/csv",
                ),
            )
        ],
    )


def assert_empty_document_list(client: TestClient) -> None:
    response = client.get("/api/documents")
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_health_reports_api_liveness(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["environment"] == "test"
    assert response.json()["check"] == "api_liveness"


def test_health_does_not_require_database_tables(settings):
    application = create_app(settings)

    with TestClient(application) as test_client:
        assert test_client.get("/api/health").status_code == 200

        response = test_client.get("/api/documents")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"


def test_upload_and_get_document_preserve_queued_state(client):
    response = upload_csv(client)

    assert response.status_code == 202
    payload = response.json()
    assert payload["count"] == 1

    document = payload["documents"][0]
    assert document["status"] == "queued"
    assert document["processing_stage"] == "queued"
    assert document["progress_percent"] == 0
    assert document["processed_units"] == 0
    assert document["total_units"] is None
    assert document["processing_started_at"] is None
    assert document["processing_finished_at"] is None
    assert "storage_key" not in document
    assert "storage_backend" not in document

    detail = client.get(f"/api/documents/{document['id']}")
    assert detail.status_code == 200
    assert detail.json() == document


def test_multiple_uploads_and_pagination(client):
    response = client.post(
        "/api/documents/upload",
        files=[
            ("files", ("first.csv", b"id,total\nA,1\n", "text/csv")),
            ("files", ("second.csv", b"id,total\nB,2\n", "text/csv")),
        ],
    )

    assert response.status_code == 202
    assert response.json()["count"] == 2

    first_page = client.get(
        "/api/documents",
        params={"limit": 1, "offset": 0},
    ).json()
    second_page = client.get(
        "/api/documents",
        params={"limit": 1, "offset": 1},
    ).json()

    assert first_page["total"] == 2
    assert first_page["limit"] == 1
    assert first_page["offset"] == 0
    assert first_page["has_more"] is True
    assert second_page["has_more"] is False
    assert first_page["items"][0]["id"] != second_page["items"][0]["id"]


def test_document_not_found_returns_structured_error(client):
    response = client.get(f"/api/documents/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


@pytest.mark.parametrize(
    "url",
    [
        "/api/documents/not-a-uuid",
        "/api/documents?limit=0",
        "/api/documents?limit=101",
        "/api/documents?offset=-1",
    ],
)
def test_invalid_parameters_return_structured_errors(client, url):
    response = client.get(url)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "request_validation_failed"
    assert error["details"]["errors"]


def test_invalid_file_rejects_batch_and_cleans_storage(client, settings):
    response = client.post(
        "/api/documents/upload",
        files=[
            ("files", ("valid.csv", b"id,total\nA,1\n", "text/csv")),
            ("files", ("broken.pdf", b"not a PDF", "application/pdf")),
        ],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "file_type_mismatch"
    assert response.json()["error"]["details"]["file_index"] == 1
    assert list(settings.local_storage_path.glob("*")) == []
    assert_empty_document_list(client)


def test_parser_enforces_file_count_limit(client, settings):
    response = client.post(
        "/api/documents/upload",
        files=[
            (
                "files",
                (f"{number}.csv", b"id,total\nA,1\n", "text/csv"),
            )
            for number in range(settings.max_upload_files + 1)
        ],
    )

    # Starlette reports multipart parser limits as HTTP 400.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "http_400"
    assert_empty_document_list(client)


def test_unknown_file_field_is_rejected(client):
    response = client.post(
        "/api/documents/upload",
        files=[
            ("attachment", ("invoice.csv", b"id,total\nA,1\n", "text/csv")),
        ],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_upload_field"
    assert_empty_document_list(client)


def test_non_multipart_body_is_rejected(client):
    response = client.post(
        "/api/documents/upload",
        json={"files": []},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "multipart_required"


def test_declared_oversized_body_is_rejected_before_parsing(client, settings):
    response = client.post(
        "/api/documents/upload",
        content=b"not a multipart body",
        headers={
            "Content-Type": "multipart/form-data; boundary=synthetic",
            "Content-Length": str(upload_request_limit_bytes(settings) + 1),
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert_empty_document_list(client)


def test_oversized_body_without_content_length_is_rejected(client, settings):
    chunk = b"x" * (64 * 1024)
    number_of_chunks = upload_request_limit_bytes(settings) // len(chunk) + 1

    response = client.post(
        "/api/documents/upload",
        content=(chunk for _ in range(number_of_chunks)),
        headers={
            "Content-Type": "multipart/form-data; boundary=synthetic",
        },
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert_empty_document_list(client)


def test_per_file_limit_returns_413_and_cleans_storage(client, settings):
    response = client.post(
        "/api/documents/upload",
        files=[
            (
                "files",
                (
                    "large.csv",
                    b"x" * (settings.max_file_size_bytes + 1),
                    "text/csv",
                ),
            ),
        ],
    )

    # Multipart overhead is allowed by the request guard, while the
    # existing storage service independently enforces exact file limits.
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert list(settings.local_storage_path.glob("*")) == []
    assert_empty_document_list(client)


def test_allowed_origin_receives_cors_headers(client):
    response = client.options(
        "/api/documents/upload",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "http://localhost:5173"
    )


def test_unapproved_origin_does_not_receive_cors_permission(client):
    response = client.options(
        "/api/documents/upload",
        headers={
            "Origin": "https://unapproved.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_request_limit_error_includes_cors_headers(client, settings):
    response = client.post(
        "/api/documents/upload",
        content=b"x",
        headers={
            "Origin": "http://localhost:5173",
            "Content-Type": "multipart/form-data; boundary=synthetic",
            "Content-Length": str(upload_request_limit_bytes(settings) + 1),
        },
    )

    assert response.status_code == 413
    assert (
        response.headers["access-control-allow-origin"]
        == "http://localhost:5173"
    )


def test_openapi_documents_multiple_file_upload(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    operation = response.json()["paths"]["/api/documents/upload"]["post"]
    schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]

    assert schema["required"] == ["files"]
    assert schema["properties"]["files"]["type"] == "array"
    assert schema["properties"]["files"]["items"]["format"] == "binary"