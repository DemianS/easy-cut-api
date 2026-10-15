"""Версионированный embedding структурированного описания плана."""

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy import Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.enums import EmbeddingStatus
from sql.models.mixins import TimestampMixin
from sql.types import embedding_status_type

EMBEDDING_DIMENSIONS = 768


class ShotEmbedding(TimestampMixin, Base):
    __tablename__ = "shot_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "shot_description_id",
            "provider",
            "model",
            "text_version",
            name="shot_description_provider_model_text_version",
        ),
        CheckConstraint("dimension > 0", name="dimension"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    shot_description_id: Mapped[UUID] = mapped_column(
        ForeignKey("shot_descriptions.id", ondelete="CASCADE"),
    )
    status: Mapped[EmbeddingStatus] = mapped_column(
        embedding_status_type,
        default=EmbeddingStatus.PENDING,
        server_default=text("'pending'"),
    )
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    text_version: Mapped[str] = mapped_column(String(100))
    dimension: Mapped[int] = mapped_column(Integer)
    indexed_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
