from __future__ import annotations

import logging
import tempfile
from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import ApplicationError


logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/documents/upload"
MULTIPART_OVERHEAD_PER_FILE = 64 * 1024
MULTIPART_BASE_OVERHEAD = 64 * 1024
REPLAY_CHUNK_SIZE = 64 * 1024


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
        headers=headers,
    )


async def application_error_handler(
    request: Request,
    error: ApplicationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": error.as_dict()},
    )


async def request_validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    # Do not expose Pydantic's raw input or exception context.
    problems = [
        {
            "location": list(problem["loc"]),
            "type": problem["type"],
        }
        for problem in error.errors()
    ]

    return error_response(
        status_code=422,
        code="request_validation_failed",
        message="One or more request parameters are invalid.",
        details={"errors": problems},
    )


async def http_error_handler(
    request: Request,
    error: StarletteHTTPException,
) -> JSONResponse:
    try:
        description = HTTPStatus(error.status_code).phrase
    except ValueError:
        description = "HTTP request failed"

    # Parser errors can contain untrusted strings. Return a stable public
    # description rather than forwarding arbitrary exception details.
    if error.status_code == 400:
        description = "The request is malformed or exceeds multipart limits."

    return error_response(
        status_code=error.status_code,
        code=f"http_{error.status_code}",
        message=description,
        headers=error.headers,
    )


async def database_error_handler(
    request: Request,
    error: SQLAlchemyError,
) -> JSONResponse:
    logger.error(
        "Database request failed.",
        exc_info=(type(error), error, error.__traceback__),
    )
    return error_response(
        status_code=503,
        code="database_unavailable",
        message="The database is unavailable or has not been initialized.",
    )


async def unexpected_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    logger.error(
        "Unhandled API exception.",
        exc_info=(type(error), error, error.__traceback__),
    )
    return error_response(
        status_code=500,
        code="internal_server_error",
        message="The request could not be completed.",
    )


def upload_request_limit_bytes(settings: Settings) -> int:
    """Allow bounded multipart headers in addition to file-content limits."""
    return (
        settings.max_upload_total_bytes
        + settings.max_upload_files * MULTIPART_OVERHEAD_PER_FILE
        + MULTIPART_BASE_OVERHEAD
    )


class UploadBodyLimitMiddleware:
    """Bound upload requests before Starlette parses multipart content.

    The body is copied into a bounded temporary spool, then replayed to
    Starlette. Small bodies stay in memory; larger bodies use temporary
    disk storage. This temporary spool is never permanent document storage.

    Declared lengths are checked immediately. Actual received bytes are
    always counted, including requests without Content-Length.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.max_body_bytes = upload_request_limit_bytes(settings)
        self.spool_memory_bytes = min(
            settings.upload_chunk_size_bytes,
            1024 * 1024,
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path", "").rstrip("/") != UPLOAD_PATH
        ):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])

        lengths = [
            value
            for name, value in headers
            if name.lower() == b"content-length"
        ]
        declared_length: int | None = None

        if lengths:
            raw_length = lengths[0].strip()

            if (
                len(lengths) != 1
                or not raw_length
                or len(raw_length) > 20
                or not raw_length.isdigit()
            ):
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be a single nonnegative integer.",
                )
                return

            declared_length = int(raw_length)

            if declared_length > self.max_body_bytes:
                await self._reject_too_large(scope, receive, send)
                return

        encodings = [
            value.strip().lower()
            for name, value in headers
            if name.lower() == b"content-encoding"
        ]
        if encodings and encodings != [b"identity"]:
            await self._reject(
                scope,
                receive,
                send,
                status_code=415,
                code="unsupported_content_encoding",
                message="Compressed HTTP upload bodies are not supported.",
            )
            return

        content_types = [
            value.split(b";", 1)[0].strip().lower()
            for name, value in headers
            if name.lower() == b"content-type"
        ]
        if content_types != [b"multipart/form-data"]:
            await self._reject(
                scope,
                receive,
                send,
                status_code=415,
                code="multipart_required",
                message="Upload files using multipart/form-data.",
            )
            return

        spool = tempfile.SpooledTemporaryFile(
            max_size=self.spool_memory_bytes,
            mode="w+b",
        )
        received_bytes = 0

        try:
            try:
                while True:
                    message = await receive()

                    if message["type"] == "http.disconnect":
                        return

                    if message["type"] != "http.request":
                        continue

                    chunk = message.get("body", b"")
                    received_bytes += len(chunk)

                    if received_bytes > self.max_body_bytes:
                        await self._reject_too_large(scope, receive, send)
                        return

                    if chunk:
                        await run_in_threadpool(spool.write, chunk)

                    if not message.get("more_body", False):
                        break

                if (
                    declared_length is not None
                    and received_bytes != declared_length
                ):
                    await self._reject(
                        scope,
                        receive,
                        send,
                        status_code=400,
                        code="content_length_mismatch",
                        message="The request body does not match Content-Length.",
                    )
                    return

                await run_in_threadpool(spool.seek, 0)

            except OSError:
                logger.exception("Temporary upload buffering failed.")
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=503,
                    code="upload_buffer_unavailable",
                    message="Temporary upload storage is unavailable.",
                )
                return

            remaining_bytes = received_bytes
            body_finished = False

            async def replay_receive() -> Message:
                nonlocal remaining_bytes, body_finished

                if body_finished:
                    return await receive()

                chunk = await run_in_threadpool(
                    spool.read,
                    min(REPLAY_CHUNK_SIZE, remaining_bytes),
                )
                remaining_bytes -= len(chunk)
                body_finished = remaining_bytes == 0

                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": not body_finished,
                }

            await self.app(scope, replay_receive, send)

        finally:
            try:
                await run_in_threadpool(spool.close)
            except OSError:
                logger.exception("Temporary upload spool cleanup failed.")

    async def _reject_too_large(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = error_response(
            status_code=413,
            code="request_too_large",
            message="The upload request exceeds the configured request limit.",
            details={"max_request_bytes": self.max_body_bytes},
        )
        await response(scope, receive, send)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = error_response(
            status_code=status_code,
            code=code,
            message=message,
        )
        await response(scope, receive, send)