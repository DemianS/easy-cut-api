"""Оркестрация задачи сканирования медиатеки через PostgreSQL."""

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import select

from services.media_scanner import MediaScanResult
from services.media_scanner import discover_media_files
from services.logging_config import log_event, LogStatus
from sql.database import session_scope
from sql.enums import AssetStatus, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot

logger = logging.getLogger(__name__)

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
    asset.status = AssetStatus.DISCOVERED
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
        if job.status == JobStatus.SUCCEEDED:
            return "", job.result or {}
        if job.status == JobStatus.CANCELLED:
            return "", {"status": JobStatus.CANCELLED.value}
        if job.media_root_id is None:
            raise InvalidLibraryScanJobError("Для задачи не задан media_root_id")

        media_root = await session.get(MediaRoot, job.media_root_id)
        if media_root is None:
            raise MediaRootNotFoundError("Корень медиатеки не найден")
        if not media_root.is_enabled:
            raise MediaRootDisabledError("Корень медиатеки отключён")

        current_time = _current_time()
        job.status = JobStatus.RUNNING
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
        probe_candidate_ids: set[UUID] = set()

        for index, discovered_file in enumerate(scan_result.files, start=1):
            seen_paths.add(discovered_file.normalized_path)
            location = locations_by_path.get(discovered_file.normalized_path)

            if location is None:
                asset = Asset(
                    status=AssetStatus.DISCOVERED,
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
                    asset.status = AssetStatus.DISCOVERED

                counter_name = (
                    "updated" if file_changed or became_available else "unchanged"
                )
                counters[counter_name] += 1

            if asset.status in {
                AssetStatus.DISCOVERED,
                AssetStatus.FAILED,
            }:
                probe_candidate_ids.add(asset.id)

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
            probe_candidate_ids -= missing_asset_ids
            cancelled_probe_jobs = list(
                await session.scalars(
                    select(Job).where(
                        Job.asset_id.in_(missing_asset_ids),
                        Job.job_type == JobType.ASSET_PROBE.value,
                        Job.status == JobStatus.PENDING,
                    )
                )
            )
            cancelled_at = _current_time()
            for probe_job in cancelled_probe_jobs:
                probe_job.status = JobStatus.CANCELLED
                probe_job.finished_at = cancelled_at
                probe_job.heartbeat_at = cancelled_at
            available_asset_ids = set(
                await session.scalars(
                    select(AssetLocation.asset_id).where(
                        AssetLocation.asset_id.in_(missing_asset_ids),
                        AssetLocation.is_available.is_(True),
                    )
                )
            )
            for asset_id in missing_asset_ids - available_asset_ids:
                assets_by_id[asset_id].status = AssetStatus.MISSING

        active_probe_asset_ids = set(
            await session.scalars(
                select(Job.asset_id).where(
                    Job.asset_id.in_(probe_candidate_ids),
                    Job.job_type == JobType.ASSET_PROBE.value,
                    Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
                )
            )
        )
        probe_job_ids: list[str] = []
        for asset_id in sorted(
            probe_candidate_ids - active_probe_asset_ids,
            key=str,
        ):
            probe_job_id = uuid4()
            session.add(
                Job(
                    id=probe_job_id,
                    job_type=JobType.ASSET_PROBE.value,
                    status=JobStatus.PENDING,
                    parent_job_id=job.parent_job_id,
                    media_root_id=job.media_root_id,
                    asset_id=asset_id,
                    created_by_user_id=job.created_by_user_id,
                    celery_task_id=str(probe_job_id),
                    progress_total=1,
                )
            )
            probe_job_ids.append(str(probe_job_id))

        result = {
            **counters,
            "missing_detection_complete": missing_detection_complete,
            "probe_job_ids": probe_job_ids,
            "probe_jobs_created": len(probe_job_ids),
            "probe_jobs_cancelled": (
                len(cancelled_probe_jobs) if missing_asset_ids else 0
            ),
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
        job.status = JobStatus.SUCCEEDED
        job.result = result
        job.heartbeat_at = finished_at
        job.finished_at = finished_at
        return result


async def record_probe_dispatch_result(
    scan_job_id: UUID,
    dispatched_job_ids: list[str],
    failed_job_ids: list[str],
) -> None:
    """Дополнить результат сканирования итогом отправки дочерних задач."""
    async with session_scope() as session:
        job = await session.get(Job, scan_job_id)
        if job is None:
            return
        result = dict(job.result or {})
        result["probe_jobs_dispatched"] = len(dispatched_job_ids)
        result["probe_dispatch_failed_ids"] = failed_job_ids
        job.result = result
        dispatched_at = _current_time()
        for dispatched_job_id in dispatched_job_ids:
            probe_job = await session.get(Job, UUID(dispatched_job_id))
            if probe_job is not None:
                probe_job.last_dispatched_at = dispatched_at


async def _mark_job_failed(job_id: UUID, error: Exception) -> None:
    """Сохранить диагностическую ошибку, не маскируя исходное исключение."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {
            JobStatus.CANCELLED,
            JobStatus.SUCCEEDED,
        }:
            return
        finished_at = _current_time()
        job.status = JobStatus.FAILED
        job.error_code = getattr(error, "error_code", "library_scan_failed")
        job.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        job.heartbeat_at = finished_at
        job.finished_at = finished_at


async def execute_library_scan(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Выполнить полный проход library_scan и сохранить его состояние."""
    started_at = monotonic()
    log_event(
        logger,
        logging.INFO,
        "library_scan",
        LogStatus.STARTED,
        "Сканирование медиатеки начато",
        stage="prepare",
        job_id=str(job_id),
    )
    try:
        media_root_path, completed_result = await _prepare_job(
            job_id,
            celery_task_id,
        )
        if completed_result is not None:
            log_event(
                logger,
                logging.INFO,
                "library_scan",
                LogStatus.REUSED,
                "Готовый результат сканирования медиатеки переиспользован",
                stage="prepare",
                job_id=str(job_id),
            )
            return completed_result

        allowed_root_path = Path(os.getenv("MEDIA_ROOT_PATH", DEFAULT_MEDIA_ROOT_PATH))
        scan_result = discover_media_files(
            Path(media_root_path),
            allowed_root_path,
        )
        log_event(
            logger,
            logging.INFO,
            "library_scan",
            LogStatus.PROGRESS,
            "Обход медиатеки завершён",
            stage="discovery",
            job_id=str(job_id),
            total=len(scan_result.files),
            items_skipped=scan_result.skipped_count,
        )
        await _set_progress_total(job_id, len(scan_result.files))
        result = await _store_scan_result(job_id, scan_result)
        log_event(
            logger,
            logging.INFO,
            "library_scan",
            LogStatus.SUCCEEDED,
            "Сканирование медиатеки успешно завершено",
            stage="store",
            job_id=str(job_id),
            elapsed_ms=round((monotonic() - started_at) * 1000),
            assets_created=result["created"],
            assets_updated=result["updated"],
            assets_unchanged=result["unchanged"],
            assets_missing=result["missing"],
            items_skipped=result["skipped"],
            dispatches=result["probe_jobs_created"],
        )
        return result
    except Exception as error:
        await _mark_job_failed(job_id, error)
        log_event(
            logger,
            logging.ERROR,
            "library_scan",
            LogStatus.FAILED,
            "Сканирование медиатеки завершилось ошибкой",
            stage="execution",
            exc_info=True,
            job_id=str(job_id),
            elapsed_ms=round((monotonic() - started_at) * 1000),
            error_type=type(error).__name__,
            error_code=getattr(error, "error_code", "library_scan_failed"),
        )
        raise
