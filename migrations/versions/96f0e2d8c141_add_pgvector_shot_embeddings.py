"""add pgvector shot embeddings

Revision ID: 96f0e2d8c141
Revises: face66979dc4
Create Date: 2026-10-15 21:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "96f0e2d8c141"
down_revision: str | None = "face66979dc4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 768
embedding_status_enum = postgresql.ENUM(
    "pending",
    "running",
    "succeeded",
    "failed",
    name="embedding_status",
    create_type=False,
)


def upgrade() -> None:
    """Включить pgvector и добавить версионированные embeddings планов."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    connection = op.get_bind()
    embedding_status_enum.create(connection, checkfirst=True)

    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_scan', 'asset_probe', 'shot_detection', "
        "'asset_description', 'asset_embedding')",
    )

    op.create_table(
        "shot_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shot_description_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            embedding_status_enum,
            server_default=sa.text("'pending'::embedding_status"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("text_version", sa.String(length=100), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("indexed_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dimension > 0",
            name=op.f("ck_shot_embeddings_dimension"),
        ),
        sa.ForeignKeyConstraint(
            ["shot_description_id"],
            ["shot_descriptions.id"],
            name=op.f("fk_shot_embeddings_shot_description_id_shot_descriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shot_embeddings")),
        sa.UniqueConstraint(
            "shot_description_id",
            "provider",
            "model",
            "text_version",
            name="shot_description_provider_model_text_version",
        ),
    )
    op.create_index(
        "ix_shot_embeddings_search_version",
        "shot_embeddings",
        ["status", "provider", "model", "text_version"],
        unique=False,
    )


def downgrade() -> None:
    """Удалить embeddings, сохранив расширение для безопасного отката."""
    op.drop_index(
        "ix_shot_embeddings_search_version",
        table_name="shot_embeddings",
    )
    op.drop_table("shot_embeddings")
    embedding_status_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_scan', 'asset_probe', 'shot_detection', "
        "'asset_description')",
    )
