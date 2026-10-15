"""Семантический поиск воспроизводимых интервалов видео."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.search import ShotSearchRequest, ShotSearchResponse, ShotSearchResult
from services.asset_embedding import TEXT_VERSION
from services.embedding_provider import EmbeddingProviderError
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import TextEmbeddingProvider
from services.embedding_provider import get_embedding_settings
from sql.enums import EmbeddingStatus
from sql.models import Shot, ShotDescription, ShotEmbedding

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/shots", response_model=ShotSearchResponse)
async def search_shots(
    request: ShotSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> ShotSearchResponse:
    """Найти планы по embedding текста и вернуть точные интервалы."""
    try:
        settings = get_embedding_settings()
        provider = TextEmbeddingProvider(settings)
        query_vector = await asyncio.to_thread(provider.embed, request.query)
    except EmbeddingProviderNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except EmbeddingProviderError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error

    similarity = (1 - ShotEmbedding.embedding.cosine_distance(query_vector)).label(
        "similarity"
    )
    filters = [
        ShotEmbedding.status == EmbeddingStatus.SUCCEEDED,
        ShotEmbedding.provider == provider.provider_name,
        ShotEmbedding.model == settings.model,
        ShotEmbedding.text_version == TEXT_VERSION,
        ShotEmbedding.embedding.is_not(None),
    ]
    if request.asset_id is not None:
        filters.append(Shot.asset_id == request.asset_id)
    if request.min_similarity is not None:
        filters.append(similarity >= request.min_similarity)

    statement = (
        select(Shot, ShotDescription, ShotEmbedding, similarity)
        .join(
            ShotDescription,
            ShotDescription.id == ShotEmbedding.shot_description_id,
        )
        .join(Shot, Shot.id == ShotDescription.shot_id)
        .where(*filters)
        .order_by(similarity.desc(), Shot.id)
        .limit(request.limit)
    )
    rows = (await session.execute(statement)).all()
    return ShotSearchResponse(
        query=request.query,
        items=[
            ShotSearchResult(
                asset_id=shot.asset_id,
                shot_id=shot.id,
                start_ms=shot.start_ms,
                end_ms=shot.end_ms,
                summary=description.summary or embedding.indexed_text,
                similarity=float(score),
                embedding_model=embedding.model,
            )
            for shot, description, embedding, score in rows
        ],
    )
