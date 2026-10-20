"""Общий SQL-поиск планов с фильтрацией области до top-k."""

from sqlalchemy import select

from services.asset_embedding import TEXT_VERSION
from sql.enums import AssetStatus, DescriptionStatus, EmbeddingStatus
from sql.models import (
    Asset,
    AssetLocation,
    MediaRoot,
    Shot,
    ShotDescription,
    ShotEmbedding,
)


def shot_search_statement(
    vector,
    provider,
    settings,
    *,
    limit=20,
    min_similarity=None,
    asset_id=None,
    asset_ids=None,
    media_root_id=None,
    min_duration_ms=0,
    available_only=False,
):
    similarity = (1 - ShotEmbedding.embedding.cosine_distance(vector)).label(
        "similarity"
    )
    filters = [
        ShotEmbedding.status == EmbeddingStatus.SUCCEEDED,
        ShotEmbedding.provider == provider.provider_name,
        ShotEmbedding.model == settings.model,
        ShotEmbedding.text_version == TEXT_VERSION,
        ShotEmbedding.embedding.is_not(None),
        ShotDescription.status == DescriptionStatus.SUCCEEDED,
        Shot.end_ms - Shot.start_ms >= min_duration_ms,
    ]
    if asset_id is not None:
        filters.append(Shot.asset_id == asset_id)
    if asset_ids:
        filters.append(Shot.asset_id.in_(asset_ids))
    if min_similarity is not None:
        filters.append(similarity >= min_similarity)
    if available_only or media_root_id is not None:
        location_filters = [
            AssetLocation.asset_id == Shot.asset_id,
            AssetLocation.is_available.is_(True),
            MediaRoot.is_enabled.is_(True),
        ]
        if media_root_id is not None:
            location_filters.append(AssetLocation.media_root_id == media_root_id)
        filters.append(
            select(AssetLocation.id)
            .join(
                MediaRoot,
                MediaRoot.id == AssetLocation.media_root_id,
            )
            .where(*location_filters)
            .exists()
        )
    statement = (
        select(Shot, ShotDescription, ShotEmbedding, similarity)
        .select_from(
            ShotEmbedding,
        )
        .join(ShotDescription, ShotDescription.id == ShotEmbedding.shot_description_id)
        .join(
            Shot,
            Shot.id == ShotDescription.shot_id,
        )
        .where(*filters)
        .order_by(similarity.desc(), Shot.id)
        .limit(limit)
    )
    if available_only:
        statement = statement.join(Asset, Asset.id == Shot.asset_id).where(
            Asset.media_kind == "video",
            Asset.status.in_([AssetStatus.INDEXED, AssetStatus.READY]),
        )
    return statement
