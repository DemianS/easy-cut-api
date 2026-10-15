"""Просмотр проиндексированной медиабиблиотеки."""

from collections import defaultdict
from enum import StrEnum
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.assets import AssetDetailResponse, AssetListItem, AssetPageResponse
from schemas.assets import AssetLocationResponse
from sql.enums import AssetKind, AssetStatus
from sql.models import Asset, AssetLocation

router = APIRouter(prefix="/assets", tags=["assets"])
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
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
