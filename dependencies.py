"""Общие зависимости FastAPI."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession


async def get_session() -> AsyncIterator[AsyncSession]:
    """Лениво создать DB-сессию, не связывая `/health` с конфигурацией БД."""
    from sql.database import session_factory

    async with session_factory() as session:
        yield session
