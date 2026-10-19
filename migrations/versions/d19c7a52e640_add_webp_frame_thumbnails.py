"""add webp frame thumbnails

Revision ID: d19c7a52e640
Revises: b82a143e6c11
Create Date: 2026-10-19 15:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d19c7a52e640"
down_revision: str | None = "b82a143e6c11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить путь к восстанавливаемому WebP-превью кадра."""
    op.add_column(
        "shot_frames",
        sa.Column("thumbnail_path", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "thumbnail_path",
        "shot_frames",
        ["thumbnail_path"],
    )


def downgrade() -> None:
    """Удалить ссылку на WebP-превью."""
    op.drop_constraint("thumbnail_path", "shot_frames", type_="unique")
    op.drop_column("shot_frames", "thumbnail_path")
