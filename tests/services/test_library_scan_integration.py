import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_library_scan_is_idempotent_and_marks_missing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEDIA_ROOT_PATH", str(tmp_path))
    (tmp_path / "clip.mp4").write_bytes(b"video")
    missing_file = tmp_path / "sound.wav"
    missing_file.write_bytes(b"audio")

    asyncio.run(_run_library_scan_scenario(tmp_path, missing_file))


async def _run_library_scan_scenario(
    media_path: Path,
    missing_file: Path,
) -> None:
    from sqlalchemy import delete, select

    from services.library_scan import execute_library_scan
    from sql.database import engine, session_scope
    from sql.models import Asset, AssetLocation, Job, MediaRoot, User

    user_id = uuid4()
    media_root_id = uuid4()
    job_ids = [uuid4(), uuid4(), uuid4()]

    try:
        async with session_scope() as session:
            user = User(
                id=user_id,
                username=f"library-scan-{user_id.hex}",
            )
            session.add(user)
            await session.flush()
            media_root = MediaRoot(
                id=media_root_id,
                name="integration-test",
                container_path=str(media_path),
                created_by_user_id=user_id,
            )
            session.add(media_root)
            await session.flush()
            session.add(
                Job(
                    id=job_ids[0],
                    media_root_id=media_root_id,
                    created_by_user_id=user_id,
                )
            )

        first_result = await execute_library_scan(job_ids[0], "test-task-1")
        assert first_result["created"] == 2
        assert first_result["missing"] == 0

        async with session_scope() as session:
            session.add(
                Job(
                    id=job_ids[1],
                    media_root_id=media_root_id,
                    created_by_user_id=user_id,
                )
            )

        second_result = await execute_library_scan(job_ids[1], "test-task-2")
        assert second_result["created"] == 0
        assert second_result["unchanged"] == 2

        missing_file.unlink()
        async with session_scope() as session:
            session.add(
                Job(
                    id=job_ids[2],
                    media_root_id=media_root_id,
                    created_by_user_id=user_id,
                )
            )

        third_result = await execute_library_scan(job_ids[2], "test-task-3")
        assert third_result["missing"] == 1

        async with session_scope() as session:
            locations = list(
                await session.scalars(
                    select(AssetLocation).where(
                        AssetLocation.media_root_id == media_root_id
                    )
                )
            )
            unavailable_locations = [
                location for location in locations if not location.is_available
            ]
            assert len(locations) == 2
            assert len(unavailable_locations) == 1
            missing_asset = await session.get(
                Asset,
                unavailable_locations[0].asset_id,
            )
            assert missing_asset is not None
            assert missing_asset.status == "missing"
    finally:
        async with session_scope() as session:
            asset_ids = set(
                await session.scalars(
                    select(AssetLocation.asset_id).where(
                        AssetLocation.media_root_id == media_root_id
                    )
                )
            )
            await session.execute(
                delete(AssetLocation).where(
                    AssetLocation.media_root_id == media_root_id
                )
            )
            await session.execute(delete(Job).where(Job.id.in_(job_ids)))
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            if asset_ids:
                await session.execute(delete(Asset).where(Asset.id.in_(asset_ids)))
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
