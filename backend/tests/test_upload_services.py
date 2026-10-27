from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import openpyxl
import pymupdf
import pytest
from fastapi import UploadFile
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import Base, build_engine
from app.core.errors import ApplicationError
from app.models.ingestion import (
    Document,
    DocumentStatus,
    JobStatus,
    ProcessingJob,
)
from app.services.file_validation import validate_file
from app.services.storage import LocalStorage, normalize_upload_name
from app.services.uploads import upload_documents


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        debug=False,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}",
        local_storage_path=tmp_path / "uploads",
        max_upload_files=10,
        max_file_size_mb=1,
        max_upload_total_mb=2,
        upload_chunk_size_bytes=64 * 1024,
        max_archive_entries=10000,
        max_archive_uncompressed_mb=16,
        max_archive_compression_ratio=200,
        max_image_pixels=1_000_000,
        job_max_attempts=3,
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Session]:
    engine = build_engine(settings)

    # create_all is only for isolated service tests.
    # The real application will use Alembic migrations.
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def upload_factory():
    opened: list[UploadFile] = []

    def make(filename: str, content: bytes) -> UploadFile:
        upload = UploadFile(
            filename=filename,
            file=io.BytesIO(content),
        )
        opened.append(upload)
        return upload

    yield make

    for upload in opened:
        upload.file.close()


def pdf_bytes(pages: int = 1) -> bytes:
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"Synthetic invoice page {number + 1}")
        return document.tobytes()


def image_bytes(image_format: str, *, multiple_frames: bool = False) -> bytes:
    output = io.BytesIO()

    with Image.new("RGB", (32, 32), color="white") as first:
        if multiple_frames:
            with Image.new("RGB", (32, 32), color="gray") as second:
                first.save(
                    output,
                    format=image_format,
                    save_all=True,
                    append_images=[second],
                )
        else:
            first.save(output, format=image_format)

    return output.getvalue()


def xlsx_bytes() -> bytes:
    output = io.BytesIO()
    workbook = openpyxl.Workbook()

    try:
        first = workbook.active
        first.title = "Invoices"
        first.append(["invoice_number", "total"])
        first.append(["SYN-001", 125])

        second = workbook.create_sheet("Payments")
        second.append(["invoice_number", "paid"])
        second.append(["SYN-001", 125])

        workbook.save(output)
    finally:
        workbook.close()

    return output.getvalue()


def stored_paths(settings: Settings) -> list[Path]:
    return list(settings.local_storage_path.glob("*"))


def assert_no_database_records(database: Session) -> None:
    assert database.scalar(select(func.count()).select_from(Document)) == 0
    assert database.scalar(select(func.count()).select_from(ProcessingJob)) == 0


@pytest.mark.parametrize(
    ("filename", "builder"),
    [
        ("invoice.pdf", lambda: pdf_bytes(3)),
        ("invoice.jpg", lambda: image_bytes("JPEG")),
        ("invoice.png", lambda: image_bytes("PNG")),
        (
            "invoice.tiff",
            lambda: image_bytes("TIFF", multiple_frames=True),
        ),
        ("invoice.xlsx", xlsx_bytes),
        ("invoice.csv", lambda: b"invoice_number,total\nSYN-001,125.00\n"),
    ],
)
def test_supported_files_create_queued_records(
    filename,
    builder,
    settings,
    database,
    upload_factory,
):
    payload = builder()

    documents = upload_documents(
        uploads=[upload_factory(filename, payload)],
        database=database,
        settings=settings,
    )

    assert len(documents) == 1
    document = documents[0]

    assert document.original_filename == filename
    assert document.status == DocumentStatus.QUEUED
    assert document.progress_percent == 0
    assert document.processing_stage == "queued"
    assert document.processed_units == 0
    assert document.total_units is None
    assert document.size_bytes == len(payload)
    assert len(document.sha256) == 64

    job = database.scalar(
        select(ProcessingJob).where(ProcessingJob.document_id == document.id)
    )
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.max_attempts == settings.job_max_attempts
    assert job.lease_token is None
    assert job.lease_expires_at is None

    stored = LocalStorage(settings).path_for(document.storage_key)
    assert stored.read_bytes() == payload


def test_repeated_filenames_use_distinct_storage_keys(
    settings,
    database,
    upload_factory,
):
    payload = b"invoice,total\nSYN-001,125\n"

    documents = upload_documents(
        uploads=[
            upload_factory("invoice.csv", payload),
            upload_factory("invoice.csv", payload),
        ],
        database=database,
        settings=settings,
    )

    assert len(documents) == 2
    assert documents[0].storage_key != documents[1].storage_key
    assert documents[0].sha256 == documents[1].sha256
    assert len(stored_paths(settings)) == 2


def test_filename_paths_are_removed():
    result = normalize_upload_name(r"C:\customer\..\invoice.CSV")

    assert result.original_filename == "invoice.csv"
    assert result.extension == ".csv"


def test_storage_rejects_path_traversal(settings):
    with pytest.raises(ApplicationError) as caught:
        LocalStorage(settings).path_for("../../outside.csv")

    assert caught.value.code == "invalid_storage_key"


@pytest.mark.parametrize(
    ("filename", "content", "expected_status"),
    [
        ("invoice.exe", b"MZ", 415),
        ("invoice.pdf", b"not a PDF", 422),
        ("invoice.png", b"\x89PNG\r\n\x1a\nbroken", 422),
        ("invoice.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1broken", 422),
        ("invoice.xlsx", b"not a ZIP archive", 422),
        ("invoice.csv", b"invoice,total\nSYN-001,\x00\n", 422),
        ("invoice.csv", b"", 422),
    ],
)
def test_invalid_files_leave_no_records_or_files(
    filename,
    content,
    expected_status,
    settings,
    database,
    upload_factory,
):
    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[upload_factory(filename, content)],
            database=database,
            settings=settings,
        )

    assert caught.value.status_code == expected_status
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_image_extension_must_match_contents(
    settings,
    database,
    upload_factory,
):
    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[upload_factory("invoice.jpg", image_bytes("PNG"))],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "file_type_mismatch"
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_failed_second_file_rolls_back_entire_batch(
    settings,
    database,
    upload_factory,
):
    with pytest.raises(ApplicationError):
        upload_documents(
            uploads=[
                upload_factory("valid.csv", b"invoice,total\nSYN-001,125\n"),
                upload_factory("broken.pdf", b"not a PDF"),
            ],
            database=database,
            settings=settings,
        )

    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_per_file_limit_removes_partial_file(
    settings,
    database,
    upload_factory,
):
    payload = b"x" * (settings.max_file_size_bytes + 1)

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[upload_factory("large.csv", payload)],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "file_too_large"
    assert caught.value.status_code == 413
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_total_limit_removes_all_batch_files(
    settings,
    database,
    upload_factory,
):
    # Valid, consistent CSV rows. Each file is below the per-file limit.
    payload = b"invoice,total\n" + b"SYN,1\n" * 120_000

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory("first.csv", payload),
                upload_factory("second.csv", payload),
                upload_factory("third.csv", payload),
            ],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "upload_total_too_large"
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_too_many_files_rejected_before_saving(
    settings,
    database,
    upload_factory,
):
    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[
                upload_factory(f"invoice-{number}.csv", b"id,total\nA,1\n")
                for number in range(settings.max_upload_files + 1)
            ],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "too_many_files"
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_database_commit_failure_cleans_uncommitted_files(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    def fail_commit():
        raise OperationalError(
            statement=None,
            params=None,
            orig=RuntimeError("Synthetic database failure"),
        )

    monkeypatch.setattr(database, "commit", fail_commit)

    with pytest.raises(ApplicationError) as caught:
        upload_documents(
            uploads=[upload_factory("invoice.csv", b"id,total\nSYN-001,125\n")],
            database=database,
            settings=settings,
        )

    assert caught.value.code == "database_unavailable"
    assert stored_paths(settings) == []
    assert_no_database_records(database)


def test_acknowledgement_failure_preserves_committed_file(
    monkeypatch,
    settings,
    database,
    upload_factory,
):
    actual_commit = database.commit

    def commit_then_report_failure():
        actual_commit()
        raise OperationalError(
            statement=None,
            params=None,
            orig=RuntimeError("Synthetic acknowledgement failure"),
        )

    monkeypatch.setattr(database, "commit", commit_then_report_failure)

    documents = upload_documents(
        uploads=[upload_factory("invoice.csv", b"id,total\nSYN-001,125\n")],
        database=database,
        settings=settings,
    )

    assert len(documents) == 1
    assert len(stored_paths(settings)) == 1
    assert database.scalar(select(func.count()).select_from(Document)) == 1
    assert database.scalar(select(func.count()).select_from(ProcessingJob)) == 1


def test_xlsx_xml_doctype_is_rejected(settings, tmp_path):
    path = tmp_path / "unsafe.xlsx"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            b'<!DOCTYPE Types [<!ENTITY value "unsafe">]>'
            b"<Types>&value;</Types>",
        )
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        archive.writestr("xl/_rels/workbook.xml.rels", b"<Relationships/>")

    with pytest.raises(ApplicationError) as caught:
        validate_file(path, ".xlsx", settings)

    assert caught.value.code == "unsafe_spreadsheet_xml"


def test_xlsx_high_compression_ratio_is_rejected(settings, tmp_path):
    path = tmp_path / "compressed.xlsx"

    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "[Content_Types].xml",
            b"<Types>" + b" " * 500_000 + b"</Types>",
        )
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        archive.writestr("xl/_rels/workbook.xml.rels", b"<Relationships/>")

    with pytest.raises(ApplicationError) as caught:
        validate_file(path, ".xlsx", settings)

    assert caught.value.code == "archive_compression_limit"


def test_utf16_csv_is_supported(
    settings,
    database,
    upload_factory,
):
    documents = upload_documents(
        uploads=[
            upload_factory(
                "invoice.csv",
                "invoice,total\nSYN-001,125\n".encode("utf-16"),
            )
        ],
        database=database,
        settings=settings,
    )

    assert len(documents) == 1
    assert documents[0].media_type == "text/csv"
