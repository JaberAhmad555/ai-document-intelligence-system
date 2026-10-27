from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.ingestion import (
    Document,
    DocumentStatus,
    JobKind,
    JobStatus,
    ProcessingJob,
)
from app.services.file_validation import validate_file
from app.services.storage import LocalStorage, StoredFile, normalize_upload_name


logger = logging.getLogger(__name__)


def _rollback_safely(database: Session) -> None:
    try:
        database.rollback()
    except Exception:
        logger.exception("Failed to roll back the upload database session.")


def _cleanup_files(
    storage: LocalStorage,
    files: Sequence[StoredFile],
    *,
    preserve_keys: set[str] | None = None,
) -> int:
    preserved = preserve_keys or set()
    failures = 0

    for stored in reversed(files):
        if stored.storage_key in preserved:
            continue

        try:
            storage.delete(stored.storage_key)
        except Exception:
            failures += 1
            logger.exception(
                "Upload cleanup requires recovery; storage_key=%s",
                stored.storage_key,
            )

    return failures


def _find_committed_documents(
    database: Session,
    files: Sequence[StoredFile],
) -> dict[str, Document]:
    """Check an uncertain commit using a separate database session."""
    keys = [stored.storage_key for stored in files]

    with Session(bind=database.get_bind(), expire_on_commit=False) as probe:
        documents = probe.scalars(
            select(Document).where(Document.storage_key.in_(keys))
        ).all()

        # Detach loaded scalar fields so the caller can serialize them.
        for document in documents:
            probe.expunge(document)

        return {document.storage_key: document for document in documents}


def upload_documents(
    *,
    uploads: Sequence[UploadFile],
    database: Session,
    settings: Settings,
    storage: LocalStorage | None = None,
) -> list[Document]:
    """Validate and persist one all-or-nothing upload batch.

    This is synchronous code. The API must call it from a synchronous
    FastAPI route or explicitly offload it to a worker thread.

    The caller owns and closes UploadFile objects. This service owns the
    database transaction and requires a session with no active transaction.
    """
    if database.in_transaction():
        raise RuntimeError(
            "upload_documents requires a session without an active transaction."
        )

    if not uploads:
        raise ApplicationError(
            status_code=422,
            code="no_files",
            message="Select at least one file to upload.",
        )

    if len(uploads) > settings.max_upload_files:
        raise ApplicationError(
            status_code=413,
            code="too_many_files",
            message="The upload contains too many files.",
            details={"max_upload_files": settings.max_upload_files},
        )

    # Validate names before storing any bytes.
    names = []
    for index, upload in enumerate(uploads):
        try:
            names.append(normalize_upload_name(upload.filename))
        except ApplicationError as error:
            error.details.setdefault("file_index", index)
            raise

    file_storage = storage if storage is not None else LocalStorage(settings)
    stored_files: list[StoredFile] = []
    documents: list[Document] = []
    jobs: list[ProcessingJob] = []
    total_bytes = 0
    commit_started = False

    try:
        for index, (upload, name) in enumerate(zip(uploads, names, strict=True)):
            try:
                # UploadFile uses a seekable, potentially disk-spooled stream.
                upload.file.seek(0)

                stored = file_storage.save(
                    upload.file,
                    name,
                    remaining_total_bytes=(
                        settings.max_upload_total_bytes - total_bytes
                    ),
                )
                stored_files.append(stored)
                total_bytes += stored.size_bytes

                validated = validate_file(
                    file_storage.path_for(stored.storage_key),
                    stored.extension,
                    settings,
                )

                document = Document(
                    original_filename=stored.original_filename,
                    storage_key=stored.storage_key,
                    storage_backend=stored.storage_backend,
                    extension=stored.extension,
                    media_type=validated.media_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    status=DocumentStatus.QUEUED,
                    processing_stage="queued",
                    progress_percent=0,
                    processed_units=0,
                    total_units=None,
                )
                job = ProcessingJob(
                    document=document,
                    kind=JobKind.PROCESS_DOCUMENT,
                    status=JobStatus.QUEUED,
                    attempt_count=0,
                    max_attempts=settings.job_max_attempts,
                )

                documents.append(document)
                jobs.append(job)

            except ApplicationError as error:
                error.details.setdefault("file_index", index)
                error.details.setdefault(
                    "filename",
                    name.original_filename,
                )
                raise

        # Files are all validated before database writes begin.
        database.add_all(documents)
        database.add_all(jobs)

        # Detect ordinary constraint failures before entering commit.
        database.flush()

        commit_started = True
        database.commit()

        return documents

    except Exception as error:
        _rollback_safely(database)

        committed: dict[str, Document] = {}

        if commit_started:
            try:
                committed = _find_committed_documents(database, stored_files)
            except Exception as probe_error:
                # A lost connection during COMMIT does not establish whether
                # the transaction committed. Do not delete possible originals.
                logger.exception(
                    "Upload commit outcome unknown; retained storage_keys=%s",
                    [stored.storage_key for stored in stored_files],
                )
                raise ApplicationError(
                    status_code=503,
                    code="upload_outcome_unknown",
                    message=(
                        "The upload outcome could not be confirmed. "
                        "Check the document list before uploading again."
                    ),
                    details={"files_retained_for_recovery": True},
                ) from probe_error

            if len(committed) == len(stored_files):
                logger.warning(
                    "Upload commit succeeded despite a reported commit error."
                )
                return [
                    committed[stored.storage_key]
                    for stored in stored_files
                ]

        cleanup_failures = _cleanup_files(
            file_storage,
            stored_files,
            preserve_keys=set(committed),
        )

        if committed:
            # An ordinary transaction should never partially commit.
            logger.error(
                "Unexpected partial upload persistence; preserved storage_keys=%s",
                list(committed),
            )
            raise ApplicationError(
                status_code=500,
                code="upload_recovery_required",
                message=(
                    "The upload requires recovery. "
                    "Check the document list before uploading again."
                ),
                details={"files_retained_for_recovery": True},
            ) from error

        if isinstance(error, ApplicationError):
            if cleanup_failures:
                error.details["cleanup_pending"] = True
            raise

        if isinstance(error, SQLAlchemyError):
            logger.exception("The upload database transaction failed.")
            raise ApplicationError(
                status_code=503,
                code="database_unavailable",
                message="The upload could not be recorded. Please try again.",
                details={"cleanup_pending": bool(cleanup_failures)},
            ) from error

        logger.exception("Unexpected upload failure.")
        raise ApplicationError(
            status_code=500,
            code="upload_failed",
            message="The upload could not be completed.",
            details={"cleanup_pending": bool(cleanup_failures)},
        ) from error
