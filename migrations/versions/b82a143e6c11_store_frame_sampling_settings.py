"""store frame sampling settings

Revision ID: b82a143e6c11
Revises: 7f4d9c2a1b30
Create Date: 2026-10-18 01:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b82a143e6c11"
down_revision: str | None = "7f4d9c2a1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Сохранить фактические параметры выборки для воспроизводимости."""
    op.add_column(
        "shot_frames",
        sa.Column(
            "sampling_settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "shot_descriptions",
        sa.Column("sampling_settings", postgresql.JSONB(), nullable=True),
    )
    op.alter_column(
        "shot_frames",
        "sampling_version",
        server_default="multi-frame-v1",
    )


def downgrade() -> None:
    """Удалить снимок параметров sampling-политики."""
    op.alter_column(
        "shot_frames",
        "sampling_version",
        server_default="legacy-middle-v1",
    )
    op.drop_column("shot_descriptions", "sampling_settings")
    op.drop_column("shot_frames", "sampling_settings")
