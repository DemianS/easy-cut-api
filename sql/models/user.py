"""Пользователь локальной системы."""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from sql.base import Base
from sql.models.mixins import TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
