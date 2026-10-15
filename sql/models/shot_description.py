"""Структурированное локальной vision-моделью описание плана."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.enums import DescriptionStatus
from sql.models.mixins import TimestampMixin
from sql.types import description_status_type


class ShotDescription(TimestampMixin, Base):
    __tablename__ = "shot_descriptions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    shot_id: Mapped[UUID] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"),
        unique=True,
    )
    status: Mapped[DescriptionStatus] = mapped_column(
        description_status_type,
        default=DescriptionStatus.PENDING,
        server_default=text("'pending'"),
    )
    summary: Mapped[str | None] = mapped_column(Text)
    visible_objects: Mapped[list | None] = mapped_column(JSONB)
    action: Mapped[str | None] = mapped_column(Text)
    setting: Mapped[str | None] = mapped_column(Text)
    shot_type: Mapped[str | None] = mapped_column(String(100))
    camera_motion: Mapped[str | None] = mapped_column(String(100))
    visible_text: Mapped[list | None] = mapped_column(JSONB)
    uncertainty: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(
        String(16),
        default="ru",
        server_default=text("'ru'"),
    )
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
