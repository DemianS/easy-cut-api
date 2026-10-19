"""add multiframe temporal analysis

Revision ID: 7f4d9c2a1b30
Revises: c481d720c929
Create Date: 2026-10-18 01:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "7f4d9c2a1b30"
down_revision: str | None = "c481d720c929"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Расширить кадры и описания временным контрактом."""
    op.add_column(
        "shot_frames",
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "shot_frames",
        sa.Column(
            "selection_reason",
            sa.Text(),
            server_default="legacy middle frame",
            nullable=False,
        ),
    )
    op.add_column(
        "shot_frames",
        sa.Column(
            "sampling_version",
            sa.String(length=100),
            server_default="legacy-middle-v1",
            nullable=False,
        ),
    )
    op.drop_constraint("shot_role", "shot_frames", type_="unique")
    op.drop_constraint(
        op.f("ck_shot_frames_role"),
        "shot_frames",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_shot_frames_role"),
        "shot_frames",
        "role IN ('single', 'start', 'middle', 'sample', 'end')",
    )
    op.create_check_constraint(
        op.f("ck_shot_frames_sequence"),
        "shot_frames",
        "sequence >= 0",
    )
    op.create_unique_constraint("shot_sequence", "shot_frames", ["shot_id", "sequence"])
    op.create_unique_constraint(
        "shot_timestamp", "shot_frames", ["shot_id", "timestamp_ms"]
    )
    op.alter_column("shot_frames", "sequence", server_default=None)
    op.alter_column("shot_frames", "selection_reason", server_default=None)
    op.alter_column(
        "shot_frames",
        "sampling_version",
        server_default="multi-frame-v1",
    )

    for column_name, column_type in (
        ("stable_objects", postgresql.JSONB()),
        ("object_motion", sa.Text()),
        ("frame_observations", postgresql.JSONB()),
        ("frame_transitions", postgresql.JSONB()),
        ("temporal_events", postgresql.JSONB()),
        ("field_evidence", postgresql.JSONB()),
        ("schema_version", sa.String(length=100)),
        ("sampling_version", sa.String(length=100)),
    ):
        op.add_column(
            "shot_descriptions",
            sa.Column(column_name, column_type, nullable=True),
        )


def downgrade() -> None:
    """Вернуть одиночный средний кадр и прежнюю схему описания."""
    for column_name in (
        "sampling_version",
        "schema_version",
        "field_evidence",
        "temporal_events",
        "frame_transitions",
        "frame_observations",
        "object_motion",
        "stable_objects",
    ):
        op.drop_column("shot_descriptions", column_name)

    op.drop_constraint("shot_timestamp", "shot_frames", type_="unique")
    op.drop_constraint("shot_sequence", "shot_frames", type_="unique")
    op.drop_constraint(op.f("ck_shot_frames_sequence"), "shot_frames", type_="check")
    op.drop_constraint(op.f("ck_shot_frames_role"), "shot_frames", type_="check")
    op.execute("DELETE FROM shot_frames WHERE sequence <> 0")
    op.execute("UPDATE shot_frames SET role = 'middle'")
    op.create_check_constraint(
        op.f("ck_shot_frames_role"), "shot_frames", "role IN ('middle')"
    )
    op.create_unique_constraint("shot_role", "shot_frames", ["shot_id", "role"])
    op.drop_column("shot_frames", "sampling_version")
    op.drop_column("shot_frames", "selection_reason")
    op.drop_column("shot_frames", "sequence")
