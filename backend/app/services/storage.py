from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from app.core.config import SUPPORTED_EXTENSIONS, Settings
from app.core.errors import ApplicationError


logger = logging.getLogger(__name__)

_STORAGE_KEY_PATTERN = re.compile(
    r"[0-9a-f]{32}\.(?:pdf|jpg|jpeg|png|tif|tiff|xlsx|xls|csv)"
)


@dataclass(frozen=True, slots=True)
class UploadName:
    original_filename: str
    extension: str


@dataclass(frozen=True, slots=True)
class StoredFile:
    storage_key: str
    storage_backend: str
    original_filename: str
    extension: str
    size_bytes: int
    sha256: str


class StorageBackend(Protocol):
    """Storage operations needed by the upload service.

    A later S3 implementation can expose downloaded files through a
    temporary local validation path without changing database records.
    """

    backend_name: str

    def save(
        self,
        stream: BinaryIO,
        name: UploadName,
        *,
        remaining_total_bytes: int,
    ) -> StoredFile:
        ...

    def delete(self, storage_key: str) -> None:
        ...


def normalize_upload_name(filename: str | None) -> UploadName:
    """Extract a safe display name; it is never used as a storage path."""
    if not filename:
        raise ApplicationError(
            status_code=422,
            code="missing_filename",
            message="Each uploaded file must have a filename.",
        )

    # Browsers or clients may send Windows or POSIX path components.
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = unicodedata.normalize("NFC", filename)

    # Remove control characters, including bidirectional format controls.
    filename = "".join(
        character
        for character in filename
        if not unicodedata.category(character).startswith("C")
    ).strip()

    if "." not in filename:
        raise ApplicationError(
            status_code=415,
            code="unsupported_file_type",
            message="The file must have a supported filename extension.",
        )

    stem, suffix = filename.rsplit(".", 1)
    extension = f".{suffix.lower()}"

    if extension not in SUPPORTED_EXTENSIONS:
        raise ApplicationError(
            status_code=415,
            code="unsupported_file_type",
            message="Supported files are PDF, JPG, PNG, TIFF, XLSX, XLS, and CSV.",
        )

    stem = stem.strip(" .")
    if not stem:
        raise ApplicationError(
            status_code=422,
            code="invalid_filename",
            message="The filename must contain a name before its extension.",
        )

    # Match the existing database column's maximum character length.
    stem = stem[: 255 - len(extension)].rstrip(" .")

    return UploadName(
        original_filename=f"{stem}{extension}",
        extension=extension,
    )


class LocalStorage:
    backend_name = "local"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.local_storage_path.resolve()

    def path_for(self, storage_key: str) -> Path:
        """Resolve only application-generated keys inside the storage root."""
        if _STORAGE_KEY_PATTERN.fullmatch(storage_key) is None:
            raise ApplicationError(
                status_code=400,
                code="invalid_storage_key",
                message="The document storage reference is invalid.",
            )

        candidate = self.root / storage_key

        # The application does not follow file symlinks in its storage area.
        if candidate.is_symlink() or candidate.resolve().parent != self.root:
            raise ApplicationError(
                status_code=400,
                code="invalid_storage_key",
                message="The document storage reference is invalid.",
            )

        return candidate

    def save(
        self,
        stream: BinaryIO,
        name: UploadName,
        *,
        remaining_total_bytes: int,
    ) -> StoredFile:
        if name.extension not in SUPPORTED_EXTENSIONS:
            raise ApplicationError(
                status_code=415,
                code="unsupported_file_type",
                message="The file extension is not supported.",
            )

        storage_key = f"{uuid4().hex}{name.extension}"
        destination = self.path_for(storage_key)
        created = False
        size_bytes = 0
        digest = hashlib.sha256()

        try:
            self.root.mkdir(parents=True, exist_ok=True)

            # Exclusive creation prevents overwriting an existing file.
            with destination.open("xb") as output:
                created = True

                while True:
                    chunk = stream.read(self.settings.upload_chunk_size_bytes)
                    if not chunk:
                        break

                    if not isinstance(chunk, bytes):
                        raise TypeError("Upload streams must return bytes.")

                    next_size = size_bytes + len(chunk)

                    if next_size > self.settings.max_file_size_bytes:
                        raise ApplicationError(
                            status_code=413,
                            code="file_too_large",
                            message="A file exceeds the configured upload limit.",
                            details={
                                "max_file_size_bytes": (
                                    self.settings.max_file_size_bytes
                                ),
                            },
                        )

                    if next_size > remaining_total_bytes:
                        raise ApplicationError(
                            status_code=413,
                            code="upload_total_too_large",
                            message=(
                                "The combined files exceed the configured "
                                "upload limit."
                            ),
                            details={
                                "max_upload_total_bytes": (
                                    self.settings.max_upload_total_bytes
                                ),
                            },
                        )

                    output.write(chunk)
                    digest.update(chunk)
                    size_bytes = next_size

                if size_bytes == 0:
                    raise ApplicationError(
                        status_code=422,
                        code="empty_file",
                        message="Empty files cannot be uploaded.",
                    )

                output.flush()
                os.fsync(output.fileno())

            return StoredFile(
                storage_key=storage_key,
                storage_backend=self.backend_name,
                original_filename=name.original_filename,
                extension=name.extension,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
            )

        except Exception as error:
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Failed to remove partial upload; storage_key=%s",
                        storage_key,
                    )

            if isinstance(error, ApplicationError):
                raise

            logger.exception(
                "Local upload storage failed; storage_key=%s",
                storage_key,
            )
            raise ApplicationError(
                status_code=503,
                code="storage_unavailable",
                message="The file could not be stored. Please try again.",
            ) from error

    def delete(self, storage_key: str) -> None:
        destination = self.path_for(storage_key)

        try:
            destination.unlink(missing_ok=True)
        except OSError as error:
            logger.exception(
                "Local file deletion failed; storage_key=%s",
                storage_key,
            )
            raise ApplicationError(
                status_code=503,
                code="storage_cleanup_failed",
                message="Temporary upload cleanup could not be completed.",
            ) from error
