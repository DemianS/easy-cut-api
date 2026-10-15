"""Разрешённый корень локальной медиатеки."""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class MediaRoot(TimestampMixin, Base):
    __tablename__ = "media_roots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    container_path: Mapped[str] = mapped_column(Text, unique=True)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
