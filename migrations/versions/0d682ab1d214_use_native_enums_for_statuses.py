"""use native enums for statuses

Revision ID: 0d682ab1d214
Revises: 4789a5a5ba78
Create Date: 2026-10-15 19:12:46.588911
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0d682ab1d214"
down_revision: str | None = "4789a5a5ba78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

asset_status_enum = postgresql.ENUM(
    "discovered",
    "probing",
    "indexed",
    "analyzing",
    "ready",
    "failed",
    "missing",
    name="asset_status",
)
job_status_enum = postgresql.ENUM(
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    name="job_status",
)


def upgrade() -> None:
    """Применить изменения схемы."""
    connection = op.get_bind()
    asset_status_enum.create(connection, checkfirst=True)
    job_status_enum.create(connection, checkfirst=True)

    op.drop_constraint(op.f("ck_assets_status"), "assets", type_="check")
    op.alter_column(
        "assets",
        "status",
        existing_type=sa.String(length=32),
        server_default=None,
    )
    op.alter_column(
        "assets",
        "status",
        existing_type=sa.String(length=32),
        type_=asset_status_enum,
        existing_nullable=False,
        postgresql_using="status::text::asset_status",
    )
    op.alter_column(
        "assets",
        "status",
        existing_type=asset_status_enum,
        server_default=sa.text("'discovered'::asset_status"),
    )

    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.alter_column(
        "jobs",
        "status",
        existing_type=sa.String(length=32),
        server_default=None,
    )
    op.alter_column(
        "jobs",
        "status",
        existing_type=sa.String(length=32),
        type_=job_status_enum,
        existing_nullable=False,
        postgresql_using="status::text::job_status",
    )
    op.alter_column(
        "jobs",
        "status",
        existing_type=job_status_enum,
        server_default=sa.text("'pending'::job_status"),
    )


def downgrade() -> None:
    """Откатить изменения схемы."""
    op.alter_column(
        "jobs",
        "status",
        existing_type=job_status_enum,
        server_default=None,
    )
    op.alter_column(
        "jobs",
        "status",
        existing_type=job_status_enum,
        type_=sa.String(length=32),
        existing_nullable=False,
        postgresql_using="status::text",
    )
    op.alter_column(
        "jobs",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'pending'"),
    )
    op.create_check_constraint(
        op.f("ck_jobs_status"),
        "jobs",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
    )

    op.alter_column(
        "assets",
        "status",
        existing_type=asset_status_enum,
        server_default=None,
    )
    op.alter_column(
        "assets",
        "status",
        existing_type=asset_status_enum,
        type_=sa.String(length=32),
        existing_nullable=False,
        postgresql_using="status::text",
    )
    op.alter_column(
        "assets",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'discovered'"),
    )
    op.create_check_constraint(
        op.f("ck_assets_status"),
        "assets",
        "status IN ('discovered', 'probing', 'indexed', 'analyzing', "
        "'ready', 'failed', 'missing')",
    )

    job_status_enum.drop(op.get_bind(), checkfirst=True)
    asset_status_enum.drop(op.get_bind(), checkfirst=True)
