"""add library preparation jobs

Revision ID: c481d720c929
Revises: 96f0e2d8c141
Create Date: 2026-10-16 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c481d720c929"
down_revision: str | None = "96f0e2d8c141"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить состояние составного конвейера и восстановления доставки."""
    op.add_column("jobs", sa.Column("parent_job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "recovery_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f("fk_jobs_parent_job_id_jobs"),
        "jobs",
        "jobs",
        ["parent_job_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_jobs_parent_status",
        "jobs",
        ["parent_job_id", "status"],
        unique=False,
    )
    op.create_check_constraint(
        op.f("ck_jobs_recovery_attempts"),
        "jobs",
        "recovery_attempts >= 0",
    )
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_preparation', 'library_scan', 'asset_probe', "
        "'shot_detection', 'asset_description', 'asset_embedding')",
    )


def downgrade() -> None:
    """Удалить состояние диспетчера и вернуть прежние типы задач."""
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_scan', 'asset_probe', 'shot_detection', "
        "'asset_description', 'asset_embedding')",
    )
    op.drop_constraint(op.f("ck_jobs_recovery_attempts"), "jobs", type_="check")
    op.drop_index("ix_jobs_parent_status", table_name="jobs")
    op.drop_constraint(
        op.f("fk_jobs_parent_job_id_jobs"),
        "jobs",
        type_="foreignkey",
    )
    op.drop_column("jobs", "recovery_attempts")
    op.drop_column("jobs", "last_dispatched_at")
    op.drop_column("jobs", "parent_job_id")
