"""Производный репрезентативный кадр плана."""

from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy import Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class ShotFrame(TimestampMixin, Base):
    __tablename__ = "shot_frames"
    __table_args__ = (
        UniqueConstraint("shot_id", "sequence", name="shot_sequence"),
        UniqueConstraint("shot_id", "timestamp_ms", name="shot_timestamp"),
        UniqueConstraint("artifact_path", name="artifact_path"),
        UniqueConstraint("thumbnail_path", name="thumbnail_path"),
        CheckConstraint(
            "role IN ('single', 'start', 'middle', 'sample', 'end')",
            name="role",
        ),
        CheckConstraint("sequence >= 0", name="sequence"),
        CheckConstraint("timestamp_ms >= 0", name="timestamp_ms"),
        CheckConstraint("width > 0", name="width"),
        CheckConstraint("height > 0", name="height"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    shot_id: Mapped[UUID] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"),
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    timestamp_ms: Mapped[int] = mapped_column(BigInteger)
    selection_reason: Mapped[str] = mapped_column(Text)
    sampling_version: Mapped[str] = mapped_column(
        String(100),
        server_default=text("'multi-frame-v1'"),
    )
    sampling_settings: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    artifact_path: Mapped[str] = mapped_column(Text)
    thumbnail_path: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
