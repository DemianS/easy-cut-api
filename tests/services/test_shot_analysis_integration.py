import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_shot_analysis_is_idempotent_and_stores_descriptions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asyncio.run(_run_shot_analysis_scenario(tmp_path, monkeypatch))


async def _run_shot_analysis_scenario(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import delete, select

    from services.asset_description import execute_asset_description
    from services.shot_detection import execute_shot_detection
    from services.shot_detector import ExtractedFrame, ShotBoundary
    from services.vision_provider import FrameObservation, VisionDescription
    from services.vision_provider import VisionSettings
    from sql.database import engine, session_scope
    from sql.enums import AssetKind, AssetStatus, JobStatus, JobType
    from sql.models import Asset, AssetLocation, Job, MediaRoot, Shot
    from sql.models import ShotDescription, ShotFrame, User

    media_root_path = tmp_path / "media"
    artifacts_path = tmp_path / "artifacts"
    media_root_path.mkdir()
    video_path = media_root_path / "video.mp4"
    video_path.write_bytes(b"test-video")
    file_stat = video_path.stat()

    monkeypatch.setenv("MEDIA_ROOT_PATH", str(media_root_path))
    monkeypatch.setenv("ARTIFACTS_PATH", str(artifacts_path))
    monkeypatch.setattr(
        "services.shot_detection.detect_shots",
        lambda *args, **kwargs: [
            ShotBoundary(0, 0, 3000),
            ShotBoundary(1, 3000, 6000),
        ],
    )

    def fake_extract(media_path, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"jpeg")
        return ExtractedFrame(kwargs["artifact_path"], 640, 360)

    monkeypatch.setattr(
        "services.shot_detection.extract_representative_frame",
        fake_extract,
    )
    monkeypatch.setattr(
        "services.asset_description.get_vision_settings",
        lambda: VisionSettings("http://ollama:11434", "vision-model", 30),
    )
    description_summary = "Человек работает с металлической деталью."

    def fake_describe(self, frames, **kwargs):
        description_data = VisionDescription(
            summary=description_summary,
            visible_objects=["человек", "деталь"],
            stable_objects=["человек", "деталь"],
            action="работает с деталью",
            setting="мастерская",
            shot_type="средний план",
            camera_motion=None,
            object_motion=None,
            visible_text=[],
            uncertainty=None,
            frame_observations=[
                FrameObservation(
                    frame_id=frame.id,
                    timestamp_ms=frame.timestamp_ms,
                    summary=description_summary,
                    visible_objects=["человек", "деталь"],
                    confidence=0.9,
                )
                for frame in frames
            ],
        )
        return description_data, {"model": self.settings.model}, "test-prompt:v2"

    monkeypatch.setattr(
        "services.asset_description.VisionDescriptionProvider.describe",
        fake_describe,
    )

    user_id = uuid4()
    media_root_id = uuid4()
    asset_id = uuid4()
    detection_job_ids = [uuid4(), uuid4()]
    description_job_id = uuid4()
    try:
        async with session_scope() as session:
            session.add(User(id=user_id, username=f"shot-analysis-{user_id.hex}"))
            await session.flush()
            session.add(
                MediaRoot(
                    id=media_root_id,
                    name="shot-analysis-integration-test",
                    container_path=str(media_root_path),
                    created_by_user_id=user_id,
                )
            )
            session.add(
                Asset(
                    id=asset_id,
                    status=AssetStatus.INDEXED,
                    media_kind=AssetKind.VIDEO.value,
                    size_bytes=file_stat.st_size,
                    duration_ms=6000,
                    width=640,
                    height=360,
                )
            )
            await session.flush()
            session.add(
                AssetLocation(
                    asset_id=asset_id,
                    media_root_id=media_root_id,
                    relative_path="video.mp4",
                    normalized_path="video.mp4",
                    is_available=True,
                    size_bytes=file_stat.st_size,
                    modified_at_ns=file_stat.st_mtime_ns,
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            for job_id in detection_job_ids:
                session.add(
                    Job(
                        id=job_id,
                        job_type=JobType.SHOT_DETECTION.value,
                        status=JobStatus.PENDING,
                        asset_id=asset_id,
                        media_root_id=media_root_id,
                    )
                )

        await execute_shot_detection(detection_job_ids[0], "celery-detection-1")
        async with session_scope() as session:
            first_shot_ids = list(
                await session.scalars(
                    select(Shot.id)
                    .where(Shot.asset_id == asset_id)
                    .order_by(Shot.sequence)
                )
            )

        await execute_shot_detection(detection_job_ids[1], "celery-detection-2")
        async with session_scope() as session:
            second_shot_ids = list(
                await session.scalars(
                    select(Shot.id)
                    .where(Shot.asset_id == asset_id)
                    .order_by(Shot.sequence)
                )
            )
            session.add(
                Job(
                    id=description_job_id,
                    job_type=JobType.ASSET_DESCRIPTION.value,
                    status=JobStatus.PENDING,
                    asset_id=asset_id,
                    media_root_id=media_root_id,
                )
            )

        assert first_shot_ids == second_shot_ids
        assert len(second_shot_ids) == 2

        async with session_scope() as session:
            frames = list(
                await session.scalars(
                    select(ShotFrame)
                    .join(Shot, Shot.id == ShotFrame.shot_id)
                    .where(Shot.asset_id == asset_id)
                    .order_by(Shot.sequence, ShotFrame.sequence)
                )
            )
        assert len(frames) == 6
        assert [frame.role for frame in frames[:3]] == ["start", "middle", "end"]
        assert all(frame.sampling_version == "multi-frame-v1" for frame in frames)
        assert all(
            frame.sampling_settings["max_frames_per_shot"] == 8 for frame in frames
        )

        result = await execute_asset_description(
            description_job_id,
            "celery-description",
        )
        async with session_scope() as session:
            asset = await session.get(Asset, asset_id)
            descriptions = list(
                await session.scalars(
                    select(ShotDescription)
                    .join(Shot, Shot.id == ShotDescription.shot_id)
                    .where(Shot.asset_id == asset_id)
                )
            )

        assert result["described"] == 2
        assert result["failed"] == 0
        assert asset.status == AssetStatus.READY
        assert {item.summary for item in descriptions} == {description_summary}
        assert all(len(item.frame_observations) == 3 for item in descriptions)
        assert all(
            item.schema_version == "shot-temporal-description-v1"
            for item in descriptions
        )
        assert all(
            item.sampling_settings["short_shot_threshold_ms"] == 1500
            for item in descriptions
        )
    finally:
        async with session_scope() as session:
            await session.execute(
                delete(Job).where(Job.id.in_([*detection_job_ids, description_job_id]))
            )
            await session.execute(
                delete(AssetLocation).where(AssetLocation.asset_id == asset_id)
            )
            await session.execute(
                delete(ShotFrame).where(
                    ShotFrame.shot_id.in_(
                        select(Shot.id).where(Shot.asset_id == asset_id)
                    )
                )
            )
            await session.execute(delete(Shot).where(Shot.asset_id == asset_id))
            await session.execute(delete(Asset).where(Asset.id == asset_id))
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
