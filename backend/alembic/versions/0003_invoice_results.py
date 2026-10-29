"""Store invoice snapshots on existing extraction attempts.

Revision ID: 0003_invoice_results
Revises: 0002_extracted_content

Only additive ALTER TABLE operations are used during upgrade. In particular,
SQLite's referenced extraction table is not copied or dropped.
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_invoice_results"
down_revision = "0002_extracted_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_extractions",
        sa.Column("invoice_result", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column(
        "document_extractions",
        sa.Column("invoice_identity", sa.String(64), nullable=True),
    )
    op.add_column(
        "document_extractions",
        sa.Column("invoice_digest", sa.String(64), nullable=True),
    )
    op.add_column(
        "document_extractions",
        sa.Column("invoice_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_document_extractions_invoice_identity",
        "document_extractions",
        ["invoice_identity"],
        unique=False,
    )


def downgrade() -> None:
    # Native DROP COLUMN requires SQLite >= 3.35, as supplied by current
    # standard Python 3.13 distributions. Avoid rebuilding the parent table:
    # rebuilding it with foreign keys enabled could cascade-delete its units.
    op.drop_index(
        "ix_document_extractions_invoice_identity",
        table_name="document_extractions",
    )
    op.drop_column("document_extractions", "invoice_created_at")
    op.drop_column("document_extractions", "invoice_digest")
    op.drop_column("document_extractions", "invoice_identity")
    op.drop_column("document_extractions", "invoice_result")
