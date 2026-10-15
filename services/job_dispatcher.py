"""Идемпотентное продвижение pipeline и восстановление потерянных задач."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import os
from uuid import UUID, uuid4

from sqlalchemy import select

from sql.database import session_scope
from sql.enums import AssetKind, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job

logger = logging.getLogger(__name__)

DEFAULT_PENDING_GRACE_SECONDS = 60
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RECOVERY_ATTEMPTS = 3

TASK_NAMES = {
    JobType.LIBRARY_SCAN: "easy_cut.library_scan",
    JobType.ASSET_PROBE: "easy_cut.asset_probe",
    JobType.SHOT_DETECTION: "easy_cut.shot_detection",
    JobType.ASSET_DESCRIPTION: "easy_cut.asset_description",
    JobType.ASSET_EMBEDDING: "easy_cut.asset_embedding",
}
PIPELINE_ASSET_STAGES = (
    JobType.ASSET_PROBE,
    JobType.SHOT_DETECTION,
    JobType.ASSET_DESCRIPTION,
    JobType.ASSET_EMBEDDING,
)
ACTIVE_STATUSES = {JobStatus.PENDING, JobStatus.RUNNING}


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    job_id: UUID
    task_id: str
    task_name: str


@dataclass(frozen=True, slots=True)
class PipelineAssetDecision:
    next_stage: JobType | None = None
    terminal: bool = False
    failed: bool = False
    skipped: bool = False


def decide_asset_pipeline_stage(
    asset: Asset,
    jobs_by_stage: dict[str, Job],
) -> PipelineAssetDecision:
    """Определить следующий этап актива без обращения к инфраструктуре."""
    for stage in PIPELINE_ASSET_STAGES:
        if stage != JobType.ASSET_PROBE and asset.media_kind != AssetKind.VIDEO.value:
            return PipelineAssetDecision(terminal=True, skipped=True)
        stage_job = jobs_by_stage.get(stage.value)
        if stage_job is None:
            return PipelineAssetDecision(next_stage=stage)
        if stage_job.status in ACTIVE_STATUSES:
            return PipelineAssetDecision()
        if stage_job.status != JobStatus.SUCCEEDED:
            return PipelineAssetDecision(terminal=True, failed=True)
        if stage == JobType.ASSET_EMBEDDING:
            return PipelineAssetDecision(terminal=True)
    return PipelineAssetDecision()


def job_requires_recovery(
    job: Job,
    pending_cutoff: datetime,
    heartbeat_cutoff: datetime,
) -> bool:
    """Проверить потерю доставки или просроченный heartbeat."""
    pending_reference = job.last_dispatched_at or job.created_at
    if job.status == JobStatus.PENDING:
        return pending_reference <= pending_cutoff
    heartbeat_reference = job.heartbeat_at or job.started_at or job.created_at
    return job.status == JobStatus.RUNNING and heartbeat_reference <= heartbeat_cutoff


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _new_child_job(
    parent: Job,
    job_type: JobType,
    *,
    asset_id: UUID | None = None,
) -> tuple[Job, DispatchRequest]:
    job_id = uuid4()
    task_id = str(uuid4())
    now = datetime.now(UTC)
    job = Job(
        id=job_id,
        job_type=job_type.value,
        status=JobStatus.PENDING,
        parent_job_id=parent.id,
        media_root_id=parent.media_root_id,
        asset_id=asset_id,
        created_by_user_id=parent.created_by_user_id,
        celery_task_id=task_id,
        last_dispatched_at=now,
    )
    return job, DispatchRequest(job_id, task_id, TASK_NAMES[job_type])


async def _advance_pipeline(parent: Job) -> list[DispatchRequest]:
    dispatches: list[DispatchRequest] = []
    async with session_scope() as session:
        pipeline = await session.scalar(
            select(Job).where(Job.id == parent.id).with_for_update()
        )
        if pipeline is None or pipeline.status not in ACTIVE_STATUSES:
            return dispatches
        children = list(
            await session.scalars(
                select(Job)
                .where(Job.parent_job_id == pipeline.id)
                .order_by(Job.created_at)
            )
        )
        scan = next(
            (
                child
                for child in children
                if child.job_type == JobType.LIBRARY_SCAN.value
            ),
            None,
        )
        now = datetime.now(UTC)
        if scan is None:
            scan, dispatch = _new_child_job(pipeline, JobType.LIBRARY_SCAN)
            session.add(scan)
            pipeline.status = JobStatus.RUNNING
            pipeline.started_at = pipeline.started_at or now
            pipeline.heartbeat_at = now
            dispatches.append(dispatch)
            return dispatches
        if scan.status in ACTIVE_STATUSES:
            pipeline.status = JobStatus.RUNNING
            pipeline.heartbeat_at = now
            return dispatches
        if scan.status != JobStatus.SUCCEEDED:
            pipeline.status = JobStatus.FAILED
            pipeline.error_code = "library_scan_failed"
            pipeline.error_message = "Сканирование медиакорня завершилось ошибкой"
            pipeline.finished_at = now
            pipeline.heartbeat_at = now
            return dispatches

        assets = list(
            await session.scalars(
                select(Asset)
                .join(AssetLocation, AssetLocation.asset_id == Asset.id)
                .where(
                    AssetLocation.media_root_id == pipeline.media_root_id,
                    AssetLocation.is_available.is_(True),
                )
                .distinct()
            )
        )
        children = list(
            await session.scalars(
                select(Job)
                .where(Job.parent_job_id == pipeline.id)
                .order_by(Job.created_at)
            )
        )
        jobs_by_asset_stage = {
            (child.asset_id, child.job_type): child
            for child in children
            if child.asset_id is not None
        }
        completed = 0
        failed_asset_ids: list[str] = []
        skipped = 0

        for asset in assets:
            stage_jobs = {
                stage.value: jobs_by_asset_stage.get((asset.id, stage.value))
                for stage in PIPELINE_ASSET_STAGES
            }
            decision = decide_asset_pipeline_stage(
                asset,
                {key: value for key, value in stage_jobs.items() if value is not None},
            )
            if decision.next_stage is not None:
                stage_job, dispatch = _new_child_job(
                    pipeline,
                    decision.next_stage,
                    asset_id=asset.id,
                )
                session.add(stage_job)
                dispatches.append(dispatch)
            if decision.failed:
                failed_asset_ids.append(str(asset.id))
            if decision.skipped:
                skipped += 1
            if decision.terminal:
                completed += 1

        pipeline.status = JobStatus.RUNNING
        pipeline.progress_current = completed
        pipeline.progress_total = len(assets)
        pipeline.heartbeat_at = now
        pipeline.result = {
            "media_root_id": str(pipeline.media_root_id),
            "assets_total": len(assets),
            "assets_completed": completed,
            "assets_skipped_visual_analysis": skipped,
            "failed_asset_ids": failed_asset_ids,
        }
        if completed == len(assets):
            pipeline.finished_at = now
            if failed_asset_ids:
                pipeline.status = JobStatus.FAILED
                pipeline.error_code = "partial_library_preparation_failure"
                pipeline.error_message = "Не удалось подготовить часть активов"
            else:
                pipeline.status = JobStatus.SUCCEEDED
                pipeline.error_code = None
                pipeline.error_message = None
    return dispatches


async def _recover_stalled_jobs() -> list[DispatchRequest]:
    now = datetime.now(UTC)
    pending_cutoff = now - timedelta(
        seconds=_positive_setting(
            "JOB_PENDING_GRACE_SECONDS",
            DEFAULT_PENDING_GRACE_SECONDS,
        )
    )
    heartbeat_cutoff = now - timedelta(
        seconds=_positive_setting(
            "JOB_HEARTBEAT_TIMEOUT_SECONDS",
            DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        )
    )
    max_attempts = _positive_setting(
        "JOB_MAX_RECOVERY_ATTEMPTS",
        DEFAULT_MAX_RECOVERY_ATTEMPTS,
    )
    dispatches: list[DispatchRequest] = []
    async with session_scope() as session:
        active_jobs = list(
            await session.scalars(
                select(Job)
                .where(
                    Job.job_type != JobType.LIBRARY_PREPARATION.value,
                    Job.status.in_(ACTIVE_STATUSES),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for job in active_jobs:
            if not job_requires_recovery(job, pending_cutoff, heartbeat_cutoff):
                continue
            if job.recovery_attempts >= max_attempts:
                job.status = JobStatus.FAILED
                job.error_code = "job_recovery_exhausted"
                job.error_message = "Исчерпано число автоматических восстановлений"
                job.finished_at = now
                job.heartbeat_at = now
                continue
            job_type = JobType(job.job_type)
            task_name = TASK_NAMES.get(job_type)
            if task_name is None:
                continue
            task_id = str(uuid4())
            job.status = JobStatus.PENDING
            job.celery_task_id = task_id
            job.last_dispatched_at = now
            job.recovery_attempts += 1
            job.error_code = None
            job.error_message = None
            job.finished_at = None
            dispatches.append(DispatchRequest(job.id, task_id, task_name))
            logger.warning(
                "Фоновая задача поставлена на восстановление",
                extra={
                    "job_id": str(job.id),
                    "job_type": job.job_type,
                    "recovery_attempt": job.recovery_attempts,
                },
            )
    return dispatches


async def reconcile_jobs() -> list[DispatchRequest]:
    """Выполнить один неблокирующий проход диспетчера."""
    dispatches = await _recover_stalled_jobs()
    async with session_scope() as session:
        pipelines = list(
            await session.scalars(
                select(Job).where(
                    Job.job_type == JobType.LIBRARY_PREPARATION.value,
                    Job.status.in_(ACTIVE_STATUSES),
                )
            )
        )
    for pipeline in pipelines:
        dispatches.extend(await _advance_pipeline(pipeline))
    return dispatches


async def record_dispatch_failure(job_id: UUID) -> None:
    """Сохранить сбой broker, оставив задачу доступной для восстановления."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != JobStatus.PENDING:
            return
        job.error_code = "queue_unavailable"
        job.error_message = "Не удалось отправить задачу в очередь"
