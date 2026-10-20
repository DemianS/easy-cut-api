"""Публичные схемы семантического поиска по планам."""

from uuid import UUID

from pydantic import BaseModel, Field


class ShotSearchRequest(BaseModel):
    """Текстовый запрос и ограничители поиска."""

    query: str = Field(min_length=1, max_length=2000)
    asset_id: UUID | None = None
    asset_ids: list[UUID] = Field(default_factory=list, max_length=200)
    media_root_id: UUID | None = None
    min_duration_ms: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    min_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)


class ShotSearchResult(BaseModel):
    """Воспроизводимый временной интервал найденного плана."""

    asset_id: UUID
    shot_id: UUID
    start_ms: int
    end_ms: int
    summary: str
    similarity: float
    embedding_model: str


class ShotSearchResponse(BaseModel):
    """Результаты семантического поиска для исходного запроса."""

    query: str
    items: list[ShotSearchResult]
