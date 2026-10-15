from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from dependencies import get_session
from main import app
from services.embedding_provider import EmbeddingSettings


class SearchRows:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def all(self) -> list[tuple]:
        return self.rows


class SearchSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return SearchRows(self.rows)


def _session_dependency(session: SearchSession):
    async def dependency() -> AsyncIterator[SearchSession]:
        yield session

    return dependency


def test_search_returns_reproducible_shot_interval(monkeypatch) -> None:
    asset_id = uuid4()
    shot_id = uuid4()
    shot = SimpleNamespace(
        id=shot_id,
        asset_id=asset_id,
        start_ms=1250,
        end_ms=3100,
    )
    description = SimpleNamespace(summary="Руки собирают металлическую деталь.")
    embedding = SimpleNamespace(
        indexed_text=description.summary,
        model="nomic-embed-text",
    )
    session = SearchSession([(shot, description, embedding, 0.87)])
    monkeypatch.setattr(
        "routers.search.get_embedding_settings",
        lambda: EmbeddingSettings(
            "http://ollama:11434",
            "nomic-embed-text",
            30,
            768,
        ),
    )
    monkeypatch.setattr(
        "routers.search.OllamaEmbeddingProvider.embed",
        lambda self, text: [0.0] * 768,
    )
    app.dependency_overrides[get_session] = _session_dependency(session)
    try:
        response = TestClient(app).post(
            "/search/shots",
            json={"query": "сборка металлической детали", "limit": 5},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "asset_id": str(asset_id),
            "shot_id": str(shot_id),
            "start_ms": 1250,
            "end_ms": 3100,
            "summary": description.summary,
            "similarity": 0.87,
            "embedding_model": "nomic-embed-text",
        }
    ]
    assert "shot_embeddings.embedding" in str(session.statement)
    assert "ORDER BY similarity DESC" in str(session.statement)
