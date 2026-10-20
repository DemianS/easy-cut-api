"""Сохраняемые запросы монтажа и неизменяемые версии timeline."""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))


class MontageRun(TimestampMixin, Base):
    __tablename__ = "montage_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    request: Mapped[dict] = mapped_column(JSONB)
    search_plan: Mapped[dict | None] = mapped_column(JSONB)
    candidates: Mapped[list | None] = mapped_column(JSONB)
    selection: Mapped[dict | None] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    output_path: Mapped[str | None] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    current_timeline_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "timeline_versions.id",
            use_alter=True,
            name="fk_montage_runs_current_timeline_version_id_timeline_versions",
        )
    )


class TimelineVersion(TimestampMixin, Base):
    __tablename__ = "timeline_versions"
    __table_args__ = (CheckConstraint("source IN ('llm', 'user')", name="source"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    montage_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("montage_runs.id"),
        index=True,
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("timeline_versions.id"),
    )
    timeline: Mapped[dict] = mapped_column(JSONB)
    selection: Mapped[dict | None] = mapped_column(JSONB)
    candidates: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    source: Mapped[str] = mapped_column(String(16), default="llm", server_default="llm")


class MontageRender(TimestampMixin, Base):
    __tablename__ = "montage_renders"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    montage_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("montage_runs.id"), index=True
    )
    timeline_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("timeline_versions.id"), index=True
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    output_path: Mapped[str | None] = mapped_column(Text)
