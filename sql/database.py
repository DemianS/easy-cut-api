"""Асинхронная фабрика сессий PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sql.config import DatabaseSettings

database_settings = DatabaseSettings.from_environment()
engine = create_async_engine(
    database_settings.url,
    pool_pre_ping=True,
)
session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Выдать транзакционную сессию с rollback при ошибке."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
