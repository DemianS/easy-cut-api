"""Оркестрация технического анализа одного медиаактива."""

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from services.media_probe import DEFAULT_FFPROBE_TIMEOUT_SECONDS
from services.media_probe import MediaProbeResult, probe_media_file
from services.media_scanner import resolve_media_root
from sql.database import session_scope
from sql.enums import AssetStatus, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot

DEFAULT_MEDIA_ROOT_PATH = "/media/library"
MAX_ERROR_MESSAGE_LENGTH = 2000
FFPROBE_TIMEOUT_ENV_NAME = "FFPROBE_TIMEOUT_SECONDS"


class AssetProbeError(RuntimeError):
    """Базовая ошибка оркестрации asset_probe."""

    error_code = "asset_probe_failed"


class AssetProbeJobNotFoundError(AssetProbeError):
    error_code = "job_not_found"


class InvalidAssetProbeJobError(AssetProbeError):
    error_code = "invalid_asset_probe_job"


class MediaFileUnavailableError(AssetProbeError):
    error_code = "media_file_unavailable"


class AssetChangedDuringProbeError(AssetProbeError):
    error_code = "asset_changed_during_probe"


@dataclass(frozen=True, slots=True)
class ProbeContext:
    asset_id: UUID
    media_root_path: str
    relative_path: str
    expected_size_bytes: int
    expected_modified_at_ns: int | None


def _current_time() -> datetime:
    return datetime.now(UTC)


def _ffprobe_timeout_seconds() -> int:
    raw_timeout = os.getenv(
        FFPROBE_TIMEOUT_ENV_NAME,
        str(DEFAULT_FFPROBE_TIMEOUT_SECONDS),
    )
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as error:
        raise AssetProbeError("Некорректный таймаут ffprobe") from error
    if timeout_seconds <= 0:
        raise AssetProbeError("Таймаут ffprobe должен быть положительным")
    return timeout_seconds


async def _prepare_probe(
    job_id: UUID,
    celery_task_id: str | None,
) -> tuple[ProbeContext | None, dict | None]:
    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None:
            raise AssetProbeJobNotFoundError("Задача анализа не найдена")
        if job.job_type != JobType.ASSET_PROBE.value:
            raise InvalidAssetProbeJobError("Задача имеет тип, отличный от asset_probe")
        if job.status == JobStatus.SUCCEEDED:
            return None, job.result or {}
        if job.status == JobStatus.CANCELLED:
            return None, {"status": JobStatus.CANCELLED.value}
        if job.asset_id is None or job.media_root_id is None:
            raise InvalidAssetProbeJobError(
                "Для задачи не заданы asset_id или media_root_id"
            )

        asset = await session.get(Asset, job.asset_id)
        media_root = await session.get(MediaRoot, job.media_root_id)
        if asset is None or media_root is None or not media_root.is_enabled:
            raise MediaFileUnavailableError("Актив или корень медиатеки недоступен")
        location = await session.scalar(
            select(AssetLocation)
            .where(
                AssetLocation.asset_id == job.asset_id,
                AssetLocation.media_root_id == job.media_root_id,
                AssetLocation.is_available.is_(True),
            )
            .order_by(AssetLocation.relative_path)
            .limit(1)
        )
        if location is None:
            raise MediaFileUnavailableError("Доступное расположение актива не найдено")

        current_time = _current_time()
        job.status = JobStatus.RUNNING
        job.celery_task_id = celery_task_id or job.celery_task_id
        job.progress_current = 0
        job.progress_total = 1
        job.result = None
        job.error_code = None
        job.error_message = None
        job.started_at = current_time
        job.heartbeat_at = current_time
        job.finished_at = None
        asset.status = AssetStatus.PROBING
        asset.last_attempt_at = current_time
        asset.last_error_code = None
        asset.last_error_message = None
        return (
            ProbeContext(
                asset_id=asset.id,
                media_root_path=media_root.container_path,
                relative_path=location.relative_path,
                expected_size_bytes=location.size_bytes,
                expected_modified_at_ns=location.modified_at_ns,
            ),
            None,
        )


def _resolve_media_path(context: ProbeContext) -> Path:
    allowed_root_path = Path(os.getenv("MEDIA_ROOT_PATH", DEFAULT_MEDIA_ROOT_PATH))
    media_root = resolve_media_root(
        Path(context.media_root_path),
        allowed_root_path,
    )
    relative_path = Path(context.relative_path)
    if relative_path.is_absolute():
        raise MediaFileUnavailableError("Путь актива должен быть относительным")
    unresolved_media_path = media_root / relative_path
    if unresolved_media_path.is_symlink():
        raise MediaFileUnavailableError("Символическая ссылка не разрешена")
    try:
        media_path = unresolved_media_path.resolve(strict=True)
        media_path.relative_to(media_root)
    except (OSError, ValueError) as error:
        raise MediaFileUnavailableError("Медиафайл недоступен") from error
    if not media_path.is_file():
        raise MediaFileUnavailableError("Путь актива не является файлом")
    return media_path


def _validate_file_version(media_path: Path, context: ProbeContext) -> None:
    try:
        file_stat = media_path.stat()
    except OSError as error:
        raise MediaFileUnavailableError("Медиафайл недоступен") from error
    if (
        file_stat.st_size != context.expected_size_bytes
        or context.expected_modified_at_ns is not None
        and file_stat.st_mtime_ns != context.expected_modified_at_ns
    ):
        raise AssetChangedDuringProbeError(
            "Файл изменился после сканирования медиатеки"
        )


async def _store_probe_result(
    job_id: UUID,
    context: ProbeContext,
    probe_result: MediaProbeResult,
) -> dict:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        asset = await session.get(Asset, context.asset_id)
        if job is None or asset is None:
            raise AssetProbeJobNotFoundError("Задача или актив не найдены")
        if job.status == JobStatus.CANCELLED:
            asset.status = AssetStatus.DISCOVERED
            return {"status": JobStatus.CANCELLED.value}

        asset.status = AssetStatus.INDEXED
        asset.format_name = probe_result.format_name or asset.format_name
        asset.duration_ms = probe_result.duration_ms
        asset.width = probe_result.width
        asset.height = probe_result.height
        asset.video_codec = probe_result.video_codec
        asset.frame_rate_num = probe_result.frame_rate_num
        asset.frame_rate_den = probe_result.frame_rate_den
        asset.audio_codec = probe_result.audio_codec
        asset.sample_rate_hz = probe_result.sample_rate_hz
        asset.audio_channels = probe_result.audio_channels
        asset.probe_metadata = probe_result.metadata
        asset.last_error_code = None
        asset.last_error_message = None

        result = {
            "asset_id": str(asset.id),
            "duration_ms": asset.duration_ms,
            "format_name": asset.format_name,
            "has_video": asset.video_codec is not None,
            "has_audio": asset.audio_codec is not None,
        }
        finished_at = _current_time()
        job.status = JobStatus.SUCCEEDED
        job.progress_current = 1
        job.progress_total = 1
        job.result = result
        job.heartbeat_at = finished_at
        job.finished_at = finished_at
        return result


async def _mark_probe_failed(job_id: UUID, error: Exception) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {
            JobStatus.CANCELLED,
            JobStatus.SUCCEEDED,
        }:
            return
        error_code = getattr(error, "error_code", "asset_probe_failed")
        error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        finished_at = _current_time()
        job.status = JobStatus.FAILED
        job.error_code = error_code
        job.error_message = error_message
        job.heartbeat_at = finished_at
        job.finished_at = finished_at
        if job.asset_id is not None:
            asset = await session.get(Asset, job.asset_id)
            if asset is not None:
                asset.status = (
                    AssetStatus.MISSING
                    if isinstance(error, MediaFileUnavailableError)
                    else AssetStatus.FAILED
                )
                asset.last_error_code = error_code
                asset.last_error_message = error_message
            if (
                isinstance(error, MediaFileUnavailableError)
                and job.media_root_id is not None
            ):
                location = await session.scalar(
                    select(AssetLocation).where(
                        AssetLocation.asset_id == job.asset_id,
                        AssetLocation.media_root_id == job.media_root_id,
                        AssetLocation.is_available.is_(True),
                    )
                )
                if location is not None:
                    location.is_available = False
                    location.missing_since = finished_at


async def mark_probe_dispatch_failed(job_id: UUID) -> None:
    """Пометить probe-job ошибочной, если её не удалось отправить в Redis."""
    await _mark_probe_failed(
        job_id,
        AssetProbeError("Не удалось отправить asset_probe в очередь"),
    )


async def execute_asset_probe(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Проверить версию файла, вызвать ffprobe и сохранить результат."""
    try:
        context, completed_result = await _prepare_probe(job_id, celery_task_id)
        if completed_result is not None:
            return completed_result
        if context is None:
            raise InvalidAssetProbeJobError("Не удалось подготовить задачу анализа")

        media_path = _resolve_media_path(context)
        _validate_file_version(media_path, context)
        probe_result = probe_media_file(
            media_path,
            timeout_seconds=_ffprobe_timeout_seconds(),
        )
        _validate_file_version(media_path, context)
        return await _store_probe_result(job_id, context, probe_result)
    except Exception as error:
        await _mark_probe_failed(job_id, error)
        raise
