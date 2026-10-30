"""Authentication integration tests; synthetic credentials and test LLM only."""
import asyncio
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from app.core import auth as auth_module
from app.core.auth import DocumentAccess, DocumentAuthMiddleware
from app.core.config import Settings
from app.main import create_app
from app.models.extraction import InvoiceCorrection
from app.models.ingestion import Document, ProcessingJob
from app.workers.jobs import ProcessingError
from app.workers.runtime import RunOutcome
from test_invoice_pipeline import ControlledProvider, environment, invoice_response, processor, upload, worker

# Deliberately synthetic. Never use this value in a deployed environment.
TEST_TOKEN = "test-only-" + "x" * 34
WRONG_TOKEN = "test-only-" + "y" * 34
AUTH = {"Authorization": "Bearer " + TEST_TOKEN}
ORIGIN = "https://review.example.test"


@pytest.fixture
def secured(environment):
    settings = environment["settings"].model_copy(update={
        "app_env": "production", "document_api_token": SecretStr(TEST_TOKEN),
        "document_auth_disabled": False, "render": False, "cors_origins": [ORIGIN],
    })
    application = create_app(settings)
    with TestClient(application) as client:
        yield environment, client, application


def counts(env):
    with env["factory"]() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in
                     (Document, ProcessingJob, InvoiceCorrection))


@pytest.mark.parametrize("app_env,disabled,render", [
    ("production", False, False), ("production", True, False),
    ("development", False, False), ("development", True, True),
    ("test", False, True), ("production", False, True),
])
def test_missing_token_fails_startup(app_env, disabled, render, tmp_path):
    settings = Settings(_env_file=None, app_env=app_env, document_api_token=None,
        document_auth_disabled=disabled, render=render,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'unused.db').as_posix()}")
    with pytest.raises(RuntimeError, match="DOCUMENT_"):
        with TestClient(create_app(settings)):
            pytest.fail("Startup must not succeed")
    assert not (tmp_path / "unused.db").exists()


def test_production_rejects_disable_flag_even_with_token():
    settings = Settings(_env_file=None, app_env="production", document_api_token=TEST_TOKEN, document_auth_disabled=True)
    with pytest.raises(RuntimeError, match="DOCUMENT_AUTH_DISABLED"):
        with TestClient(create_app(settings)):
            pytest.fail("Production bypass must be rejected")


@pytest.mark.parametrize("token", ["", "short", " " * 43, "x" * 257, "x" * 32 + "\n", "é" * 43])
def test_malformed_configured_tokens_fail_without_exposing_input(token):
    policy = DocumentAccess(Settings(_env_file=None, app_env="production", document_api_token=token))
    with pytest.raises(RuntimeError) as error:
        policy.initialize()
    if token:
        assert token not in str(error.value)
    assert not policy.initialized


@pytest.mark.parametrize("app_env,disabled", [("test", False), ("development", True)])
def test_explicit_local_test_modes_remain_available(app_env, disabled, tmp_path):
    settings = Settings(_env_file=None, app_env=app_env, document_api_token=None,
        document_auth_disabled=disabled, render=False,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'local.db').as_posix()}")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        # A bypass must not pretend that the frontend password was authenticated.
        response = client.get("/api/auth/check")
        assert response.status_code == 200 and response.json() == {"authenticated": False}


def test_environment_secret_is_masked_and_excluded(monkeypatch):
    monkeypatch.setenv("DOCUMENT_API_TOKEN", TEST_TOKEN)
    settings = Settings(_env_file=None, app_env="production")
    assert settings.document_api_token.get_secret_value() == TEST_TOKEN
    assert TEST_TOKEN not in repr(settings)
    assert "document_api_token" not in settings.model_dump()
    assert TEST_TOKEN not in settings.model_dump_json()


def test_configured_test_token_is_enforced_and_comparison_is_constant_time(monkeypatch):
    policy = DocumentAccess(Settings(_env_file=None, app_env="test", document_api_token=TEST_TOKEN))
    policy.initialize()
    original_compare = auth_module.hmac.compare_digest
    comparison = Mock(side_effect=original_compare)
    monkeypatch.setattr(auth_module.hmac, "compare_digest", comparison)
    assert not policy.accepts([])
    assert policy.accepts([(b"authorization", ("Bearer " + TEST_TOKEN).encode())])
    assert not policy.accepts([(b"authorization", ("Bearer " + WRONG_TOKEN).encode())])
    assert comparison.call_count == 2
    for call in comparison.call_args_list:
        assert len(call.args[0]) == len(call.args[1]) == 32
    policy.close()
    assert not policy.accepts([(b"authorization", ("Bearer " + TEST_TOKEN).encode())])


def test_health_public_and_access_check_has_no_database_dependency(secured, caplog):
    env, client, _ = secured
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Authorization": "Bearer " + WRONG_TOKEN}).status_code == 200
    assert client.get("/api/auth/check").status_code == 401
    assert client.get("/api/auth/check", headers=AUTH).json() == {"authenticated": True}
    assert counts(env) == (0, 0, 0)
    assert TEST_TOKEN not in caplog.text and WRONG_TOKEN not in caplog.text


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/documents"), ("GET", "/api/documents?q=synthetic"),
    ("POST", "/api/documents/upload"), ("GET", "/api/documents/{id}"),
    ("GET", "/api/documents/{id}/invoice"), ("GET", "/api/documents/{id}/validation"),
    ("GET", "/api/documents/{id}/content"), ("GET", "/api/documents/{id}/corrections"),
    ("GET", "/api/documents/{id}/corrections/1"), ("POST", "/api/documents/{id}/corrections"),
    ("POST", "/api/documents/{id}/reprocess"), ("GET", "/api/auth/check"),
    ("POST", "/api/documents/upload/"), ("GET", "/api/documents/not-a-uuid/content"),
])
def test_every_document_api_rejects_missing_and_invalid_auth(secured, method, path):
    env, client, _ = secured
    path = path.replace("{id}", str(uuid4()))
    for supplied in ({}, {"Authorization": "Bearer " + WRONG_TOKEN}):
        response = client.request(method, path, headers={**supplied, "Origin": ORIGIN})
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "authentication_required"
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert response.headers["cache-control"] == "no-store"
        assert "authorization" in response.headers["vary"].lower()
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert TEST_TOKEN not in response.text and WRONG_TOKEN not in response.text
    assert counts(env) == (0, 0, 0)


def test_cors_preflight_does_not_require_auth_or_create_work(secured):
    env, client, _ = secured
    for path in ("/api/documents/upload", f"/api/documents/{uuid4()}/corrections", f"/api/documents/{uuid4()}/reprocess"):
        response = client.options(path, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert client.options("/api/documents").status_code == 401
    rejected = client.options("/api/documents", headers={"Origin": "https://untrusted.example.test", "Access-Control-Request-Method": "GET"})
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
    assert counts(env) == (0, 0, 0)


def test_duplicate_malformed_and_query_credentials_are_rejected(secured):
    _, client, _ = secured
    for value in ("Basic " + TEST_TOKEN, "Bearer", "Bearer ", "Bearer  " + TEST_TOKEN, "Bearer " + TEST_TOKEN + "," + TEST_TOKEN, "Bearer " + "x" * 300):
        assert client.get("/api/documents", headers={"Authorization": value}).status_code == 401
    assert client.get("/api/documents", headers=[("Authorization", "Bearer " + TEST_TOKEN), ("Authorization", "Bearer " + TEST_TOKEN)]).status_code == 401
    assert client.get("/api/documents?token=" + TEST_TOKEN).status_code == 401
    assert client.get("/api/documents", headers={"Authorization": "bearer " + TEST_TOKEN}).status_code == 200


def test_auth_runs_before_upload_spooling_or_request_validation(secured, monkeypatch):
    _, client, _ = secured
    def no_spool(*args, **kwargs):
        pytest.fail("An unauthorized upload must not allocate a temporary spool")
    monkeypatch.setattr("app.core.http.tempfile.SpooledTemporaryFile", no_spool)
    response = client.post("/api/documents/upload", content=b"not multipart",
        headers={"Content-Type": "text/plain", "Content-Length": "999999999"})
    assert response.status_code == 401
    assert client.post(f"/api/documents/{uuid4()}/corrections", content=b"not json").status_code == 401


def test_guard_does_not_read_unauthorized_bodies_and_covers_future_paths():
    policy = DocumentAccess(Settings(_env_file=None, app_env="production", document_api_token=TEST_TOKEN))
    policy.initialize()
    messages = []
    async def forbidden(*args):
        pytest.fail("Unauthorized request reached body reading or downstream code")
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "method": "POST", "path": "/future-document-mutation", "headers": []}
    asyncio.run(DocumentAuthMiddleware(forbidden, policy)(scope, forbidden, send))
    assert messages[0]["status"] == 401


def test_unauthorized_upload_cannot_queue_gemini_work(secured):
    env, client, _ = secured
    response = client.post("/api/documents/upload", files=[("files", ("synthetic.csv", b"invoice,total\nSYN,10\n", "text/csv"))])
    assert response.status_code == 401
    assert counts(env) == (0, 0, 0)
    assert not list(env["settings"].local_storage_path.glob("*"))
    provider = ControlledProvider()
    assert worker(env, processor(provider)).run_once() == RunOutcome.IDLE
    assert provider.calls == 0


def test_unauthorized_correction_preserves_invoice_and_audit(secured):
    env, client, _ = secured
    doc = upload(env)
    assert worker(env, processor(ControlledProvider())).run_once() == RunOutcome.SUCCEEDED
    original = invoice_response(env, doc)
    body = {"request_id": str(uuid4()), "extraction_id": original["extraction_id"], "expected_revision": 0,
        "reviewer_label": "Synthetic reviewer", "reason": "Synthetic security test", "changes": {"total": "111.00"}}
    before = counts(env)
    assert client.post(f"/api/documents/{doc}/corrections", json=body).status_code == 401
    assert counts(env) == before
    current = client.get(f"/api/documents/{doc}/invoice", headers=AUTH).json()
    assert current["correction_revision"] == 0
    assert current["result"]["invoice"] == original["result"]["invoice"]
    assert client.post(f"/api/documents/{doc}/corrections", json=body, headers=AUTH).status_code == 200
    assert client.get(f"/api/documents/{doc}/invoice", headers=AUTH).json()["correction_revision"] == 1


def test_unauthorized_retry_cannot_trigger_gemini_and_authorized_retry_works(secured):
    env, client, _ = secured
    doc = upload(env)
    claim = env["store"].claim_next("security-test-worker")
    env["store"].fail(claim, ProcessingError("synthetic_failure", "Synthetic failure.", retryable=False))
    body = {"request_id": str(uuid4()), "failed_job_id": str(claim.job_id)}
    assert client.post(f"/api/documents/{doc}/reprocess", json=body, headers={"Authorization": "Bearer " + WRONG_TOKEN}).status_code == 401
    assert counts(env) == (1, 1, 0)
    provider = ControlledProvider()
    runtime = worker(env, processor(provider))
    assert runtime.run_once() == RunOutcome.IDLE and provider.calls == 0
    assert client.post(f"/api/documents/{doc}/reprocess", json=body, headers=AUTH).status_code == 202
    assert runtime.run_once() == RunOutcome.SUCCEEDED and provider.calls == 1
    response = client.get(f"/api/documents/{doc}/invoice", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_authorized_upload_still_obeys_existing_errors_and_queue_contract(secured):
    env, client, _ = secured
    bad = client.post("/api/documents/upload", files=[("files", ("bad.exe", b"bad", "application/octet-stream"))], headers=AUTH)
    assert bad.status_code == 415
    assert bad.headers["cache-control"] == "no-store"
    good = client.post("/api/documents/upload", files=[("files", ("invoice.csv", b"invoice,total\nSYN,10\n", "text/csv"))], headers=AUTH)
    assert good.status_code == 202, good.text
    assert good.json()["documents"][0]["status"] == "queued"
    assert counts(env) == (1, 1, 0)


def test_openapi_marks_document_operations_as_protected(secured):
    _, client, _ = secured
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["DocumentToken"]["scheme"] == "bearer"
    assert not schema["paths"]["/api/health"]["get"].get("security")
    for path, methods in schema["paths"].items():
        if path.startswith("/api/documents") or path == "/api/auth/check":
            for method, operation in methods.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    assert operation["security"] == [{"DocumentToken": []}]
    assert TEST_TOKEN not in str(schema)


def test_token_rotation_rejects_previous_credential(environment):
    settings = environment["settings"].model_copy(update={"app_env": "production", "document_api_token": SecretStr(WRONG_TOKEN)})
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/documents", headers=AUTH).status_code == 401
        assert client.get("/api/auth/check", headers={"Authorization": "Bearer " + WRONG_TOKEN}).status_code == 200
