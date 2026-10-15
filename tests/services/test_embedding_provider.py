import pytest

from services.embedding_provider import EmbeddingProviderError
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import EmbeddingSettings, TextEmbeddingProvider
from services.embedding_provider import get_embedding_settings


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

    def fake_embed(self, **payload):
        captured_payload.update(payload)
        return [0.25, -0.5, 0.75]

    monkeypatch.setattr("services.embedding_provider.OllamaProvider.embed", fake_embed)
    provider = TextEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    vector = provider.embed("металлическая деталь")

    assert vector == [0.25, -0.5, 0.75]
    assert captured_payload == {
        "model": "embedding-model",
        "text": "металлическая деталь",
        "dimensions": 3,
    }


def test_ollama_embedding_provider_rejects_wrong_dimension(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.embedding_provider.OllamaProvider.embed",
        lambda *args, **kwargs: [0.25, -0.5],
    )
    provider = TextEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    with pytest.raises(EmbeddingProviderError, match="размерность"):
        provider.embed("запрос")


def test_ollama_embedding_provider_rejects_empty_text() -> None:
    provider = TextEmbeddingProvider(
        EmbeddingSettings("http://ollama:11434", "embedding-model", 30, 3)
    )

    with pytest.raises(EmbeddingProviderError, match="пустого"):
        provider.embed("   ")
