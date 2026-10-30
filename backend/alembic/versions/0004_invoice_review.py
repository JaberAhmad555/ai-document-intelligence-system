"""Append-only human corrections and idempotent failed-job reprocessing.

Revision ID: 0004_invoice_review
Revises: 0003_invoice_results
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_invoice_review"
down_revision = "0003_invoice_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive operations preserve existing SQLite foreign keys and all units.
    op.add_column("document_extractions", sa.Column(
        "correction_revision", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("document_extractions", sa.Column(
        "corrected_result", sa.JSON(none_as_null=True), nullable=True
    ))
    op.add_column("document_extractions", sa.Column(
        "corrected_identity", sa.String(64), nullable=True
    ))
    op.create_index("ix_document_extractions_corrected_identity",
                    "document_extractions", ["corrected_identity"])
    op.create_table(
        "invoice_corrections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("original_digest", sa.String(64), nullable=False),
        sa.Column("reviewer_label", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("corrected_fields", sa.JSON(), nullable=False),
        sa.Column("before_values", sa.JSON(), nullable=False),
        sa.Column("after_values", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("tolerance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["extraction_id", "document_id"],
            ["document_extractions.id", "document_extractions.document_id"],
            name="fk_invoice_corrections_attempt_document", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("extraction_id", "revision", name="uq_invoice_corrections_revision"),
        sa.UniqueConstraint("document_id", "request_id", name="uq_invoice_corrections_request"),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
    )
    op.create_index("ix_invoice_corrections_document_id", "invoice_corrections", ["document_id"])
    op.create_table(
        "reprocessing_requests",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("failed_job_id", sa.Uuid(), sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("new_job_id", sa.Uuid(), sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("new_job_id", name="uq_reprocessing_requests_new_job_id"),
        sa.UniqueConstraint("failed_job_id", name="uq_reprocessing_requests_failed_job_id"),
    )
    op.create_index("ix_reprocessing_requests_document_id", "reprocessing_requests", ["document_id"])


def downgrade() -> None:
    # This intentionally removes HUMAN REVIEW history. Back up before downgrading.
    op.drop_table("reprocessing_requests")
    op.drop_table("invoice_corrections")
    op.drop_index("ix_document_extractions_corrected_identity", table_name="document_extractions")
    # Native SQLite DROP COLUMN (>=3.35), no parent-table reconstruction.
    op.drop_column("document_extractions", "corrected_identity")
    op.drop_column("document_extractions", "corrected_result")
    op.drop_column("document_extractions", "correction_revision")
