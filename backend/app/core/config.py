from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]
MEBIBYTE = 1024 * 1024

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"}
)


class Settings(BaseSettings):
    """Validated application configuration.

    Environment variables override values loaded from backend/.env.
    Relative local paths are anchored to the backend directory.
    """

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(
        default="Document Intelligence API",
        min_length=1,
        max_length=100,
    )
    app_env: Literal["development", "test", "production"] = "development"
    # Never serialize or display the credential in settings output.
    document_api_token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    document_auth_disabled: bool = False
    # Render sets RENDER=true. Hosted services cannot use a development/test bypass.
    render: bool = Field(default=False, repr=False, exclude=True)

    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Prevent accidental inclusion of a credential-bearing URL in repr(settings).
    database_url: str = Field(
        default="sqlite+pysqlite:///./data/document_intelligence.db",
        min_length=1,
        repr=False,
    )

    # S3 support will be added explicitly during the deployment phase.
    storage_backend: Literal["local"] = "local"
    local_storage_path: Path = Path("data/uploads")

    max_upload_files: int = Field(default=10, ge=1, le=100)
    max_file_size_mb: int = Field(default=25, ge=1, le=2048)
    max_upload_total_mb: int = Field(default=100, ge=1, le=4096)
    upload_chunk_size_bytes: int = Field(
        default=1024 * 1024,
        ge=64 * 1024,
        le=8 * 1024 * 1024,
    )

    max_archive_entries: int = Field(default=10000, ge=1, le=100000)
    max_archive_uncompressed_mb: int = Field(default=256, ge=1, le=4096)
    max_archive_compression_ratio: int = Field(default=200, ge=1, le=1000)
    max_image_pixels: int = Field(
        default=40_000_000,
        ge=1_000_000,
        le=200_000_000,
    )

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_lease_seconds: int = Field(default=300, ge=30, le=3600)

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        allowed_prefixes = (
            "sqlite://",
            "sqlite+pysqlite://",
            "postgres://",
            "postgresql://",
            "postgresql+psycopg://",
        )
        if not value.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL must use SQLite or PostgreSQL with psycopg."
            )
        return value

    @field_validator("local_storage_path")
    @classmethod
    def resolve_storage_path(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        origins: list[str] = []

        for value in values:
            origin = value.strip().rstrip("/")
            parsed = urlsplit(origin)

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Each CORS origin must be an explicit HTTP or HTTPS "
                    "origin without credentials, a path, query, or fragment."
                )

            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("CORS origin contains an invalid port.") from exc

            if origin not in origins:
                origins.append(origin)

        return origins

    @model_validator(mode="after")
    def validate_related_settings(self) -> "Settings":
        if self.max_upload_total_mb < self.max_file_size_mb:
            raise ValueError(
                "MAX_UPLOAD_TOTAL_MB must be at least MAX_FILE_SIZE_MB."
            )

        if self.upload_chunk_size_bytes > self.max_file_size_bytes:
            raise ValueError(
                "UPLOAD_CHUNK_SIZE_BYTES cannot exceed the per-file limit."
            )

        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production.")

        return self

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * MEBIBYTE

    @property
    def max_upload_total_bytes(self) -> int:
        return self.max_upload_total_mb * MEBIBYTE

    @property
    def max_archive_uncompressed_bytes(self) -> int:
        return self.max_archive_uncompressed_mb * MEBIBYTE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()
