"""Оркестрация локального описания планов одного видеоактива."""

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from services.shot_detection import DEFAULT_ARTIFACTS_PATH
from services.vision_provider import OllamaVisionProvider, VisionProviderError
from services.vision_provider import PROMPT_VERSION, get_vision_settings
from sql.database import session_scope
from sql.enums import AssetStatus, DescriptionStatus, JobStatus, JobType
from sql.models import Asset, Job, Shot, ShotDescription, ShotFrame

MAX_ERROR_MESSAGE_LENGTH = 2000


class AssetDescriptionError(RuntimeError):
    error_code = "asset_description_failed"


class InvalidAssetDescriptionJobError(AssetDescriptionError):
    error_code = "invalid_asset_description_job"


@dataclass(frozen=True, slots=True)
class DescriptionContext:
    shot_id: UUID
    artifact_path: str


def _current_time() -> datetime:
    return datetime.now(UTC)


def _resolve_artifact_path(relative_path: str) -> Path:
    root = Path(os.getenv("ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH)).resolve()
    try:
        artifact_path = (root / relative_path).resolve(strict=True)
        artifact_path.relative_to(root)
    except (OSError, ValueError) as error:
        raise AssetDescriptionError("Кадр плана недоступен") from error
    if not artifact_path.is_file():
        raise AssetDescriptionError("Кадр плана недоступен")
    return artifact_path


async def _prepare_descriptions(
    job_id: UUID,
    celery_task_id: str | None,
) -> tuple[UUID | None, list[DescriptionContext], dict | None]:
    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None or job.job_type != JobType.ASSET_DESCRIPTION.value:
            raise InvalidAssetDescriptionJobError("Задача описания не найдена")
        if job.status == JobStatus.SUCCEEDED:
            return None, [], job.result or {}
        if job.status == JobStatus.CANCELLED:
            return None, [], {"status": JobStatus.CANCELLED.value}
        if job.asset_id is None:
            raise InvalidAssetDescriptionJobError("В задаче отсутствует asset")

        asset = await session.get(Asset, job.asset_id)
        if asset is None:
            raise InvalidAssetDescriptionJobError("Медиаактив не найден")
        rows = (
            await session.execute(
                select(Shot, ShotFrame)
                .join(ShotFrame, ShotFrame.shot_id == Shot.id)
                .where(
                    Shot.asset_id == asset.id,
                    ShotFrame.role == "middle",
                )
                .order_by(Shot.sequence)
            )
        ).all()
        if not rows:
            raise InvalidAssetDescriptionJobError(
                "Сначала необходимо определить планы и кадры"
            )

        now = _current_time()
        contexts = []
        for shot, frame in rows:
            description = await session.scalar(
                select(ShotDescription).where(ShotDescription.shot_id == shot.id)
            )
            if description is None:
                description = ShotDescription(shot_id=shot.id)
                session.add(description)
            description.status = DescriptionStatus.PENDING
            description.error_code = None
            description.error_message = None
            contexts.append(DescriptionContext(shot.id, frame.artifact_path))

        job.status = JobStatus.RUNNING
        job.celery_task_id = celery_task_id or job.celery_task_id
        job.progress_current = 0
        job.progress_total = len(contexts)
        job.result = None
        job.error_code = None
        job.error_message = None
        job.started_at = now
        job.heartbeat_at = now
        job.finished_at = None
        asset.status = AssetStatus.ANALYZING
        asset.last_attempt_at = now
        return asset.id, contexts, None


async def _mark_description_running(shot_id: UUID) -> None:
    async with session_scope() as session:
        description = await session.scalar(
            select(ShotDescription).where(ShotDescription.shot_id == shot_id)
        )
        if description is not None:
            description.status = DescriptionStatus.RUNNING
            description.last_attempt_at = _current_time()


async def _store_description_success(
    job_id: UUID,
    shot_id: UUID,
    provider: OllamaVisionProvider,
    description_data,
    raw_response: dict,
) -> None:
    async with session_scope() as session:
        description = await session.scalar(
            select(ShotDescription).where(ShotDescription.shot_id == shot_id)
        )
        job = await session.get(Job, job_id)
        if description is None or job is None:
            raise InvalidAssetDescriptionJobError("Описание или задача исчезли")
        description.status = DescriptionStatus.SUCCEEDED
        description.summary = description_data.summary
        description.visible_objects = description_data.visible_objects
        description.action = description_data.action
        description.setting = description_data.setting
        description.shot_type = description_data.shot_type
        description.camera_motion = description_data.camera_motion
        description.visible_text = description_data.visible_text
        description.uncertainty = description_data.uncertainty
        description.language = "ru"
        description.provider = provider.provider_name
        description.model = provider.settings.model
        description.prompt_version = PROMPT_VERSION
        description.raw_response = raw_response
        description.error_code = None
        description.error_message = None
        job.progress_current += 1
        job.heartbeat_at = _current_time()


async def _store_description_failure(
    job_id: UUID,
    shot_id: UUID,
    error: Exception,
) -> None:
    async with session_scope() as session:
        description = await session.scalar(
            select(ShotDescription).where(ShotDescription.shot_id == shot_id)
        )
        job = await session.get(Job, job_id)
        if description is not None:
            description.status = DescriptionStatus.FAILED
            description.error_code = "vision_provider_failed"
            description.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        if job is not None:
            job.progress_current += 1
            job.heartbeat_at = _current_time()


async def _complete_descriptions(
    job_id: UUID,
    asset_id: UUID,
    failed_shot_ids: list[str],
) -> dict:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        asset = await session.get(Asset, asset_id)
        if job is None or asset is None:
            raise InvalidAssetDescriptionJobError("Задача или актив исчезли")
        result = {
            "asset_id": str(asset_id),
            "described": job.progress_current - len(failed_shot_ids),
            "failed": len(failed_shot_ids),
            "failed_shot_ids": failed_shot_ids,
        }
        now = _current_time()
        job.result = result
        job.heartbeat_at = now
        job.finished_at = now
        if failed_shot_ids:
            job.status = JobStatus.FAILED
            job.error_code = "partial_description_failure"
            job.error_message = "Не удалось описать часть планов"
            asset.status = AssetStatus.FAILED
            asset.last_error_code = job.error_code
            asset.last_error_message = job.error_message
        else:
            job.status = JobStatus.SUCCEEDED
            asset.status = AssetStatus.READY
            asset.last_error_code = None
            asset.last_error_message = None
        return result


async def _mark_job_failed(job_id: UUID, error: Exception) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {JobStatus.SUCCEEDED, JobStatus.CANCELLED}:
            return
        now = _current_time()
        job.status = JobStatus.FAILED
        job.error_code = getattr(error, "error_code", "asset_description_failed")
        job.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        job.heartbeat_at = now
        job.finished_at = now
        if job.asset_id is not None:
            asset = await session.get(Asset, job.asset_id)
            if asset is not None:
                asset.status = AssetStatus.FAILED
                asset.last_error_code = job.error_code
                asset.last_error_message = job.error_message


async def execute_asset_description(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Последовательно описать средний кадр каждого плана локальной моделью."""
    try:
        settings = get_vision_settings()
        provider = OllamaVisionProvider(settings)
        asset_id, contexts, completed_result = await _prepare_descriptions(
            job_id,
            celery_task_id,
        )
        if completed_result is not None:
            return completed_result
        if asset_id is None:
            raise InvalidAssetDescriptionJobError("Не удалось подготовить задачу")

        failed_shot_ids = []
        for context in contexts:
            await _mark_description_running(context.shot_id)
            try:
                image_path = _resolve_artifact_path(context.artifact_path)
                description, raw_response = provider.describe(image_path)
                await _store_description_success(
                    job_id,
                    context.shot_id,
                    provider,
                    description,
                    raw_response,
                )
            except (VisionProviderError, AssetDescriptionError) as error:
                failed_shot_ids.append(str(context.shot_id))
                await _store_description_failure(job_id, context.shot_id, error)
        return await _complete_descriptions(job_id, asset_id, failed_shot_ids)
    except Exception as error:
        await _mark_job_failed(job_id, error)
        raise
