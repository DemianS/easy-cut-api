import asyncio
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_library_preparation_advances_all_stages() -> None:
    asyncio.run(_run_pipeline_scenario())


async def _run_pipeline_scenario() -> None:
    from sqlalchemy import delete, select

    from services.job_dispatcher import reconcile_jobs
    from sql.database import engine, session_scope
    from sql.enums import AssetKind, AssetStatus, JobStatus, JobType
    from sql.models import Asset, AssetLocation, Job, MediaRoot, User

    now = datetime.now(UTC)
    user_id = uuid4()
    media_root_id = uuid4()
    pipeline_id = uuid4()
    video_id = uuid4()
    audio_id = uuid4()

    async def mark_stage_succeeded(job_type: JobType, asset_id=None) -> None:
        async with session_scope() as session:
            job = await session.scalar(
                select(Job).where(
                    Job.parent_job_id == pipeline_id,
                    Job.job_type == job_type.value,
                    Job.asset_id == asset_id,
                )
            )
            assert job is not None
            job.status = JobStatus.SUCCEEDED
            job.finished_at = datetime.now(UTC)

    try:
        async with session_scope() as session:
            session.add(User(id=user_id, username=f"pipeline-{user_id.hex}"))
            await session.flush()
            session.add(
                MediaRoot(
                    id=media_root_id,
                    name="pipeline-integration-test",
                    container_path=f"/media/{media_root_id}",
                    created_by_user_id=user_id,
                )
            )
            await session.flush()
            session.add(
                Job(
                    id=pipeline_id,
                    job_type=JobType.LIBRARY_PREPARATION.value,
                    status=JobStatus.PENDING,
                    media_root_id=media_root_id,
                )
            )

        dispatches = await reconcile_jobs()
        assert [item.task_name for item in dispatches] == ["easy_cut.library_scan"]
        await mark_stage_succeeded(JobType.LIBRARY_SCAN)

        async with session_scope() as session:
            for asset_id, media_kind, filename in (
                (video_id, AssetKind.VIDEO, "video.mp4"),
                (audio_id, AssetKind.AUDIO, "audio.wav"),
            ):
                session.add(
                    Asset(
                        id=asset_id,
                        status=AssetStatus.INDEXED,
                        media_kind=media_kind.value,
                        size_bytes=1,
                    )
                )
                await session.flush()
                session.add(
                    AssetLocation(
                        asset_id=asset_id,
                        media_root_id=media_root_id,
                        relative_path=filename,
                        normalized_path=filename,
                        is_available=True,
                        size_bytes=1,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )

        dispatches = await reconcile_jobs()
        assert {item.task_name for item in dispatches} == {"easy_cut.asset_probe"}
        assert len(dispatches) == 2
        await mark_stage_succeeded(JobType.ASSET_PROBE, video_id)
        await mark_stage_succeeded(JobType.ASSET_PROBE, audio_id)

        expected_stages = (
            JobType.SHOT_DETECTION,
            JobType.ASSET_DESCRIPTION,
            JobType.ASSET_EMBEDDING,
        )
        for stage in expected_stages:
            dispatches = await reconcile_jobs()
            assert [item.task_name for item in dispatches] == [
                f"easy_cut.{stage.value}"
            ]
            await mark_stage_succeeded(stage, video_id)

        assert await reconcile_jobs() == []
        async with session_scope() as session:
            pipeline = await session.get(Job, pipeline_id)
            assert pipeline.status == JobStatus.SUCCEEDED
            assert pipeline.progress_current == 2
            assert pipeline.progress_total == 2
            assert pipeline.result["assets_skipped_visual_analysis"] == 1
    finally:
        async with session_scope() as session:
            await session.execute(delete(Job).where(Job.id == pipeline_id))
            await session.execute(
                delete(AssetLocation).where(
                    AssetLocation.asset_id.in_([video_id, audio_id])
                )
            )
            await session.execute(
                delete(Asset).where(Asset.id.in_([video_id, audio_id]))
            )
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
