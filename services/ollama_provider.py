"""Единый локальный HTTP-provider для Ollama."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from services.logging_config import log_event, LogStatus

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
        log_event(
            logger,
            logging.INFO,
            "ollama_wait",
            LogStatus.STARTED,
            "Ожидание готовности Ollama начато",
            stage="healthcheck",
            total=max_retries,
        )
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(
                    f"{self.base_url}/api/tags",
                    timeout=DEFAULT_HEALTH_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                log_event(
                    logger,
                    logging.INFO,
                    "ollama_wait",
                    LogStatus.SUCCEEDED,
                    "Ollama готова к обработке запросов",
                    stage="healthcheck",
                    attempt=attempt,
                )
                return True
            except requests.RequestException as error:
                log_event(
                    logger,
                    logging.WARNING,
                    "ollama_wait",
                    LogStatus.RETRYING,
                    "Ollama пока недоступна",
                    stage="healthcheck",
                    attempt=attempt,
                    error_type=type(error).__name__,
                )
            if attempt < max_retries:
                sleep(delay_seconds)
        log_event(
            logger,
            logging.ERROR,
            "ollama_wait",
            LogStatus.FAILED,
            "Ожидание готовности Ollama завершилось ошибкой",
            stage="healthcheck",
            attempt=max_retries,
        )
        return False

    def pull_model(self, model_name: str) -> None:
        """Загрузить модель, последовательно читая NDJSON-прогресс Ollama."""
        started_at = time.monotonic()
        log_event(
            logger,
            logging.INFO,
            "ollama_pull",
            LogStatus.STARTED,
            "Загрузка модели Ollama начата",
            stage="download",
            model=model_name,
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
                    log_event(
                        logger,
                        logging.DEBUG,
                        "ollama_pull",
                        LogStatus.PROGRESS,
                        "Обновлён прогресс загрузки модели Ollama",
                        stage="download",
                        model=model_name,
                        provider_status=final_status,
                        completed=event.get("completed"),
                        total=event.get("total"),
                    )
        except (
            requests.RequestException,
            json.JSONDecodeError,
            OllamaProviderError,
        ) as error:
            log_event(
                logger,
                logging.ERROR,
                "ollama_pull",
                LogStatus.FAILED,
                "Загрузка модели Ollama завершилась ошибкой",
                stage="download",
                model=model_name,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(error).__name__,
            )
            raise OllamaProviderError(
                "Ollama недоступна для загрузки модели"
            ) from error
        log_event(
            logger,
            logging.INFO,
            "ollama_pull",
            LogStatus.SUCCEEDED,
            "Загрузка модели Ollama успешно завершена",
            stage="download",
            model=model_name,
            provider_status=final_status,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
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
        log_event(
            logger,
            logging.INFO,
            "ollama_generate",
            LogStatus.STARTED,
            "Генерация ответа Ollama начата",
            stage="request",
            model=model,
        )
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
            log_event(
                logger,
                logging.ERROR,
                "ollama_generate",
                LogStatus.FAILED,
                "Генерация ответа Ollama завершилась ошибкой",
                stage="request",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(error).__name__,
            )
            raise OllamaProviderError("Локальная модель Ollama недоступна") from error
        try:
            result = response.json()
        except ValueError as error:
            log_event(
                logger,
                logging.ERROR,
                "ollama_generate",
                LogStatus.FAILED,
                "Проверка ответа Ollama завершилась ошибкой",
                stage="validation",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(error).__name__,
            )
            raise OllamaProviderError(
                "Ollama вернула ответ неверного формата"
            ) from error
        if not isinstance(result, dict) or result.get("error"):
            log_event(
                logger,
                logging.ERROR,
                "ollama_generate",
                LogStatus.FAILED,
                "Проверка ответа Ollama завершилась ошибкой",
                stage="validation",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type="InvalidResponse",
            )
            raise OllamaProviderError("Ollama вернула ошибку генерации")
        log_event(
            logger,
            logging.INFO,
            "ollama_generate",
            LogStatus.SUCCEEDED,
            "Генерация ответа Ollama успешно завершена",
            stage="response",
            model=model,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result

    def embed(self, *, model: str, text: str, dimensions: int) -> list[Any]:
        """Получить один необработанный embedding через `/api/embed`."""
        started_at = time.monotonic()
        log_event(
            logger,
            logging.INFO,
            "ollama_embed",
            LogStatus.STARTED,
            "Построение embedding Ollama начато",
            stage="request",
            model=model,
        )
        try:
            response = requests.post(
                f"{self.base_url}/api/embed",
                json={"model": model, "input": text, "dimensions": dimensions},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            log_event(
                logger,
                logging.ERROR,
                "ollama_embed",
                LogStatus.FAILED,
                "Построение embedding Ollama завершилось ошибкой",
                stage="request",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(error).__name__,
            )
            raise OllamaProviderError("Локальная модель Ollama недоступна") from error
        try:
            result = response.json()
            embeddings = result["embeddings"]
            vector = embeddings[0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            log_event(
                logger,
                logging.ERROR,
                "ollama_embed",
                LogStatus.FAILED,
                "Проверка ответа embedding Ollama завершилась ошибкой",
                stage="validation",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(error).__name__,
            )
            raise OllamaProviderError(
                "Ollama вернула ответ неверного формата"
            ) from error
        if not isinstance(vector, list):
            log_event(
                logger,
                logging.ERROR,
                "ollama_embed",
                LogStatus.FAILED,
                "Проверка ответа embedding Ollama завершилась ошибкой",
                stage="validation",
                model=model,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_type="InvalidResponse",
            )
            raise OllamaProviderError("Ollama вернула неверный embedding")
        log_event(
            logger,
            logging.INFO,
            "ollama_embed",
            LogStatus.SUCCEEDED,
            "Построение embedding Ollama успешно завершено",
            stage="response",
            model=model,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        return vector
