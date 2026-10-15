"""Оркестрация задачи сканирования медиатеки через PostgreSQL."""

from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from services.media_scanner import MediaScanResult
from services.media_scanner import discover_media_files
from sql.database import session_scope
from sql.enums import AssetStatus, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot

DEFAULT_MEDIA_ROOT_PATH = "/media/library"
PROGRESS_BATCH_SIZE = 100
MAX_ERROR_MESSAGE_LENGTH = 2000


class LibraryScanError(RuntimeError):
    """Базовая ошибка выполнения library_scan."""

    error_code = "library_scan_failed"


class LibraryScanJobNotFoundError(LibraryScanError):
    error_code = "job_not_found"


class InvalidLibraryScanJobError(LibraryScanError):
    error_code = "invalid_library_scan_job"


class MediaRootNotFoundError(LibraryScanError):
    error_code = "media_root_not_found"


class MediaRootDisabledError(LibraryScanError):
    error_code = "media_root_disabled"


def _current_time() -> datetime:
    return datetime.now(UTC)


def _reset_asset_after_file_change(asset: Asset) -> None:
    """Сбросить производные данные, ставшие недостоверными после изменения файла."""
    asset.status = AssetStatus.DISCOVERED.value
    asset.checksum_algorithm = None
    asset.checksum = None
    asset.duplicate_of_asset_id = None
    asset.duration_ms = None
    asset.width = None
    asset.height = None
    asset.video_codec = None
    asset.frame_rate_num = None
    asset.frame_rate_den = None
    asset.audio_codec = None
    asset.sample_rate_hz = None
    asset.audio_channels = None
    asset.probe_metadata = None
    asset.last_error_code = None
    asset.last_error_message = None


async def _prepare_job(
    job_id: UUID, celery_task_id: str | None
) -> tuple[str, dict | None]:
    """Заблокировать job, проверить параметры и перевести её в running."""
    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None:
            raise LibraryScanJobNotFoundError("Задача сканирования не найдена")
        if job.job_type != JobType.LIBRARY_SCAN.value:
            raise InvalidLibraryScanJobError(
                "Задача имеет тип, отличный от library_scan"
            )
        if job.status == JobStatus.SUCCEEDED.value:
            return "", job.result or {}
        if job.status == JobStatus.CANCELLED.value:
            return "", {"status": JobStatus.CANCELLED.value}
        if job.media_root_id is None:
            raise InvalidLibraryScanJobError("Для задачи не задан media_root_id")

        media_root = await session.get(MediaRoot, job.media_root_id)
        if media_root is None:
            raise MediaRootNotFoundError("Корень медиатеки не найден")
        if not media_root.is_enabled:
            raise MediaRootDisabledError("Корень медиатеки отключён")

        current_time = _current_time()
        job.status = JobStatus.RUNNING.value
        job.celery_task_id = celery_task_id
        job.progress_current = 0
        job.progress_total = None
        job.result = None
        job.error_code = None
        job.error_message = None
        job.started_at = current_time
        job.heartbeat_at = current_time
        job.finished_at = None
        return media_root.container_path, None


async def _set_progress_total(job_id: UUID, progress_total: int) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise LibraryScanJobNotFoundError("Задача сканирования не найдена")
        job.progress_total = progress_total
        job.heartbeat_at = _current_time()


async def _store_scan_result(job_id: UUID, scan_result: MediaScanResult) -> dict:
    """Идемпотентно применить результат сканирования к БД."""
    current_time = _current_time()
    counters = {
        "discovered": len(scan_result.files),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "missing": 0,
        "skipped": scan_result.skipped_count,
    }

    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.media_root_id is None:
            raise LibraryScanJobNotFoundError("Задача сканирования не найдена")

        locations = list(
            await session.scalars(
                select(AssetLocation).where(
                    AssetLocation.media_root_id == job.media_root_id
                )
            )
        )
        locations_by_path = {
            location.normalized_path: location for location in locations
        }
        asset_ids = {location.asset_id for location in locations}
        assets = (
            list(await session.scalars(select(Asset).where(Asset.id.in_(asset_ids))))
            if asset_ids
            else []
        )
        assets_by_id = {asset.id: asset for asset in assets}
        seen_paths: set[str] = set()

        for index, discovered_file in enumerate(scan_result.files, start=1):
            seen_paths.add(discovered_file.normalized_path)
            location = locations_by_path.get(discovered_file.normalized_path)

            if location is None:
                asset = Asset(
                    status=AssetStatus.DISCOVERED.value,
                    media_kind=discovered_file.media_kind,
                    format_name=discovered_file.format_name,
                    mime_type=discovered_file.mime_type,
                    size_bytes=discovered_file.size_bytes,
                )
                session.add(asset)
                await session.flush()
                location = AssetLocation(
                    asset_id=asset.id,
                    media_root_id=job.media_root_id,
                    relative_path=discovered_file.relative_path,
                    normalized_path=discovered_file.normalized_path,
                    is_available=True,
                    size_bytes=discovered_file.size_bytes,
                    modified_at_ns=discovered_file.modified_at_ns,
                    first_seen_at=current_time,
                    last_seen_at=current_time,
                    last_seen_job_id=job_id,
                )
                session.add(location)
                assets_by_id[asset.id] = asset
                counters["created"] += 1
            else:
                asset = assets_by_id[location.asset_id]
                file_changed = (
                    location.size_bytes != discovered_file.size_bytes
                    or location.modified_at_ns != discovered_file.modified_at_ns
                    or location.relative_path != discovered_file.relative_path
                )
                became_available = not location.is_available
                location.relative_path = discovered_file.relative_path
                location.is_available = True
                location.size_bytes = discovered_file.size_bytes
                location.modified_at_ns = discovered_file.modified_at_ns
                location.last_seen_at = current_time
                location.missing_since = None
                location.last_seen_job_id = job_id

                if file_changed:
                    asset.media_kind = discovered_file.media_kind
                    asset.format_name = discovered_file.format_name
                    asset.mime_type = discovered_file.mime_type
                    asset.size_bytes = discovered_file.size_bytes
                    _reset_asset_after_file_change(asset)
                elif became_available:
                    asset.status = AssetStatus.DISCOVERED.value

                counter_name = (
                    "updated" if file_changed or became_available else "unchanged"
                )
                counters[counter_name] += 1

            if index % PROGRESS_BATCH_SIZE == 0:
                job.progress_current = index
                job.heartbeat_at = _current_time()
                await session.commit()

        missing_detection_complete = scan_result.skipped_count == 0
        missing_asset_ids: set[UUID] = set()
        if missing_detection_complete:
            for location in locations:
                if location.normalized_path in seen_paths or not location.is_available:
                    continue
                location.is_available = False
                location.missing_since = current_time
                missing_asset_ids.add(location.asset_id)
                counters["missing"] += 1

        if missing_asset_ids:
            available_asset_ids = set(
                await session.scalars(
                    select(AssetLocation.asset_id).where(
                        AssetLocation.asset_id.in_(missing_asset_ids),
                        AssetLocation.is_available.is_(True),
                    )
                )
            )
            for asset_id in missing_asset_ids - available_asset_ids:
                assets_by_id[asset_id].status = AssetStatus.MISSING.value

        result = {
            **counters,
            "missing_detection_complete": missing_detection_complete,
            "issues": [
                {
                    "relative_path": issue.relative_path,
                    "message": issue.message,
                }
                for issue in scan_result.issues
            ],
        }
        finished_at = _current_time()
        job.progress_current = len(scan_result.files)
        job.progress_total = len(scan_result.files)
        job.status = JobStatus.SUCCEEDED.value
        job.result = result
        job.heartbeat_at = finished_at
        job.finished_at = finished_at
        return result


async def _mark_job_failed(job_id: UUID, error: Exception) -> None:
    """Сохранить диагностическую ошибку, не маскируя исходное исключение."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {
            JobStatus.CANCELLED.value,
            JobStatus.SUCCEEDED.value,
        }:
            return
        finished_at = _current_time()
        job.status = JobStatus.FAILED.value
        job.error_code = getattr(error, "error_code", "library_scan_failed")
        job.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        job.heartbeat_at = finished_at
        job.finished_at = finished_at


async def execute_library_scan(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Выполнить полный проход library_scan и сохранить его состояние."""
    try:
        media_root_path, completed_result = await _prepare_job(
            job_id,
            celery_task_id,
        )
        if completed_result is not None:
            return completed_result

        allowed_root_path = Path(os.getenv("MEDIA_ROOT_PATH", DEFAULT_MEDIA_ROOT_PATH))
        scan_result = discover_media_files(
            Path(media_root_path),
            allowed_root_path,
        )
        await _set_progress_total(job_id, len(scan_result.files))
        return await _store_scan_result(job_id, scan_result)
    except Exception as error:
        await _mark_job_failed(job_id, error)
        raise
