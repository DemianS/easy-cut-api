"""Локальный provider структурированных описаний репрезентативных кадров."""

import base64
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_VISION_TIMEOUT_SECONDS = 180
PROMPT_VERSION = "shot-description-v1"
LOCAL_HOSTNAMES = {"localhost", "ollama"}


class VisionProviderError(RuntimeError):
    """Безопасная ошибка локального vision-provider."""


class VisionProviderNotConfiguredError(VisionProviderError):
    """Vision-модель не задана явно."""


class VisionDescription(BaseModel):
    """Недоверенный ответ модели после строгой валидации."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    visible_objects: list[str] = Field(default_factory=list, max_length=100)
    action: str | None = Field(default=None, max_length=1000)
    setting: str | None = Field(default=None, max_length=500)
    shot_type: str | None = Field(default=None, max_length=100)
    camera_motion: str | None = Field(default=None, max_length=100)
    visible_text: list[str] = Field(default_factory=list, max_length=100)
    uncertainty: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True, slots=True)
class VisionSettings:
    base_url: str
    model: str
    timeout_seconds: int


def _is_local_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname in LOCAL_HOSTNAMES or hostname.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def get_vision_settings() -> VisionSettings:
    """Прочитать только явно включённую локальную Ollama-конфигурацию."""
    if os.getenv("VISION_PROVIDER", "disabled").lower() != "ollama":
        raise VisionProviderNotConfiguredError("Vision-provider отключён")
    model = os.getenv("OLLAMA_VISION_MODEL", "").strip()
    if not model:
        raise VisionProviderNotConfiguredError("OLLAMA_VISION_MODEL не задан")
    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not _is_local_hostname(
        parsed_url.hostname
    ):
        raise VisionProviderNotConfiguredError(
            "Разрешён только локальный Ollama endpoint"
        )
    try:
        timeout_seconds = int(
            os.getenv("VISION_TIMEOUT_SECONDS", str(DEFAULT_VISION_TIMEOUT_SECONDS))
        )
    except ValueError as error:
        raise VisionProviderNotConfiguredError(
            "VISION_TIMEOUT_SECONDS должен быть целым числом"
        ) from error
    if timeout_seconds <= 0:
        raise VisionProviderNotConfiguredError(
            "VISION_TIMEOUT_SECONDS должен быть положительным"
        )
    return VisionSettings(base_url, model, timeout_seconds)


class OllamaVisionProvider:
    """Минимальный адаптер Ollama `/api/chat` без облачной отправки."""

    provider_name = "ollama"

    def __init__(self, settings: VisionSettings) -> None:
        self.settings = settings

    def wait_for_ollama(self, max_retries: int = 30, delay: int = 5) -> bool:
        """
        Ожидает доступности Ollama API.

        :param ollama_url: URL API Ollama
        :param max_retries: Максимальное количество попыток
        :param delay: Задержка между попытками в секундах
        :return: True если Ollama доступен, иначе False
        """
        ollama_url = self.settings.base_url
        for i in range(max_retries):
            try:
                resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
                if resp.status_code == 200:
                    logger.info("✅ Ollama is ready")
                    return True
            except Exception as e:
                logger.warning(f"Ollama not ready: {repr(e)}")
            logger.info(f"Waiting for Ollama... ({i + 1}/{max_retries})")
            time.sleep(delay)
        return False


    def pull_model(self, model_name: str) -> bool:
        """
        Загружает модель в Ollama с отображением прогресса.

        :param ollama_url: URL API Ollama
        :param model_name: Название модели для загрузки
        :return: True если модель успешно загружена
        """
        logger.info(f"Pulling model {model_name} (this may take several minutes)...")
        pbar = None
        ollama_url = self.settings.base_url

        try:
            resp = requests.post(
                f"{ollama_url}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=None
            )
            resp.raise_for_status()

            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line.decode('utf-8'))
                    status = data.get('status', '')

                    if status in ('downloading digest', 'verifying sha256', 'writing manifest', 'success'):
                        logger.info(f"[{model_name}] {status}")
                    elif status == 'pulling manifest':
                        logger.info(f"[{model_name}] Pulling manifest...")

                except json.JSONDecodeError:
                    continue

            logger.info(f"✅ Model {model_name} successfully pulled and ready")
            return True

        except Exception as e:
            logger.error(f"❌ Pull error for {model_name}: {repr(e)}")
            return False
        finally:
            if pbar:
                pbar.close()

    def describe(self, image_path: Path, timeout: int = 300) -> tuple[VisionDescription, dict]:
        try:
            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError as error:
            raise VisionProviderError("Репрезентативный кадр недоступен") from error

        schema = VisionDescription.model_json_schema()
        
        payload = {
            "model": self.settings.model,
            "prompt": (
                "Опиши только то, что достоверно видно на кадре. "
                "Не выдумывай события до или после кадра. Ответь на русском "
                "по переданной JSON Schema. camera_motion укажи только если "
                "это можно обоснованно определить по одному кадру, иначе null."
                "Описание впиши в summary, если предполагается какое-то действие в кадре, впиши его в action. "
                "Опиши картинку и её стиль в setting. shot_type объясняет какой взят план, крупный, средний, общий, макро или какой-либо другой"
                "всё что не вписывается в поля выше впиши в uncertainty"
            ),
            "format": schema,
            "images": [encoded_image],
            "think": False,
            "stream": False,
        }
        
        try:
            response = requests.post(f"{self.settings.base_url}/api/generate", json=payload, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                raise VisionProviderError(f"Ollama error: {result['error']}")
            logger.info(f"LLM Response: {result}")
            content = result["response"].strip()
            description = VisionDescription.model_validate_json(content)
        except requests.Timeout as error:
            logger.warning(f"Timeout warming up {self.settings.model})")
            raise VisionProviderError("Локальная vision-модель недоступна") from error
        except requests.RequestException as error:
            logger.warning(f"Request error: {repr(error)}")
            raise VisionProviderError("Локальная vision-модель недоступна") from error
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise VisionProviderError("Локальная vision-модель недоступна") from error
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
            logger.error(f"Vision provider error: {repr(error)}")
            raise VisionProviderError(
                "Vision-модель вернула ответ неверного формата"
            ) from error
        except Exception as error:
            logger.error(f"Unexpected error: {repr(error)}")
            raise VisionProviderError("Локальная vision-модель недоступна") from error
        return description, result
