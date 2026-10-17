import asyncio
import os
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_media_root_bootstrap_is_idempotent() -> None:
    asyncio.run(_run_bootstrap_scenario())


async def _run_bootstrap_scenario() -> None:
    from sqlalchemy import delete, func, select

    from sql.bootstrap import BootstrapSettings, ensure_local_media_root
    from sql.database import engine, session_scope
    from sql.models import MediaRoot, User

    test_suffix = uuid4().hex
    user_id = uuid4()
    media_root_id = uuid4()
    username = f"bootstrap-{test_suffix}"
    media_root_path = f"/media/bootstrap-{test_suffix}"
    settings = BootstrapSettings(
        enabled=True,
        user_id=user_id,
        username=username,
        user_display_name="Bootstrap test",
        media_root_id=media_root_id,
        media_root_name="Bootstrap test root",
        media_root_path=media_root_path,
    )

    try:
        async with session_scope() as session:
            first_result = await ensure_local_media_root(session, settings)
        async with session_scope() as session:
            second_result = await ensure_local_media_root(session, settings)
            user_count = await session.scalar(
                select(func.count()).select_from(User).where(User.username == username)
            )
            media_root_count = await session.scalar(
                select(func.count())
                .select_from(MediaRoot)
                .where(MediaRoot.container_path == media_root_path)
            )

        assert first_result.user_id == user_id
        assert first_result.media_root_id == media_root_id
        assert first_result.user_created is True
        assert first_result.media_root_created is True
        assert second_result.user_id == user_id
        assert second_result.media_root_id == media_root_id
        assert second_result.user_created is False
        assert second_result.media_root_created is False
        assert user_count == 1
        assert media_root_count == 1
    finally:
        async with session_scope() as session:
            await session.execute(
                delete(MediaRoot).where(MediaRoot.container_path == media_root_path)
            )
            await session.execute(delete(User).where(User.username == username))
        await engine.dispose()
