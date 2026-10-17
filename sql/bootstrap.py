"""Идемпотентная инициализация локального пользователя и медиакорня."""

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from services.logging_config import configure_logging, log_event, LogStatus
from sql.database import engine, session_scope
from sql.models import MediaRoot, User

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_MEDIA_ROOT_ID = UUID("00000000-0000-4000-8000-000000000002")
DEFAULT_LOCAL_USERNAME = "local"
DEFAULT_LOCAL_DISPLAY_NAME = "Локальный пользователь"
DEFAULT_MEDIA_ROOT_NAME = "Локальная медиатека"
DEFAULT_MEDIA_ROOT_PATH = "/media/library"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    """Настройки начальных записей локальной установки."""

    enabled: bool
    user_id: UUID
    username: str
    user_display_name: str
    media_root_id: UUID
    media_root_name: str
    media_root_path: str

    @classmethod
    def from_environment(cls) -> "BootstrapSettings":
        """Прочитать bootstrap-настройки из окружения."""
        enabled = os.getenv("BOOTSTRAP_MEDIA_ROOT", "true").strip().lower()
        media_root_path = _normalize_media_root_path(
            os.getenv("MEDIA_ROOT_PATH", DEFAULT_MEDIA_ROOT_PATH)
        )
        return cls(
            enabled=enabled in TRUE_VALUES,
            user_id=UUID(
                os.getenv("BOOTSTRAP_LOCAL_USER_ID", str(DEFAULT_LOCAL_USER_ID))
            ),
            username=os.getenv("BOOTSTRAP_LOCAL_USERNAME", DEFAULT_LOCAL_USERNAME),
            user_display_name=os.getenv(
                "BOOTSTRAP_LOCAL_USER_DISPLAY_NAME",
                DEFAULT_LOCAL_DISPLAY_NAME,
            ),
            media_root_id=UUID(
                os.getenv("BOOTSTRAP_MEDIA_ROOT_ID", str(DEFAULT_MEDIA_ROOT_ID))
            ),
            media_root_name=os.getenv(
                "BOOTSTRAP_MEDIA_ROOT_NAME",
                DEFAULT_MEDIA_ROOT_NAME,
            ),
            media_root_path=media_root_path,
        )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Результат создания или повторного использования начальных записей."""

    user_id: UUID
    media_root_id: UUID
    user_created: bool
    media_root_created: bool


def _normalize_media_root_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path.strip())
    if not path.is_absolute() or path == PurePosixPath("/"):
        raise ValueError("MEDIA_ROOT_PATH должен быть абсолютным путём не равным корню")
    return str(path)


async def ensure_local_media_root(
    session: AsyncSession,
    settings: BootstrapSettings,
) -> BootstrapResult:
    """Создать локального пользователя и медиакорень без дублей."""
    created_user_id = await session.scalar(
        insert(User)
        .values(
            id=settings.user_id,
            username=settings.username,
            display_name=settings.user_display_name,
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=[User.username])
        .returning(User.id)
    )
    user_id = created_user_id or await session.scalar(
        select(User.id).where(User.username == settings.username)
    )
    if user_id is None:
        raise RuntimeError("Не удалось получить локального пользователя")

    created_media_root_id = await session.scalar(
        insert(MediaRoot)
        .values(
            id=settings.media_root_id,
            name=settings.media_root_name,
            container_path=settings.media_root_path,
            is_enabled=True,
            created_by_user_id=user_id,
        )
        .on_conflict_do_nothing(index_elements=[MediaRoot.container_path])
        .returning(MediaRoot.id)
    )
    media_root_id = created_media_root_id or await session.scalar(
        select(MediaRoot.id).where(MediaRoot.container_path == settings.media_root_path)
    )
    if media_root_id is None:
        raise RuntimeError("Не удалось получить локальный медиакорень")

    return BootstrapResult(
        user_id=user_id,
        media_root_id=media_root_id,
        user_created=created_user_id is not None,
        media_root_created=created_media_root_id is not None,
    )


async def run_bootstrap(settings: BootstrapSettings | None = None) -> None:
    """Выполнить bootstrap и записать его итог в структурированный лог."""
    settings = settings or BootstrapSettings.from_environment()
    if not settings.enabled:
        log_event(
            logger,
            logging.INFO,
            "media_root_bootstrap",
            LogStatus.SKIPPED,
            "Bootstrap локального медиакорня отключён",
            stage="configuration",
        )
        return

    log_event(
        logger,
        logging.INFO,
        "media_root_bootstrap",
        LogStatus.STARTED,
        "Bootstrap локального медиакорня начат",
        stage="database",
    )
    try:
        async with session_scope() as session:
            result = await ensure_local_media_root(session, settings)
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "media_root_bootstrap",
            LogStatus.FAILED,
            "Bootstrap локального медиакорня завершился ошибкой",
            stage="database",
            exc_info=True,
            error_type=type(error).__name__,
        )
        raise

    log_event(
        logger,
        logging.INFO,
        "media_root_bootstrap",
        LogStatus.SUCCEEDED,
        "Локальный медиакорень готов к работе",
        stage="database",
        media_root_id=str(result.media_root_id),
        user_id=str(result.user_id),
        items_created=int(result.user_created) + int(result.media_root_created),
    )


def main() -> None:
    """Запустить bootstrap как отдельную команду контейнера."""
    configure_logging()
    asyncio.run(_run_and_dispose())


async def _run_and_dispose() -> None:
    try:
        await run_bootstrap()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
