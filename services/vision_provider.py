"""Локальный provider структурированных описаний репрезентативных кадров."""

import base64
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

    def describe(self, image_path: Path) -> tuple[VisionDescription, dict]:
        try:
            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError as error:
            raise VisionProviderError("Репрезентативный кадр недоступен") from error

        schema = VisionDescription.model_json_schema()
        payload = {
            "model": self.settings.model,
            "stream": False,
            "format": schema,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Опиши только то, что достоверно видно на кадре. "
                        "Не выдумывай события до или после кадра. Ответь на русском "
                        "по переданной JSON Schema. camera_motion укажи только если "
                        "это можно обоснованно определить по одному кадру, иначе null."
                    ),
                    "images": [encoded_image],
                }
            ],
        }
        request = Request(
            f"{self.settings.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw_response = json.loads(response.read().decode("utf-8"))
            content = raw_response["message"]["content"]
            description = VisionDescription.model_validate_json(content)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise VisionProviderError("Локальная vision-модель недоступна") from error
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
            raise VisionProviderError(
                "Vision-модель вернула ответ неверного формата"
            ) from error
        return description, raw_response
