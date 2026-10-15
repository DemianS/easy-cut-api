"""Фоновое задание сканирования или анализа."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer
from sqlalchemy import String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.enums import JobStatus, JobType
from sql.models.mixins import TimestampMixin
from sql.types import job_status_type


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ("
            "'library_scan', 'asset_probe', 'shot_detection', "
            "'asset_description', 'asset_embedding'"
            ")",
            name="job_type",
        ),
        CheckConstraint("progress_current >= 0", name="progress_current"),
        CheckConstraint(
            "progress_total IS NULL OR progress_total >= 0",
            name="progress_total",
        ),
        CheckConstraint(
            "progress_total IS NULL OR progress_current <= progress_total",
            name="progress_bounds",
        ),
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index("ix_jobs_media_root_status", "media_root_id", "status"),
        Index("ix_jobs_asset_status", "asset_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(
        String(32),
        default=JobType.LIBRARY_SCAN.value,
        server_default=text("'library_scan'"),
    )
    status: Mapped[JobStatus] = mapped_column(
        job_status_type,
        default=JobStatus.PENDING,
        server_default=text("'pending'"),
    )
    media_root_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_roots.id", ondelete="SET NULL"),
    )
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    progress_current: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    progress_total: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
