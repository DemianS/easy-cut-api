"""Настройки и предметная валидация локального vision-provider."""

import base64
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.ollama_provider import OllamaProvider, OllamaProviderError
from services.prompt_loader import PromptError, load_prompt

DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_VISION_TIMEOUT_SECONDS = 180
SHOT_DESCRIPTION_PROMPT = "shot_description.ru.txt"
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


class VisionDescriptionProvider:
    """Преобразовать результат общего Ollama provider в описание плана."""

    provider_name = OllamaProvider.provider_name

    def __init__(self, settings: VisionSettings) -> None:
        self.settings = settings
        self.ollama = OllamaProvider(settings.base_url, settings.timeout_seconds)

    def describe(
        self,
        image_path: Path,
    ) -> tuple[VisionDescription, dict, str]:
        try:
            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
            prompt = load_prompt(SHOT_DESCRIPTION_PROMPT)
        except OSError as error:
            raise VisionProviderError("Репрезентативный кадр недоступен") from error
        except PromptError as error:
            raise VisionProviderError(str(error)) from error

        try:
            result = self.ollama.generate(
                model=self.settings.model,
                prompt=prompt.text,
                images=[encoded_image],
                response_format=VisionDescription.model_json_schema(),
            )
            content = result["response"].strip()
            description = VisionDescription.model_validate_json(content)
        except OllamaProviderError as error:
            raise VisionProviderError(str(error)) from error
        except (KeyError, AttributeError, TypeError, ValidationError) as error:
            raise VisionProviderError(
                "Vision-модель вернула ответ неверного формата"
            ) from error
        return description, result, prompt.version
