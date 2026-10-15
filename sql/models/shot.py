"""План как точный временной интервал внутри медиаактива."""

from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer
from sqlalchemy import String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class Shot(TimestampMixin, Base):
    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint("asset_id", "sequence", name="asset_sequence"),
        CheckConstraint("sequence >= 0", name="sequence"),
        CheckConstraint("start_ms >= 0", name="start_ms"),
        CheckConstraint("end_ms > start_ms", name="time_range"),
        Index("ix_shots_asset_time", "asset_id", "start_ms", "end_ms"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
    )
    sequence: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    detector: Mapped[str] = mapped_column(String(100))
    detector_version: Mapped[str] = mapped_column(String(100))
    detector_settings: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
