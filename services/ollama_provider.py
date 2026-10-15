"""Единый локальный HTTP-provider для Ollama."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEALTH_TIMEOUT_SECONDS = 5
DEFAULT_PULL_CONNECT_TIMEOUT_SECONDS = 10


class OllamaProviderError(RuntimeError):
    """Безопасная ошибка взаимодействия с локальным Ollama API."""


class OllamaProvider:
    """Предоставить общие операции Ollama без предметной валидации ответа."""

    provider_name = "ollama"

    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def wait_for_ollama(
        self,
        max_retries: int = 30,
        delay_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Дождаться готовности API, не раскрывая тело ответа в логах."""
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(
                    f"{self.base_url}/api/tags",
                    timeout=DEFAULT_HEALTH_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                logger.info(
                    "Ollama готова",
                    extra={"operation": "wait", "attempt": attempt},
                )
                return True
            except requests.RequestException as error:
                logger.warning(
                    "Ollama пока недоступна",
                    extra={
                        "operation": "wait",
                        "attempt": attempt,
                        "error_type": type(error).__name__,
                    },
                )
            if attempt < max_retries:
                sleep(delay_seconds)
        return False

    def pull_model(self, model_name: str) -> None:
        """Загрузить модель, последовательно читая NDJSON-прогресс Ollama."""
        logger.info(
            "Начата загрузка модели Ollama",
            extra={"operation": "pull", "model": model_name},
        )
        try:
            with requests.post(
                f"{self.base_url}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=(DEFAULT_PULL_CONNECT_TIMEOUT_SECONDS, None),
            ) as response:
                response.raise_for_status()
                final_status = None
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise OllamaProviderError(
                            "Ollama вернула неверный прогресс загрузки модели"
                        )
                    if event.get("error"):
                        raise OllamaProviderError("Ollama не смогла загрузить модель")
                    final_status = event.get("status")
                    logger.debug(
                        "Обновлён прогресс загрузки модели Ollama",
                        extra={
                            "operation": "pull",
                            "model": model_name,
                            "status": final_status,
                            "completed": event.get("completed"),
                            "total": event.get("total"),
                        },
                    )
        except (requests.RequestException, json.JSONDecodeError) as error:
            raise OllamaProviderError(
                "Ollama недоступна для загрузки модели"
            ) from error
        logger.info(
            "Модель Ollama загружена",
            extra={
                "operation": "pull",
                "model": model_name,
                "status": final_status,
            },
        )

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        images: list[str],
        response_format: dict[str, Any],
        think: bool = False,
    ) -> dict[str, Any]:
        """Получить целостный структурированный ответ через `/api/generate`."""
        started_at = time.monotonic()
        payload = {
            "model": model,
            "prompt": prompt,
            "format": response_format,
            "images": images,
            "think": think,
            "stream": False,
        }
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            logger.warning(
                "Ошибка generate-запроса к Ollama",
                extra={
                    "operation": "generate",
                    "model": model,
                    "error_type": type(error).__name__,
                },
            )
            raise OllamaProviderError("Локальная модель Ollama недоступна") from error
        try:
            result = response.json()
        except ValueError as error:
            raise OllamaProviderError(
                "Ollama вернула ответ неверного формата"
            ) from error
        if not isinstance(result, dict) or result.get("error"):
            raise OllamaProviderError("Ollama вернула ошибку генерации")
        logger.info(
            "Generate-запрос Ollama завершён",
            extra={
                "operation": "generate",
                "model": model,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        return result

    def embed(self, *, model: str, text: str, dimensions: int) -> list[Any]:
        """Получить один необработанный embedding через `/api/embed`."""
        started_at = time.monotonic()
        try:
            response = requests.post(
                f"{self.base_url}/api/embed",
                json={"model": model, "input": text, "dimensions": dimensions},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            logger.warning(
                "Ошибка embed-запроса к Ollama",
                extra={
                    "operation": "embed",
                    "model": model,
                    "error_type": type(error).__name__,
                },
            )
            raise OllamaProviderError("Локальная модель Ollama недоступна") from error
        try:
            result = response.json()
            embeddings = result["embeddings"]
            vector = embeddings[0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            logger.warning(
                "Некорректный embed-ответ Ollama",
                extra={
                    "operation": "embed",
                    "model": model,
                    "error_type": type(error).__name__,
                },
            )
            raise OllamaProviderError(
                "Ollama вернула ответ неверного формата"
            ) from error
        if not isinstance(vector, list):
            raise OllamaProviderError("Ollama вернула неверный embedding")
        logger.info(
            "Embed-запрос Ollama завершён",
            extra={
                "operation": "embed",
                "model": model,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        return vector
