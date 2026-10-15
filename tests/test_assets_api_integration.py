import asyncio
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest

from routers.assets import SortOrder, get_asset, list_assets
from sql.enums import AssetKind, AssetStatus


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_list_and_get_asset_with_postgresql() -> None:
    asyncio.run(_run_asset_library_scenario())


async def _run_asset_library_scenario() -> None:
    from sqlalchemy import delete

    from sql.database import engine, session_factory, session_scope
    from sql.models import Asset, AssetLocation, MediaRoot, User

    current_time = datetime.now(UTC)
    user_id = uuid4()
    media_root_id = uuid4()
    asset_id = uuid4()
    try:
        async with session_scope() as session:
            session.add(User(id=user_id, username=f"assets-api-{user_id.hex}"))
            await session.flush()
            session.add(
                MediaRoot(
                    id=media_root_id,
                    name="assets-api-integration-test",
                    container_path=f"/media/library/{media_root_id}",
                    created_by_user_id=user_id,
                )
            )
            session.add(
                Asset(
                    id=asset_id,
                    status=AssetStatus.INDEXED,
                    media_kind=AssetKind.VIDEO.value,
                    size_bytes=2048,
                    duration_ms=5000,
                    width=1280,
                    height=720,
                )
            )
            await session.flush()
            session.add(
                AssetLocation(
                    asset_id=asset_id,
                    media_root_id=media_root_id,
                    relative_path="nested/integration-video.mp4",
                    normalized_path="nested/integration-video.mp4",
                    is_available=True,
                    size_bytes=2048,
                    modified_at_ns=1,
                    first_seen_at=current_time,
                    last_seen_at=current_time,
                )
            )

        async with session_factory() as session:
            page = await list_assets(
                asset_status=AssetStatus.INDEXED,
                media_kind=AssetKind.VIDEO,
                media_root_id=media_root_id,
                is_available=True,
                search="integration-video",
                sort_by="relative_path",
                sort_order=SortOrder.ASC,
                limit=10,
                offset=0,
                session=session,
            )
            detail = await get_asset(asset_id, session)

        assert page.total == 1
        assert page.items[0].id == asset_id
        assert page.items[0].relative_path == "nested/integration-video.mp4"
        assert detail.id == asset_id
        assert detail.locations[0].media_root_id == media_root_id
    finally:
        async with session_scope() as session:
            await session.execute(
                delete(AssetLocation).where(AssetLocation.asset_id == asset_id)
            )
            await session.execute(delete(Asset).where(Asset.id == asset_id))
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
