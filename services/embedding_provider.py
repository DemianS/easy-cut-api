"""Локальный provider текстовых embeddings через Ollama."""

import ipaddress
import json
import math
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sql.models.shot_embedding import EMBEDDING_DIMENSIONS

DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 60
MAX_EMBEDDING_TEXT_LENGTH = 12_000
LOCAL_HOSTNAMES = {"localhost", "ollama"}


class EmbeddingProviderError(RuntimeError):
    """Безопасная ошибка локального embedding-provider."""


class EmbeddingProviderNotConfiguredError(EmbeddingProviderError):
    """Embedding-provider отключён или настроен небезопасно."""


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    base_url: str
    model: str
    timeout_seconds: int
    dimensions: int


def _is_local_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname in LOCAL_HOSTNAMES or hostname.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def get_embedding_settings() -> EmbeddingSettings:
    """Прочитать явно включённую локальную Ollama-конфигурацию."""
    if os.getenv("EMBEDDING_PROVIDER", "disabled").lower() != "ollama":
        raise EmbeddingProviderNotConfiguredError("Embedding-provider отключён")
    model = os.getenv("OLLAMA_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    if not model:
        raise EmbeddingProviderNotConfiguredError("OLLAMA_EMBEDDING_MODEL не задан")
    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not _is_local_hostname(
        parsed_url.hostname
    ):
        raise EmbeddingProviderNotConfiguredError(
            "Разрешён только локальный Ollama endpoint"
        )
    try:
        timeout_seconds = int(
            os.getenv(
                "EMBEDDING_TIMEOUT_SECONDS",
                str(DEFAULT_EMBEDDING_TIMEOUT_SECONDS),
            )
        )
        dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", str(EMBEDDING_DIMENSIONS)))
    except ValueError as error:
        raise EmbeddingProviderNotConfiguredError(
            "Параметры embedding должны быть целыми числами"
        ) from error
    if timeout_seconds <= 0:
        raise EmbeddingProviderNotConfiguredError(
            "EMBEDDING_TIMEOUT_SECONDS должен быть положительным"
        )
    if dimensions != EMBEDDING_DIMENSIONS:
        raise EmbeddingProviderNotConfiguredError(
            f"Для текущей схемы требуется размерность {EMBEDDING_DIMENSIONS}"
        )
    return EmbeddingSettings(base_url, model, timeout_seconds, dimensions)


class OllamaEmbeddingProvider:
    """Адаптер Ollama `/api/embed` с проверкой размерности ответа."""

    provider_name = "ollama"

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings

    def embed(self, text: str) -> list[float]:
        normalized_text = text.strip()
        if not normalized_text:
            raise EmbeddingProviderError("Нельзя построить embedding пустого текста")
        if len(normalized_text) > MAX_EMBEDDING_TEXT_LENGTH:
            raise EmbeddingProviderError("Текст для embedding слишком длинный")

        payload = {
            "model": self.settings.model,
            "input": normalized_text,
            "dimensions": self.settings.dimensions,
        }
        request = Request(
            f"{self.settings.base_url}/api/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw_response = json.loads(response.read().decode("utf-8"))
            embeddings = raw_response["embeddings"]
            vector = embeddings[0]
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise EmbeddingProviderError(
                "Локальная embedding-модель недоступна"
            ) from error
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise EmbeddingProviderError(
                "Embedding-модель вернула ответ неверного формата"
            ) from error

        if len(vector) != self.settings.dimensions:
            raise EmbeddingProviderError(
                "Embedding-модель вернула неверную размерность вектора"
            )
        try:
            validated_vector = [float(value) for value in vector]
        except (TypeError, ValueError) as error:
            raise EmbeddingProviderError(
                "Embedding-модель вернула нечисловой вектор"
            ) from error
        if not all(math.isfinite(value) for value in validated_vector):
            raise EmbeddingProviderError("Embedding-модель вернула некорректные числа")
        return validated_vector
