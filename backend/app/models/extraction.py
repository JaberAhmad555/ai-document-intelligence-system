from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UTCDateTime, utc_now


class DocumentExtraction(Base):
    """One immutable-source extraction attempt.

    The primary key is the worker's lease token.

    ready_at means the reader finished and all output was persisted.
    It does not independently establish job success.
    """

    __tablename__ = "document_extractions"

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_document_extractions_job_attempt",
        ),
        UniqueConstraint(
            "id",
            "document_id",
            name="uq_document_extractions_id_document",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="positive_attempt",
        ),
        CheckConstraint(
            "unit_count >= 0",
            name="nonnegative_unit_count",
        ),
        CheckConstraint(
            "part_count IS NULL OR part_count >= 1",
            name="positive_part_count",
        ),
        CheckConstraint(
            "ready_at IS NULL OR "
            "(unit_count > 0 AND part_count IS NOT NULL AND part_count > 0)",
            name="ready_has_units",
        ),
        Index("ix_document_extractions_invoice_identity", "invoice_identity"),
        Index(
            "ix_document_extractions_document_id",
            "document_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    source: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    unit_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    part_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )


    # Immutable invoice snapshot for this lease-token extraction attempt.
    # Publication additionally requires the existing job/document success gates.
    invoice_result: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    invoice_identity: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    invoice_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    invoice_created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )


class ExtractedUnit(Base):
    """One page, image frame, worksheet boundary, or table row."""

    __tablename__ = "extracted_units"

    __table_args__ = (
        CheckConstraint("sequence >= 1", name="positive_sequence"),
        CheckConstraint(
            "kind IN ('page', 'image_frame', 'sheet', 'row')",
            name="valid_kind",
        ),
        CheckConstraint(
            "method IN "
            "('pdf_text', 'ocr', 'pdf_text_and_ocr', 'spreadsheet', 'csv')",
            name="valid_method",
        ),
    )

    extraction_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_extractions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)

    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    native_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    position: Mapped[dict] = mapped_column(JSON, nullable=False)
    cells: Mapped[list] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False)


class DocumentExtractionHead(Base):
    """Select the candidate extraction for a document.

    Retrieval must also verify the extraction's job succeeded.
    Selecting this row alone is not proof of successful processing.
    """

    __tablename__ = "document_extraction_heads"

    __table_args__ = (
        ForeignKeyConstraint(
            ["extraction_id", "document_id"],
            ["document_extractions.id", "document_extractions.document_id"],
            name="fk_extraction_heads_attempt_document",
            ondelete="CASCADE",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    extraction_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
