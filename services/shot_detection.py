"""Оркестрация детекции планов одного видеоактива."""

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

from sqlalchemy import delete, func, select

from services.media_scanner import resolve_media_root
from services.shot_detector import DEFAULT_CONTENT_THRESHOLD
from services.shot_detector import DEFAULT_FRAME_EXTRACTION_TIMEOUT_SECONDS
from services.shot_detector import DEFAULT_MIN_SHOT_LENGTH_SECONDS
from services.shot_detector import ShotBoundary, detect_shots, detector_metadata
from services.shot_detector import extract_representative_frame
from sql.database import session_scope
from sql.enums import AssetKind, AssetStatus, DescriptionStatus, JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot, Shot
from sql.models import ShotDescription, ShotFrame

DEFAULT_MEDIA_ROOT_PATH = "/media/library"
DEFAULT_ARTIFACTS_PATH = "/data/artifacts"
MAX_ERROR_MESSAGE_LENGTH = 2000


class ShotDetectionJobError(RuntimeError):
    error_code = "shot_detection_failed"


class InvalidShotDetectionJobError(ShotDetectionJobError):
    error_code = "invalid_shot_detection_job"


class VideoAssetUnavailableError(ShotDetectionJobError):
    error_code = "video_asset_unavailable"


@dataclass(frozen=True, slots=True)
class DetectionContext:
    asset_id: UUID
    media_root_path: str
    relative_path: str
    duration_ms: int
    width: int
    height: int


def _current_time() -> datetime:
    return datetime.now(UTC)


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ShotDetectionJobError(f"Некорректное значение {name}") from error
    if value <= 0:
        raise ShotDetectionJobError(f"{name} должен быть положительным")
    return value


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ShotDetectionJobError(f"Некорректное значение {name}") from error
    if value <= 0:
        raise ShotDetectionJobError(f"{name} должен быть положительным")
    return value


async def _prepare_detection(
    job_id: UUID,
    celery_task_id: str | None,
) -> tuple[DetectionContext | None, dict | None]:
    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None or job.job_type != JobType.SHOT_DETECTION.value:
            raise InvalidShotDetectionJobError("Задача детекции планов не найдена")
        if job.status == JobStatus.SUCCEEDED:
            return None, job.result or {}
        if job.status == JobStatus.CANCELLED:
            return None, {"status": JobStatus.CANCELLED.value}
        if job.asset_id is None or job.media_root_id is None:
            raise InvalidShotDetectionJobError("В задаче отсутствует asset или root")

        asset = await session.get(Asset, job.asset_id)
        media_root = await session.get(MediaRoot, job.media_root_id)
        if (
            asset is None
            or media_root is None
            or not media_root.is_enabled
            or asset.media_kind != AssetKind.VIDEO.value
            or asset.duration_ms is None
            or asset.width is None
            or asset.height is None
        ):
            raise VideoAssetUnavailableError(
                "Видеоактив не проиндексирован или недоступен"
            )
        location = await session.scalar(
            select(AssetLocation)
            .where(
                AssetLocation.asset_id == asset.id,
                AssetLocation.media_root_id == media_root.id,
                AssetLocation.is_available.is_(True),
            )
            .order_by(AssetLocation.relative_path)
            .limit(1)
        )
        if location is None:
            raise VideoAssetUnavailableError("Расположение видео недоступно")

        now = _current_time()
        job.status = JobStatus.RUNNING
        job.celery_task_id = celery_task_id or job.celery_task_id
        job.progress_current = 0
        job.progress_total = None
        job.started_at = now
        job.heartbeat_at = now
        job.finished_at = None
        job.result = None
        job.error_code = None
        job.error_message = None
        asset.status = AssetStatus.ANALYZING
        asset.last_attempt_at = now
        return (
            DetectionContext(
                asset.id,
                media_root.container_path,
                location.relative_path,
                asset.duration_ms,
                asset.width,
                asset.height,
            ),
            None,
        )


def _resolve_media_path(context: DetectionContext) -> Path:
    allowed_root = Path(os.getenv("MEDIA_ROOT_PATH", DEFAULT_MEDIA_ROOT_PATH))
    media_root = resolve_media_root(Path(context.media_root_path), allowed_root)
    relative_path = Path(context.relative_path)
    if relative_path.is_absolute():
        raise VideoAssetUnavailableError("Путь видео должен быть относительным")
    unresolved_media_path = media_root / relative_path
    if unresolved_media_path.is_symlink():
        raise VideoAssetUnavailableError("Символические ссылки не разрешены")
    try:
        media_path = unresolved_media_path.resolve(strict=True)
        media_path.relative_to(media_root)
    except (OSError, ValueError) as error:
        raise VideoAssetUnavailableError("Видео недоступно") from error
    if not media_path.is_file():
        raise VideoAssetUnavailableError("Видео недоступно")
    return media_path


async def _store_shots(
    job_id: UUID,
    context: DetectionContext,
    boundaries: list[ShotBoundary],
    detector: str,
    detector_version: str,
    detector_settings: dict,
) -> list[tuple[UUID, ShotBoundary]]:
    async with session_scope() as session:
        existing_shots = list(
            await session.scalars(
                select(Shot)
                .where(Shot.asset_id == context.asset_id)
                .order_by(Shot.sequence)
            )
        )
        stored = []
        for boundary in boundaries:
            if boundary.end_ms > context.duration_ms + 1000:
                raise ShotDetectionJobError("План выходит за длительность видео")
            shot = (
                existing_shots[boundary.sequence]
                if boundary.sequence < len(existing_shots)
                else Shot(asset_id=context.asset_id, sequence=boundary.sequence)
            )
            end_ms = min(boundary.end_ms, context.duration_ms)
            if end_ms <= boundary.start_ms:
                raise ShotDetectionJobError("План выходит за длительность видео")
            boundaries_changed = shot.id is not None and (
                shot.start_ms != boundary.start_ms
                or shot.end_ms != end_ms
                or shot.detector != detector
                or shot.detector_version != detector_version
                or shot.detector_settings != detector_settings
            )
            if boundaries_changed:
                await session.execute(
                    delete(ShotDescription).where(ShotDescription.shot_id == shot.id)
                )
            shot.start_ms = boundary.start_ms
            shot.end_ms = end_ms
            shot.detector = detector
            shot.detector_version = detector_version
            shot.detector_settings = detector_settings
            session.add(shot)
            await session.flush()
            stored.append((shot.id, boundary))

        obsolete_ids = [shot.id for shot in existing_shots[len(boundaries) :]]
        if obsolete_ids:
            await session.execute(delete(Shot).where(Shot.id.in_(obsolete_ids)))
        job = await session.get(Job, job_id)
        if job is None:
            raise InvalidShotDetectionJobError("Задача детекции исчезла")
        job.progress_total = len(stored)
        job.heartbeat_at = _current_time()
        return stored


def _artifact_destination(asset_id: UUID, shot_id: UUID) -> tuple[Path, str]:
    relative_path = PurePosixPath("frames", str(asset_id), str(shot_id), "middle.jpg")
    root = Path(os.getenv("ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH)).resolve()
    destination = (root / Path(*relative_path.parts)).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise ShotDetectionJobError("Некорректный путь артефакта") from error
    return destination, relative_path.as_posix()


async def _store_frame(
    job_id: UUID,
    shot_id: UUID,
    boundary: ShotBoundary,
    artifact_path: str,
    width: int,
    height: int,
) -> None:
    async with session_scope() as session:
        frame = await session.scalar(
            select(ShotFrame).where(
                ShotFrame.shot_id == shot_id,
                ShotFrame.role == "middle",
            )
        )
        if frame is None:
            frame = ShotFrame(shot_id=shot_id, role="middle")
            session.add(frame)
        frame.timestamp_ms = boundary.middle_ms
        frame.artifact_path = artifact_path
        frame.width = width
        frame.height = height
        job = await session.get(Job, job_id)
        if job is None:
            raise InvalidShotDetectionJobError("Задача детекции исчезла")
        job.progress_current = boundary.sequence + 1
        job.heartbeat_at = _current_time()


async def _complete_detection(job_id: UUID, context: DetectionContext) -> dict:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        asset = await session.get(Asset, context.asset_id)
        if job is None or asset is None:
            raise InvalidShotDetectionJobError("Задача или актив исчезли")
        result = {
            "asset_id": str(context.asset_id),
            "shots": job.progress_current,
            "frames": job.progress_current,
        }
        now = _current_time()
        shot_count = await session.scalar(
            select(func.count()).select_from(Shot).where(Shot.asset_id == asset.id)
        )
        described_count = await session.scalar(
            select(func.count())
            .select_from(ShotDescription)
            .join(Shot, Shot.id == ShotDescription.shot_id)
            .where(
                Shot.asset_id == asset.id,
                ShotDescription.status == DescriptionStatus.SUCCEEDED,
            )
        )
        job.status = JobStatus.SUCCEEDED
        job.result = result
        job.heartbeat_at = now
        job.finished_at = now
        asset.status = (
            AssetStatus.READY
            if shot_count and described_count == shot_count
            else AssetStatus.INDEXED
        )
        asset.last_error_code = None
        asset.last_error_message = None
        return result


async def _mark_detection_failed(job_id: UUID, error: Exception) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {JobStatus.SUCCEEDED, JobStatus.CANCELLED}:
            return
        now = _current_time()
        error_code = getattr(error, "error_code", "shot_detection_failed")
        error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        job.status = JobStatus.FAILED
        job.error_code = error_code
        job.error_message = error_message
        job.heartbeat_at = now
        job.finished_at = now
        if job.asset_id is not None:
            asset = await session.get(Asset, job.asset_id)
            if asset is not None:
                asset.status = AssetStatus.FAILED
                asset.last_error_code = error_code
                asset.last_error_message = error_message


async def execute_shot_detection(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Определить планы, сохранить интервалы и извлечь средние кадры."""
    try:
        context, completed_result = await _prepare_detection(job_id, celery_task_id)
        if completed_result is not None:
            return completed_result
        if context is None:
            raise InvalidShotDetectionJobError("Не удалось подготовить задачу")

        media_path = _resolve_media_path(context)
        threshold = _positive_float(
            "SHOT_DETECTION_THRESHOLD",
            DEFAULT_CONTENT_THRESHOLD,
        )
        min_length = _positive_float(
            "MIN_SHOT_LENGTH_SECONDS",
            DEFAULT_MIN_SHOT_LENGTH_SECONDS,
        )
        boundaries = detect_shots(
            media_path,
            threshold=threshold,
            min_shot_length_seconds=min_length,
        )
        detector, version, settings = detector_metadata(threshold, min_length)
        shots = await _store_shots(
            job_id,
            context,
            boundaries,
            detector,
            version,
            settings,
        )
        timeout = _positive_int(
            "FRAME_EXTRACTION_TIMEOUT_SECONDS",
            DEFAULT_FRAME_EXTRACTION_TIMEOUT_SECONDS,
        )
        for shot_id, boundary in shots:
            destination, artifact_path = _artifact_destination(
                context.asset_id,
                shot_id,
            )
            frame = extract_representative_frame(
                media_path,
                destination,
                timestamp_ms=boundary.middle_ms,
                source_width=context.width,
                source_height=context.height,
                artifact_path=artifact_path,
                timeout_seconds=timeout,
            )
            await _store_frame(
                job_id,
                shot_id,
                boundary,
                frame.artifact_path,
                frame.width,
                frame.height,
            )
        return await _complete_detection(job_id, context)
    except Exception as error:
        await _mark_detection_failed(job_id, error)
        raise
