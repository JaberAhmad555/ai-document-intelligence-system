"""Create document records and the persistent processing queue.

Revision ID: 0001_initial_ingestion
Revises: None
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_ingestion"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "original_filename",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "storage_key",
            sa.String(length=512),
            nullable=False,
        ),
        sa.Column(
            "storage_backend",
            sa.String(length=20),
            server_default=sa.text("'local'"),
            nullable=False,
        ),
        sa.Column(
            "extension",
            sa.String(length=10),
            nullable=False,
        ),
        sa.Column(
            "media_type",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "size_bytes",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column(
            "sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "processing",
                "completed",
                "failed",
                name="document_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "processing_stage",
            sa.String(length=50),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "progress_percent",
            sa.Integer(),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column(
            "total_units",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "processed_units",
            sa.Integer(),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column(
            "error_code",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "processing_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "processing_finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')",
            name=op.f("ck_documents_document_status"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_documents_positive_size"),
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name=op.f("ck_documents_valid_progress"),
        ),
        sa.CheckConstraint(
            "processed_units >= 0",
            name=op.f("ck_documents_nonnegative_processed_units"),
        ),
        sa.CheckConstraint(
            "total_units IS NULL OR total_units >= 0",
            name=op.f("ck_documents_nonnegative_total_units"),
        ),
        sa.CheckConstraint(
            "total_units IS NULL OR processed_units <= total_units",
            name=op.f("ck_documents_processed_units_within_total"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_documents"),
        ),
        sa.UniqueConstraint(
            "storage_key",
            name=op.f("uq_documents_storage_key"),
        ),
    )

    op.create_index(
        "ix_documents_sha256",
        "documents",
        ["sha256"],
        unique=False,
    )
    op.create_index(
        "ix_documents_created_at_id",
        "documents",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_documents_status_created_at",
        "documents",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "processing_jobs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "process_document",
                name="processing_job_kind",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'process_document'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="processing_job_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_by",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "lease_token",
            sa.Uuid(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_error_code",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "last_error_message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ('process_document')",
            name=op.f("ck_processing_jobs_processing_job_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_processing_jobs_processing_job_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_processing_jobs_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f("ck_processing_jobs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts",
            name=op.f("ck_processing_jobs_attempt_count_within_limit"),
        ),
        sa.CheckConstraint(
            "status != 'running' OR "
            "(locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_processing_jobs_running_job_has_lease"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_processing_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_processing_jobs"),
        ),
    )

    op.create_index(
        "ix_processing_jobs_document_id",
        "processing_jobs",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        "ix_processing_jobs_claim",
        "processing_jobs",
        ["status", "available_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_processing_jobs_expired_lease",
        "processing_jobs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_processing_jobs_active_document",
        "processing_jobs",
        ["document_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    # Drop the dependent table before its parent.
    op.drop_index(
        "uq_processing_jobs_active_document",
        table_name="processing_jobs",
    )
    op.drop_index(
        "ix_processing_jobs_expired_lease",
        table_name="processing_jobs",
    )
    op.drop_index(
        "ix_processing_jobs_claim",
        table_name="processing_jobs",
    )
    op.drop_index(
        "ix_processing_jobs_document_id",
        table_name="processing_jobs",
    )
    op.drop_table("processing_jobs")

    op.drop_index(
        "ix_documents_status_created_at",
        table_name="documents",
    )
    op.drop_index(
        "ix_documents_created_at_id",
        table_name="documents",
    )
    op.drop_index(
        "ix_documents_sha256",
        table_name="documents",
    )
    op.drop_table("documents")