"""Оркестрация технического анализа одного медиаактива."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from services.media_probe import DEFAULT_FFPROBE_TIMEOUT_SECONDS
from services.media_probe import MediaProbeResult, probe_media_file
from services.file_checksum import calculate_file_checksum, FileChecksum
from services.media_scanner import resolve_media_root
from services.logging_config import log_event, LogStatus
from sql.database import session_scope
from sql.enums import AssetStatus, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot

logger = logging.getLogger(__name__)

DEFAULT_MEDIA_ROOT_PATH = "/media/library"
MAX_ERROR_MESSAGE_LENGTH = 2000
FFPROBE_TIMEOUT_ENV_NAME = "FFPROBE_TIMEOUT_SECONDS"
CHECKSUM_HEARTBEAT_INTERVAL_SECONDS = 15


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
        job.progress_total = 2
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


async def _update_probe_progress(job_id: UUID, progress_current: int) -> bool:
    """Обновить heartbeat длительного probe, если задача ещё активна."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return False
        job.progress_current = progress_current
        job.heartbeat_at = _current_time()
        return True


async def _calculate_checksum_with_heartbeat(
    job_id: UUID,
    media_path: Path,
) -> FileChecksum:
    processed_bytes = 0

    def record_progress(current_bytes: int) -> None:
        nonlocal processed_bytes
        processed_bytes = current_bytes

    checksum_task = asyncio.create_task(
        asyncio.to_thread(
            calculate_file_checksum,
            media_path,
            progress_callback=record_progress,
        )
    )
    while not checksum_task.done():
        done, _ = await asyncio.wait(
            {checksum_task},
            timeout=CHECKSUM_HEARTBEAT_INTERVAL_SECONDS,
        )
        if done:
            break
        await _update_probe_progress(job_id, 1)
        log_event(
            logger,
            logging.INFO,
            "asset_checksum",
            LogStatus.PROGRESS,
            "Расчёт checksum продолжается",
            stage="hash",
            job_id=str(job_id),
            bytes_processed=processed_bytes,
            checksum_algorithm="sha256",
        )
    return await checksum_task


async def _store_probe_result(
    job_id: UUID,
    context: ProbeContext,
    probe_result: MediaProbeResult,
    checksum: FileChecksum,
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
        remains_canonical = (
            asset.checksum_algorithm == checksum.algorithm
            and asset.checksum == checksum.digest
            and asset.duplicate_of_asset_id is None
        )
        duplicate_asset_id = None
        if not remains_canonical:
            duplicate_asset_id = await session.scalar(
                select(Asset.id)
                .where(
                    Asset.id != asset.id,
                    Asset.checksum_algorithm == checksum.algorithm,
                    Asset.checksum == checksum.digest,
                    Asset.duplicate_of_asset_id.is_(None),
                )
                .order_by(Asset.created_at, Asset.id)
                .limit(1)
            )
        asset.checksum_algorithm = checksum.algorithm
        asset.checksum = checksum.digest
        asset.duplicate_of_asset_id = duplicate_asset_id
        asset.last_error_code = None
        asset.last_error_message = None

        result = {
            "asset_id": str(asset.id),
            "duration_ms": asset.duration_ms,
            "format_name": asset.format_name,
            "has_video": asset.video_codec is not None,
            "has_audio": asset.audio_codec is not None,
            "checksum_algorithm": checksum.algorithm,
            "checksum": checksum.digest,
            "duplicate_of_asset_id": (
                str(duplicate_asset_id) if duplicate_asset_id is not None else None
            ),
        }
        finished_at = _current_time()
        job.status = JobStatus.SUCCEEDED
        job.progress_current = 2
        job.progress_total = 2
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
    started_at = monotonic()
    log_event(
        logger,
        logging.INFO,
        "asset_probe",
        LogStatus.STARTED,
        "Технический анализ медиа начат",
        stage="prepare",
        job_id=str(job_id),
    )
    try:
        context, completed_result = await _prepare_probe(job_id, celery_task_id)
        if completed_result is not None:
            log_event(
                logger,
                logging.INFO,
                "asset_probe",
                LogStatus.REUSED,
                "Готовый результат технического анализа переиспользован",
                stage="prepare",
                job_id=str(job_id),
            )
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
        await _update_probe_progress(job_id, 1)
        log_event(
            logger,
            logging.INFO,
            "asset_checksum",
            LogStatus.STARTED,
            "Расчёт полного checksum начат",
            stage="hash",
            job_id=str(job_id),
            asset_id=str(context.asset_id),
            checksum_algorithm="sha256",
        )
        checksum = await _calculate_checksum_with_heartbeat(job_id, media_path)
        _validate_file_version(media_path, context)
        if checksum.size_bytes != context.expected_size_bytes:
            raise AssetChangedDuringProbeError(
                "Размер файла изменился во время расчёта checksum"
            )
        log_event(
            logger,
            logging.INFO,
            "asset_checksum",
            LogStatus.SUCCEEDED,
            "Расчёт полного checksum завершён",
            stage="hash",
            job_id=str(job_id),
            asset_id=str(context.asset_id),
            bytes_processed=checksum.size_bytes,
            checksum_algorithm=checksum.algorithm,
        )
        result = await _store_probe_result(job_id, context, probe_result, checksum)
        log_event(
            logger,
            logging.INFO,
            "asset_probe",
            LogStatus.SUCCEEDED,
            "Технический анализ медиа успешно завершён",
            stage="store",
            job_id=str(job_id),
            asset_id=str(context.asset_id),
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        return result
    except Exception as error:
        await _mark_probe_failed(job_id, error)
        log_event(
            logger,
            logging.ERROR,
            "asset_probe",
            LogStatus.FAILED,
            "Технический анализ медиа завершился ошибкой",
            stage="execution",
            exc_info=True,
            job_id=str(job_id),
            elapsed_ms=round((monotonic() - started_at) * 1000),
            error_type=type(error).__name__,
            error_code=getattr(error, "error_code", "asset_probe_failed"),
        )
        raise
