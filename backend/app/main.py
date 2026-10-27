from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.database import build_engine, get_db
from app.core.errors import ApplicationError
from app.core.http import (
    UploadBodyLimitMiddleware,
    application_error_handler,
    database_error_handler,
    http_error_handler,
    request_validation_error_handler,
    unexpected_error_handler,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Each application instance owns its engine and session factory.
        # This also lets tests use isolated configuration and databases.
        engine = build_engine(config)
        application.state.database_engine = engine
        application.state.session_factory = sessionmaker(
            bind=engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

        try:
            # Tables are deliberately NOT created here.
            # Apply Alembic migrations before using document endpoints.
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title=config.app_name,
        description=(
            "Document upload, metadata, and persistent processing queue API. "
            "An accepted upload is queued; processing requires a separate worker."
        ),
        version="0.1.0",
        # Keep structured error responses even when DEBUG is configured.
        # Never expose interactive traceback pages through the API.
        debug=False,
        lifespan=lifespan,
    )

    def request_database() -> Generator[Session, None, None]:
        factory = application.state.session_factory

        with factory() as session:
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    # Existing routes and services retain their established dependencies,
    # while every app instance receives its own configuration and sessions.
    application.dependency_overrides[get_settings] = lambda: config
    application.dependency_overrides[get_db] = request_database

    application.add_exception_handler(
        ApplicationError,
        application_error_handler,
    )
    application.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    application.add_exception_handler(
        StarletteHTTPException,
        http_error_handler,
    )
    application.add_exception_handler(
        SQLAlchemyError,
        database_error_handler,
    )
    application.add_exception_handler(
        Exception,
        unexpected_error_handler,
    )

    application.include_router(health_router)
    application.include_router(documents_router)

    application.add_middleware(
        UploadBodyLimitMiddleware,
        settings=config,
    )

    # Added last so CORS also wraps upload-limit middleware responses.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Authorization"],
        max_age=600,
    )

    return application


app = create_app()