"""Persist document reading attempts and extracted units.

Revision ID: 0002_extracted_content
Revises: 0001_initial_ingestion
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_extracted_content"
down_revision: Union[str, Sequence[str], None] = "0001_initial_ingestion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_extractions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column(
            "unit_count",
            sa.Integer(),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column("part_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_document_extractions"),
        ),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_document_extractions_job_attempt",
        ),
        sa.UniqueConstraint(
            "id",
            "document_id",
            name="uq_document_extractions_id_document",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_extractions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_document_extractions_job_id_processing_jobs"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_document_extractions_positive_attempt"),
        ),
        sa.CheckConstraint(
            "unit_count >= 0",
            name=op.f("ck_document_extractions_nonnegative_unit_count"),
        ),
        sa.CheckConstraint(
            "part_count IS NULL OR part_count >= 1",
            name=op.f("ck_document_extractions_positive_part_count"),
        ),
        sa.CheckConstraint(
            "ready_at IS NULL OR "
            "(unit_count > 0 AND part_count IS NOT NULL AND part_count > 0)",
            name=op.f("ck_document_extractions_ready_has_units"),
        ),
    )

    op.create_index(
        "ix_document_extractions_document_id",
        "document_extractions",
        ["document_id"],
        unique=False,
    )

    op.create_table(
        "extracted_units",
        sa.Column("extraction_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("method", sa.String(24), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("native_text", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("position", sa.JSON(), nullable=False),
        sa.Column("cells", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint(
            "extraction_id",
            "sequence",
            name=op.f("pk_extracted_units"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["document_extractions.id"],
            name=op.f("fk_extracted_units_extraction_id_document_extractions"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name=op.f("ck_extracted_units_positive_sequence"),
        ),
        sa.CheckConstraint(
            "kind IN ('page', 'image_frame', 'sheet', 'row')",
            name=op.f("ck_extracted_units_valid_kind"),
        ),
        sa.CheckConstraint(
            "method IN "
            "('pdf_text', 'ocr', 'pdf_text_and_ocr', 'spreadsheet', 'csv')",
            name=op.f("ck_extracted_units_valid_method"),
        ),
    )

    op.create_table(
        "document_extraction_heads",
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("extraction_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "document_id",
            name=op.f("pk_document_extraction_heads"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_extraction_heads_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "document_id"],
            ["document_extractions.id", "document_extractions.document_id"],
            name="fk_extraction_heads_attempt_document",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("document_extraction_heads")
    op.drop_table("extracted_units")
    op.drop_index(
        "ix_document_extractions_document_id",
        table_name="document_extractions",
    )
    op.drop_table("document_extractions")
