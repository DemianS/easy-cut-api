import json

import pytest

from services.embedding_provider import EmbeddingProviderError
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import EmbeddingSettings, OllamaEmbeddingProvider
from services.embedding_provider import get_embedding_settings


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_embedding_settings_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    with pytest.raises(EmbeddingProviderNotConfiguredError):
        get_embedding_settings()


def test_embedding_settings_reject_remote_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://example.com")

    with pytest.raises(EmbeddingProviderNotConfiguredError):
        get_embedding_settings()


def test_ollama_embedding_provider_validates_response(monkeypatch) -> None:
    captured_payload = {}

    def fake_urlopen(request, timeout):
        captured_payload.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse({"embeddings": [[0.25, -0.5, 0.75]]})

    monkeypatch.setattr("services.embedding_provider.urlopen", fake_urlopen)
    provider = OllamaEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    vector = provider.embed("металлическая деталь")

    assert vector == [0.25, -0.5, 0.75]
    assert captured_payload == {
        "model": "embedding-model",
        "input": "металлическая деталь",
        "dimensions": 3,
    }


def test_ollama_embedding_provider_rejects_wrong_dimension(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.embedding_provider.urlopen",
        lambda *args, **kwargs: FakeResponse({"embeddings": [[0.25, -0.5]]}),
    )
    provider = OllamaEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    with pytest.raises(EmbeddingProviderError, match="размерность"):
        provider.embed("запрос")


def test_ollama_embedding_provider_rejects_empty_text() -> None:
    provider = OllamaEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    with pytest.raises(EmbeddingProviderError, match="пустого"):
        provider.embed("   ")
