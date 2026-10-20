"""Семантический поиск воспроизводимых интервалов видео."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.search import ShotSearchRequest, ShotSearchResponse, ShotSearchResult
from services.shot_search import shot_search_statement
from services.embedding_provider import EmbeddingProviderError
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import TextEmbeddingProvider
from services.embedding_provider import get_embedding_settings

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

    statement = shot_search_statement(
        query_vector,
        provider,
        settings,
        limit=request.limit,
        min_similarity=request.min_similarity,
        asset_id=request.asset_id,
        asset_ids=request.asset_ids,
        media_root_id=request.media_root_id,
        min_duration_ms=request.min_duration_ms,
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
