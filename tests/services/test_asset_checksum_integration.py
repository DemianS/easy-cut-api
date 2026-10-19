import asyncio
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_asset_probe_uses_content_to_mark_only_real_duplicate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from services.media_probe import MediaProbeResult

    content = b"same-video-content"
    different_content = b"different-content!"
    assert len(different_content) == len(content)
    (tmp_path / "first.mp4").write_bytes(content)
    (tmp_path / "second.mp4").write_bytes(content)
    (tmp_path / "different.mp4").write_bytes(different_content)
    monkeypatch.setenv("MEDIA_ROOT_PATH", str(tmp_path))
    monkeypatch.setattr(
        "services.asset_probe.probe_media_file",
        lambda media_path, timeout_seconds: MediaProbeResult(
            format_name="mp4",
            duration_ms=1000,
            width=1920,
            height=1080,
            video_codec="h264",
            frame_rate_num=25,
            frame_rate_den=1,
            audio_codec=None,
            sample_rate_hz=None,
            audio_channels=None,
            metadata={"format": {}, "streams": []},
        ),
    )

    asyncio.run(_run_duplicate_scenario(tmp_path, content))


async def _run_duplicate_scenario(media_path: Path, content: bytes) -> None:
    from sqlalchemy import delete

    from services.asset_probe import execute_asset_probe
    from sql.database import engine, session_scope
    from sql.enums import JobType
    from sql.models import Asset, AssetLocation, Job, MediaRoot, User

    now = datetime.now(UTC)
    user_id = uuid4()
    media_root_id = uuid4()
    asset_ids = [uuid4(), uuid4(), uuid4()]
    job_ids = [uuid4(), uuid4(), uuid4()]

    try:
        async with session_scope() as session:
            session.add(User(id=user_id, username=f"checksum-{user_id.hex}"))
            await session.flush()
            session.add(
                MediaRoot(
                    id=media_root_id,
                    name="checksum-test",
                    container_path=str(media_path),
                    created_by_user_id=user_id,
                )
            )
            await session.flush()
            for asset_id, job_id, filename in zip(
                asset_ids,
                job_ids,
                ("first.mp4", "second.mp4", "different.mp4"),
                strict=True,
            ):
                file_stat = (media_path / filename).stat()
                session.add(Asset(id=asset_id, size_bytes=file_stat.st_size))
                session.add(
                    AssetLocation(
                        asset_id=asset_id,
                        media_root_id=media_root_id,
                        relative_path=filename,
                        normalized_path=filename,
                        size_bytes=file_stat.st_size,
                        modified_at_ns=file_stat.st_mtime_ns,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                session.add(
                    Job(
                        id=job_id,
                        job_type=JobType.ASSET_PROBE.value,
                        media_root_id=media_root_id,
                        asset_id=asset_id,
                    )
                )

        first_result = await execute_asset_probe(job_ids[0], "checksum-task-1")
        second_result = await execute_asset_probe(job_ids[1], "checksum-task-2")
        different_result = await execute_asset_probe(
            job_ids[2],
            "checksum-task-3",
        )
        expected_checksum = hashlib.sha256(content).hexdigest()

        assert first_result["checksum"] == expected_checksum
        assert first_result["duplicate_of_asset_id"] is None
        assert second_result["checksum"] == expected_checksum
        assert second_result["duplicate_of_asset_id"] == str(asset_ids[0])
        assert different_result["checksum"] != expected_checksum
        assert different_result["duplicate_of_asset_id"] is None
    finally:
        async with session_scope() as session:
            await session.execute(delete(Job).where(Job.id.in_(job_ids)))
            await session.execute(
                delete(AssetLocation).where(AssetLocation.asset_id.in_(asset_ids))
            )
            await session.execute(delete(Asset).where(Asset.id.in_(asset_ids)))
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
