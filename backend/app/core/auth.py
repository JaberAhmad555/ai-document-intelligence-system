"""Fail-closed demo authentication, before request buffering or route execution.

This is a shared bearer credential, not a user/role or verified-identity system.
The token is read exclusively from backend configuration, never from a URL.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.http import error_response

TOKEN_PATTERN = re.compile(rb"[A-Za-z0-9_-]{32,256}")
PUBLIC_READ_PATHS = frozenset({"/api/health", "/docs", "/redoc", "/openapi.json"})


def is_public_read(path: str, method: str) -> bool:
    return method in {"GET", "HEAD"} and path.rstrip("/") in PUBLIC_READ_PATHS


class DocumentAccess:
    """Per-app access policy. Initialization must succeed during ASGI startup."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._digest: bytes | None = None
        self.initialized = False
        self.disabled = False

    def initialize(self) -> None:
        self.initialized = False
        self.disabled = False
        self._digest = None
        settings = self._settings
        production = settings.app_env == "production" or settings.render
        if production and settings.document_auth_disabled:
            raise RuntimeError("DOCUMENT_AUTH_DISABLED must be false in production and on Render.")
        secret = settings.document_api_token
        if secret is None:
            if not production and (settings.app_env == "test" or (
                settings.app_env == "development" and settings.document_auth_disabled
            )):
                self.disabled = True
                self.initialized = True
                return
            raise RuntimeError("DOCUMENT_API_TOKEN is required. Configure it in the backend environment before starting the API.")
        # Validate without putting the input in any exception/log message.
        raw = secret.get_secret_value()
        try:
            encoded = raw.encode("ascii")
        except UnicodeEncodeError:
            raise RuntimeError("DOCUMENT_API_TOKEN must be 32–256 URL-safe ASCII characters (letters, digits, underscore, hyphen).") from None
        if TOKEN_PATTERN.fullmatch(encoded) is None:
            raise RuntimeError("DOCUMENT_API_TOKEN must be 32–256 URL-safe ASCII characters (letters, digits, underscore, hyphen).")
        self._digest = hashlib.sha256(encoded).digest()
        self.initialized = True

    def accepts(self, headers: list[tuple[bytes, bytes]]) -> bool:
        if not self.initialized:
            return False
        if self.disabled:
            return True
        values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(values) != 1:
            return False
        value = values[0]
        if len(value) > 263:
            return False
        scheme, separator, supplied = value.partition(b" ")
        if scheme.lower() != b"bearer" or not separator or TOKEN_PATTERN.fullmatch(supplied) is None:
            return False
        # Always compare fixed-length digests using constant-time comparison.
        actual_digest = hashlib.sha256(supplied).digest()
        return self._digest is not None and hmac.compare_digest(actual_digest, self._digest)

    def close(self) -> None:
        self._digest = None
        self.initialized = False
        self.disabled = False


class DocumentAuthMiddleware:
    """Protect every non-public HTTP route, including future document endpoints.

    CORSMiddleware must be outside this guard. It handles genuine OPTIONS
    preflights; arbitrary OPTIONS requests do not get an authentication bypass.
    The guard does not read unauthenticated bodies or call the downstream app.
    """

    def __init__(self, app: ASGIApp, access: DocumentAccess) -> None:
        self.app = app
        self.access = access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # No WebSocket API exists. Deny future WS paths unless a dedicated
            # authenticated protocol is implemented; lifespan must still run.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if is_public_read(scope.get("path", ""), scope.get("method", "")):
            await self.app(scope, receive, send)
            return

        async def protected_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                existing = headers.get("Vary", "")
                if "authorization" not in {item.strip().lower() for item in existing.split(",")}:
                    headers["Vary"] = (existing + ", " if existing else "") + "Authorization"
            await send(message)

        if not self.access.initialized:
            response = error_response(status_code=503, code="authentication_unavailable",
                message="API authentication has not been initialized.")
            await response(scope, receive, protected_send)
            return
        if not self.access.accepts(scope.get("headers", [])):
            response = error_response(status_code=401, code="authentication_required",
                message="Demo access is required or no longer valid.",
                headers={"WWW-Authenticate": 'Bearer realm="document-demo"'})
            await response(scope, receive, protected_send)
            return
        await self.app(scope, receive, protected_send)
