"""add shots frames and descriptions

Revision ID: face66979dc4
Revises: 0d682ab1d214
Create Date: 2026-10-15 19:41:30.619629
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "face66979dc4"
down_revision: str | None = "0d682ab1d214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

description_status_enum = postgresql.ENUM(
    "pending",
    "running",
    "succeeded",
    "failed",
    name="description_status",
    create_type=False,
)


def upgrade() -> None:
    """Применить изменения схемы."""
    connection = op.get_bind()
    description_status_enum.create(connection, checkfirst=True)
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_scan', 'asset_probe', 'shot_detection', "
        "'asset_description')",
    )

    op.create_table(
        "shots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("detector", sa.String(length=100), nullable=False),
        sa.Column("detector_version", sa.String(length=100), nullable=False),
        sa.Column(
            "detector_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            "end_ms > start_ms",
            name=op.f("ck_shots_time_range"),
        ),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_shots_sequence")),
        sa.CheckConstraint("start_ms >= 0", name=op.f("ck_shots_start_ms")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_shots_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shots")),
        sa.UniqueConstraint("asset_id", "sequence", name="asset_sequence"),
    )
    op.create_index(
        "ix_shots_asset_time",
        "shots",
        ["asset_id", "start_ms", "end_ms"],
        unique=False,
    )
    op.create_table(
        "shot_descriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            description_status_enum,
            server_default=sa.text("'pending'::description_status"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("visible_objects", postgresql.JSONB(), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("setting", sa.Text(), nullable=True),
        sa.Column("shot_type", sa.String(length=100), nullable=True),
        sa.Column("camera_motion", sa.String(length=100), nullable=True),
        sa.Column("visible_text", postgresql.JSONB(), nullable=True),
        sa.Column("uncertainty", sa.Text(), nullable=True),
        sa.Column(
            "language",
            sa.String(length=16),
            server_default=sa.text("'ru'"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["shot_id"],
            ["shots.id"],
            name=op.f("fk_shot_descriptions_shot_id_shots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shot_descriptions")),
        sa.UniqueConstraint(
            "shot_id",
            name=op.f("uq_shot_descriptions_shot_id"),
        ),
    )
    op.create_table(
        "shot_frames",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
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
            "role IN ('middle')",
            name=op.f("ck_shot_frames_role"),
        ),
        sa.CheckConstraint("height > 0", name=op.f("ck_shot_frames_height")),
        sa.CheckConstraint(
            "timestamp_ms >= 0",
            name=op.f("ck_shot_frames_timestamp_ms"),
        ),
        sa.CheckConstraint("width > 0", name=op.f("ck_shot_frames_width")),
        sa.ForeignKeyConstraint(
            ["shot_id"],
            ["shots.id"],
            name=op.f("fk_shot_frames_shot_id_shots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shot_frames")),
        sa.UniqueConstraint("artifact_path", name="artifact_path"),
        sa.UniqueConstraint("shot_id", "role", name="shot_role"),
    )


def downgrade() -> None:
    """Откатить изменения схемы."""
    op.drop_table("shot_frames")
    op.drop_table("shot_descriptions")
    op.drop_index("ix_shots_asset_time", table_name="shots")
    op.drop_table("shots")
    description_status_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"),
        "jobs",
        "job_type IN ('library_scan', 'asset_probe')",
    )
