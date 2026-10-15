"""Просмотр и запуск анализа проиндексированной медиабиблиотеки."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.assets import AssetDetailResponse, AssetListItem, AssetPageResponse
from schemas.assets import AssetLocationResponse
from schemas.assets import ShotDescriptionResponse, ShotFrameResponse
from schemas.assets import ShotListResponse, ShotResponse
from schemas.jobs import JobResponse
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import get_embedding_settings
from services.vision_provider import VisionProviderNotConfiguredError
from services.vision_provider import get_vision_settings
from sql.enums import AssetKind, AssetStatus, DescriptionStatus
from sql.enums import JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, Shot, ShotDescription, ShotFrame
from worker import celery_app

router = APIRouter(prefix="/assets", tags=["assets"])
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
SortField = Literal[
    "created_at",
    "updated_at",
    "size_bytes",
    "duration_ms",
    "relative_path",
]


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


def _location_order():
    return (
        desc(AssetLocation.is_available),
        asc(AssetLocation.normalized_path),
        asc(AssetLocation.id),
    )


def _asset_filters(
    asset_status: AssetStatus | None,
    media_kind: AssetKind | None,
    media_root_id: UUID | None,
    is_available: bool | None,
    search: str | None,
) -> list:
    filters = []
    if asset_status is not None:
        filters.append(Asset.status == asset_status)
    if media_kind is not None:
        filters.append(Asset.media_kind == media_kind.value)

    location_filters = [AssetLocation.asset_id == Asset.id]
    if media_root_id is not None:
        location_filters.append(AssetLocation.media_root_id == media_root_id)
    if is_available is not None:
        location_filters.append(AssetLocation.is_available.is_(is_available))
    if search is not None:
        location_filters.append(
            AssetLocation.relative_path.ilike(f"%{search.strip()}%")
        )
    if len(location_filters) > 1:
        filters.append(select(AssetLocation.id).where(*location_filters).exists())
    return filters


def _asset_order(sort_by: SortField, sort_order: SortOrder):
    if sort_by == "relative_path":
        sort_column = (
            select(AssetLocation.normalized_path)
            .where(AssetLocation.asset_id == Asset.id)
            .order_by(*_location_order())
            .limit(1)
            .scalar_subquery()
        )
    else:
        sort_column = getattr(Asset, sort_by)

    direction = asc if sort_order == SortOrder.ASC else desc
    return direction(sort_column).nulls_last()


def _summary(asset: Asset, locations: list[AssetLocation]) -> AssetListItem:
    primary_location = locations[0] if locations else None
    return AssetListItem(
        id=asset.id,
        status=asset.status,
        media_kind=asset.media_kind,
        relative_path=(
            primary_location.relative_path if primary_location is not None else None
        ),
        is_available=any(location.is_available for location in locations),
        format_name=asset.format_name,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        duration_ms=asset.duration_ms,
        width=asset.width,
        height=asset.height,
        video_codec=asset.video_codec,
        frame_rate_num=asset.frame_rate_num,
        frame_rate_den=asset.frame_rate_den,
        audio_codec=asset.audio_codec,
        sample_rate_hz=asset.sample_rate_hz,
        audio_channels=asset.audio_channels,
        last_error_code=asset.last_error_code,
        last_error_message=asset.last_error_message,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


async def _locations_by_asset(
    session: AsyncSession,
    asset_ids: list[UUID],
) -> dict[UUID, list[AssetLocation]]:
    if not asset_ids:
        return {}

    statement = (
        select(AssetLocation)
        .where(AssetLocation.asset_id.in_(asset_ids))
        .order_by(AssetLocation.asset_id, *_location_order())
    )
    locations = list(await session.scalars(statement))
    grouped_locations: dict[UUID, list[AssetLocation]] = defaultdict(list)
    for location in locations:
        grouped_locations[location.asset_id].append(location)
    return grouped_locations


@router.get("", response_model=AssetPageResponse)
async def list_assets(
    asset_status: AssetStatus | None = Query(default=None, alias="status"),
    media_kind: AssetKind | None = None,
    media_root_id: UUID | None = None,
    is_available: bool | None = None,
    search: str | None = Query(default=None, min_length=1, max_length=500),
    sort_by: SortField = "created_at",
    sort_order: SortOrder = SortOrder.DESC,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AssetPageResponse:
    """Вернуть страницу активов с фильтрацией и стабильной сортировкой."""
    filters = _asset_filters(
        asset_status,
        media_kind,
        media_root_id,
        is_available,
        search,
    )
    total = await session.scalar(
        select(func.count()).select_from(Asset).where(*filters)
    )
    statement: Select = (
        select(Asset)
        .where(*filters)
        .order_by(_asset_order(sort_by, sort_order), asc(Asset.id))
        .limit(limit)
        .offset(offset)
    )
    assets = list(await session.scalars(statement))
    locations = await _locations_by_asset(
        session,
        [asset.id for asset in assets],
    )

    return AssetPageResponse(
        items=[_summary(asset, locations.get(asset.id, [])) for asset in assets],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{asset_id}", response_model=AssetDetailResponse)
async def get_asset(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> AssetDetailResponse:
    """Вернуть технические метаданные и все расположения актива."""
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Медиаактив не найден",
        )

    locations = (await _locations_by_asset(session, [asset_id])).get(asset_id, [])
    summary = _summary(asset, locations)
    return AssetDetailResponse(
        **summary.model_dump(),
        checksum_algorithm=asset.checksum_algorithm,
        checksum=asset.checksum,
        duplicate_of_asset_id=asset.duplicate_of_asset_id,
        probe_metadata=asset.probe_metadata,
        last_attempt_at=asset.last_attempt_at,
        locations=[AssetLocationResponse.model_validate(item) for item in locations],
    )


async def _start_asset_job(
    asset_id: UUID,
    job_type: JobType,
    task_name: str,
    session: AsyncSession,
) -> Job:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Медиаактив не найден",
        )
    if asset.media_kind != AssetKind.VIDEO.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Анализ планов доступен только для видео",
        )
    location = await session.scalar(
        select(AssetLocation)
        .where(
            AssetLocation.asset_id == asset_id,
            AssetLocation.is_available.is_(True),
        )
        .order_by(AssetLocation.relative_path)
        .limit(1)
    )
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У медиаактива нет доступного расположения",
        )

    active_job = await session.scalar(
        select(Job)
        .where(
            Job.asset_id == asset_id,
            Job.job_type == job_type.value,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if active_job is not None:
        return active_job

    job_id = uuid4()
    job = Job(
        id=job_id,
        job_type=job_type.value,
        status=JobStatus.PENDING,
        media_root_id=location.media_root_id,
        asset_id=asset_id,
        celery_task_id=str(job_id),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    try:
        await asyncio.to_thread(
            celery_app.send_task,
            task_name,
            args=[str(job_id)],
            task_id=str(job_id),
        )
    except Exception as error:
        failed_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = "queue_unavailable"
        job.error_message = "Не удалось отправить задачу в очередь"
        job.finished_at = failed_at
        job.heartbeat_at = failed_at
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": job.error_message, "job_id": str(job_id)},
        ) from error
    return job


@router.post(
    "/{asset_id}/shots/detect",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_shot_detection(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Поставить детекцию планов и извлечение кадров в очередь."""
    asset = await session.get(Asset, asset_id)
    if asset is not None and (
        asset.duration_ms is None or asset.width is None or asset.height is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала необходимо выполнить технический анализ видео",
        )
    return await _start_asset_job(
        asset_id,
        JobType.SHOT_DETECTION,
        "easy_cut.shot_detection",
        session,
    )


@router.post(
    "/{asset_id}/describe",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_asset_description(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Поставить описание планов локальной vision-моделью в очередь."""
    if await session.get(Asset, asset_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Медиаактив не найден",
        )
    try:
        get_vision_settings()
    except VisionProviderNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    has_frames = await session.scalar(
        select(ShotFrame.id)
        .join(Shot, Shot.id == ShotFrame.shot_id)
        .where(Shot.asset_id == asset_id)
        .limit(1)
    )
    if has_frames is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала необходимо определить планы",
        )
    return await _start_asset_job(
        asset_id,
        JobType.ASSET_DESCRIPTION,
        "easy_cut.asset_description",
        session,
    )


@router.post(
    "/{asset_id}/embeddings",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_asset_embedding(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Поставить построение embeddings описанных планов в очередь."""
    if await session.get(Asset, asset_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Медиаактив не найден",
        )
    try:
        get_embedding_settings()
    except EmbeddingProviderNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    has_descriptions = await session.scalar(
        select(ShotDescription.id)
        .join(Shot, Shot.id == ShotDescription.shot_id)
        .where(
            Shot.asset_id == asset_id,
            ShotDescription.status == DescriptionStatus.SUCCEEDED,
        )
        .limit(1)
    )
    if has_descriptions is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала необходимо описать планы",
        )
    return await _start_asset_job(
        asset_id,
        JobType.ASSET_EMBEDDING,
        "easy_cut.asset_embedding",
        session,
    )


@router.get("/{asset_id}/shots", response_model=ShotListResponse)
async def list_asset_shots(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> ShotListResponse:
    """Вернуть текущие планы, кадры и локальные описания актива."""
    if await session.get(Asset, asset_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Медиаактив не найден",
        )
    shots = list(
        await session.scalars(
            select(Shot).where(Shot.asset_id == asset_id).order_by(Shot.sequence)
        )
    )
    if not shots:
        return ShotListResponse(asset_id=asset_id, items=[])

    shot_ids = [shot.id for shot in shots]
    frames = list(
        await session.scalars(
            select(ShotFrame)
            .where(ShotFrame.shot_id.in_(shot_ids))
            .order_by(ShotFrame.timestamp_ms)
        )
    )
    descriptions = list(
        await session.scalars(
            select(ShotDescription).where(ShotDescription.shot_id.in_(shot_ids))
        )
    )
    frames_by_shot: dict[UUID, list[ShotFrame]] = defaultdict(list)
    for frame in frames:
        frames_by_shot[frame.shot_id].append(frame)
    descriptions_by_shot = {
        description.shot_id: description for description in descriptions
    }
    return ShotListResponse(
        asset_id=asset_id,
        items=[
            ShotResponse(
                id=shot.id,
                sequence=shot.sequence,
                start_ms=shot.start_ms,
                end_ms=shot.end_ms,
                detector=shot.detector,
                detector_version=shot.detector_version,
                detector_settings=shot.detector_settings,
                frames=[
                    ShotFrameResponse.model_validate(frame)
                    for frame in frames_by_shot.get(shot.id, [])
                ],
                description=(
                    ShotDescriptionResponse.model_validate(
                        descriptions_by_shot[shot.id]
                    )
                    if shot.id in descriptions_by_shot
                    else None
                ),
            )
            for shot in shots
        ],
    )
