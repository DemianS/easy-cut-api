"""Производный репрезентативный кадр плана."""

from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class ShotFrame(TimestampMixin, Base):
    __tablename__ = "shot_frames"
    __table_args__ = (
        UniqueConstraint("shot_id", "role", name="shot_role"),
        UniqueConstraint("artifact_path", name="artifact_path"),
        CheckConstraint("role IN ('middle')", name="role"),
        CheckConstraint("timestamp_ms >= 0", name="timestamp_ms"),
        CheckConstraint("width > 0", name="width"),
        CheckConstraint("height > 0", name="height"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    shot_id: Mapped[UUID] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"),
    )
    role: Mapped[str] = mapped_column(String(32), default="middle")
    timestamp_ms: Mapped[int] = mapped_column(BigInteger)
    artifact_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
