"""Физическое расположение медиаактива внутри разрешённого корня."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime
from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class AssetLocation(TimestampMixin, Base):
    __tablename__ = "asset_locations"
    __table_args__ = (
        UniqueConstraint(
            "media_root_id",
            "normalized_path",
            name="root_normalized_path",
        ),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        Index("ix_asset_locations_asset_available", "asset_id", "is_available"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
    )
    media_root_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_roots.id", ondelete="RESTRICT"),
    )
    relative_path: Mapped[str] = mapped_column(Text)
    normalized_path: Mapped[str] = mapped_column(Text)
    is_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    modified_at_ns: Mapped[int | None] = mapped_column(BigInteger)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    missing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    last_seen_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
