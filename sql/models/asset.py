"""Логический медиаактив и его основные технические характеристики."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy import Integer
from sqlalchemy import String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.enums import AssetKind, AssetStatus
from sql.models.mixins import TimestampMixin
from sql.types import asset_status_type


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "media_kind IN ('unknown', 'video', 'image', 'audio')",
            name="media_kind",
        ),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="width"),
        CheckConstraint("height IS NULL OR height > 0", name="height"),
        CheckConstraint(
            "((frame_rate_num IS NULL AND frame_rate_den IS NULL) OR "
            "(frame_rate_num > 0 AND frame_rate_den > 0))",
            name="frame_rate",
        ),
        CheckConstraint(
            "sample_rate_hz IS NULL OR sample_rate_hz > 0",
            name="sample_rate_hz",
        ),
        CheckConstraint(
            "audio_channels IS NULL OR audio_channels > 0",
            name="audio_channels",
        ),
        CheckConstraint(
            "((checksum_algorithm IS NULL AND checksum IS NULL) OR "
            "(checksum_algorithm IS NOT NULL AND checksum IS NOT NULL))",
            name="checksum_pair",
        ),
        Index("ix_assets_checksum", "checksum_algorithm", "checksum"),
        Index("ix_assets_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    status: Mapped[AssetStatus] = mapped_column(
        asset_status_type,
        default=AssetStatus.DISCOVERED,
        server_default=text("'discovered'"),
    )
    media_kind: Mapped[str] = mapped_column(
        String(16),
        default=AssetKind.UNKNOWN.value,
        server_default=text("'unknown'"),
    )
    format_name: Mapped[str | None] = mapped_column(String(100))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    checksum_algorithm: Mapped[str | None] = mapped_column(String(32))
    checksum: Mapped[str | None] = mapped_column(String(128))
    duplicate_of_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    video_codec: Mapped[str | None] = mapped_column(String(100))
    frame_rate_num: Mapped[int | None] = mapped_column(Integer)
    frame_rate_den: Mapped[int | None] = mapped_column(Integer)
    audio_codec: Mapped[str | None] = mapped_column(String(100))
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    audio_channels: Mapped[int | None] = mapped_column(Integer)
    probe_metadata: Mapped[dict | None] = mapped_column(JSONB)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
