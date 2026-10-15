from datetime import UTC, datetime, timedelta

from services.job_dispatcher import decide_asset_pipeline_stage
from services.job_dispatcher import job_requires_recovery
from sql.enums import AssetKind, JobStatus, JobType
from sql.models import Asset, Job


def _job(job_type: JobType, status: JobStatus) -> Job:
    return Job(job_type=job_type.value, status=status)


def test_pipeline_advances_video_one_stage_at_a_time() -> None:
    asset = Asset(media_kind=AssetKind.VIDEO.value)
    jobs = {
        JobType.ASSET_PROBE.value: _job(
            JobType.ASSET_PROBE,
            JobStatus.SUCCEEDED,
        )
    }

    decision = decide_asset_pipeline_stage(asset, jobs)

    assert decision.next_stage == JobType.SHOT_DETECTION
    assert decision.terminal is False


def test_pipeline_finishes_non_video_after_probe() -> None:
    asset = Asset(media_kind=AssetKind.AUDIO.value)
    jobs = {
        JobType.ASSET_PROBE.value: _job(
            JobType.ASSET_PROBE,
            JobStatus.SUCCEEDED,
        )
    }

    decision = decide_asset_pipeline_stage(asset, jobs)

    assert decision.terminal is True
    assert decision.skipped is True


def test_pipeline_stops_failed_asset() -> None:
    asset = Asset(media_kind=AssetKind.VIDEO.value)
    jobs = {
        JobType.ASSET_PROBE.value: _job(
            JobType.ASSET_PROBE,
            JobStatus.FAILED,
        )
    }

    decision = decide_asset_pipeline_stage(asset, jobs)

    assert decision.terminal is True
    assert decision.failed is True


def test_recovery_uses_dispatch_and_heartbeat_deadlines() -> None:
    now = datetime.now(UTC)
    pending = _job(JobType.ASSET_PROBE, JobStatus.PENDING)
    pending.created_at = now - timedelta(minutes=2)
    running = _job(JobType.SHOT_DETECTION, JobStatus.RUNNING)
    running.created_at = now - timedelta(minutes=20)
    running.heartbeat_at = now - timedelta(minutes=1)

    assert job_requires_recovery(
        pending,
        now - timedelta(minutes=1),
        now - timedelta(minutes=10),
    )
    assert not job_requires_recovery(
        running,
        now - timedelta(minutes=1),
        now - timedelta(minutes=10),
    )
