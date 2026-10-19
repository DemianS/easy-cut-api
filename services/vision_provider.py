"""Настройки и предметная валидация локального vision-provider."""

import base64
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.ollama_provider import OllamaProvider, OllamaProviderError
from services.prompt_loader import PromptError, load_prompt

DEFAULT_OLLAMA_BASE_URL = "http://ollama:11434"
DEFAULT_VISION_TIMEOUT_SECONDS = 180
SHOT_DESCRIPTION_PROMPT = "shot_description.ru.txt"
SHOT_DESCRIPTION_SCHEMA_VERSION = "shot-temporal-description-v1"
LOCAL_HOSTNAMES = {"localhost", "ollama"}


class VisionProviderError(RuntimeError):
    """Безопасная ошибка локального vision-provider."""


class VisionProviderNotConfiguredError(VisionProviderError):
    """Vision-модель не задана явно."""


class FrameObservation(BaseModel):
    """Признаки, непосредственно наблюдаемые на одном переданном кадре."""

    model_config = ConfigDict(extra="forbid")

    frame_id: UUID
    timestamp_ms: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=1000)
    visible_objects: list[str] = Field(default_factory=list, max_length=100)
    action: str | None = Field(default=None, max_length=1000)
    setting: str | None = Field(default=None, max_length=500)
    composition: str | None = Field(default=None, max_length=500)
    visible_text: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)


class FrameTransition(BaseModel):
    """Изменения только между двумя соседними наблюдаемыми кадрами."""

    model_config = ConfigDict(extra="forbid")

    from_frame_id: UUID
    to_frame_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    changes: list[str] = Field(default_factory=list, max_length=100)
    object_motion: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class TemporalEvent(BaseModel):
    """Событие, ограниченное подтверждающими кадрами и интервалом плана."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=1000)
    evidence_frame_ids: list[UUID] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0, le=1)


EvidenceField = Literal[
    "summary",
    "action",
    "setting",
    "shot_type",
    "camera_motion",
    "object_motion",
    "stable_objects",
]


class DescriptionEvidence(BaseModel):
    """Связь агрегированного утверждения с исходными наблюдениями."""

    model_config = ConfigDict(extra="forbid")

    field: EvidenceField
    frame_ids: list[UUID] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0, le=1)


class VisionDescription(BaseModel):
    """Недоверенный временной ответ модели после строгой валидации."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    visible_objects: list[str] = Field(default_factory=list, max_length=100)
    stable_objects: list[str] = Field(default_factory=list, max_length=100)
    action: str | None = Field(default=None, max_length=1000)
    setting: str | None = Field(default=None, max_length=500)
    shot_type: str | None = Field(default=None, max_length=100)
    camera_motion: str | None = Field(default=None, max_length=100)
    object_motion: str | None = Field(default=None, max_length=1000)
    visible_text: list[str] = Field(default_factory=list, max_length=100)
    uncertainty: str | None = Field(default=None, max_length=1000)
    frame_observations: list[FrameObservation] = Field(min_length=1, max_length=20)
    frame_transitions: list[FrameTransition] = Field(
        default_factory=list,
        max_length=19,
    )
    temporal_events: list[TemporalEvent] = Field(default_factory=list, max_length=100)
    field_evidence: list[DescriptionEvidence] = Field(
        default_factory=list,
        max_length=100,
    )


@dataclass(frozen=True, slots=True)
class VisionSettings:
    base_url: str
    model: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class VisionFrame:
    id: UUID
    timestamp_ms: int
    image_path: Path


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


def _build_temporal_prompt(
    prompt_text: str,
    frames: list[VisionFrame],
    shot_start_ms: int,
    shot_end_ms: int,
) -> str:
    manifest = "\n".join(
        f"{index + 1}. frame_id={frame.id}, timestamp_ms={frame.timestamp_ms}"
        for index, frame in enumerate(frames)
    )
    return (
        f"{prompt_text}\n\n"
        f"Интервал плана: [{shot_start_ms}, {shot_end_ms}) мс.\n"
        "Кадры переданы в следующем хронологическом порядке:\n"
        f"{manifest}"
    )


def _validate_temporal_contract(
    description: VisionDescription,
    frames: list[VisionFrame],
    shot_start_ms: int,
    shot_end_ms: int,
) -> None:
    if description.camera_motion is not None or description.object_motion is not None:
        raise VisionProviderError(
            "Движение нельзя подтверждать без алгоритмического анализа"
        )
    if any(
        transition.object_motion is not None
        for transition in description.frame_transitions
    ):
        raise VisionProviderError(
            "Переход не может подменять алгоритмический анализ движения"
        )
    frame_by_id = {frame.id: frame for frame in frames}
    expected_ids = [frame.id for frame in frames]
    observed_ids = [item.frame_id for item in description.frame_observations]
    if observed_ids != expected_ids:
        raise VisionProviderError(
            "Vision-модель вернула неполный или неупорядоченный набор наблюдений"
        )
    for observation in description.frame_observations:
        if observation.timestamp_ms != frame_by_id[observation.frame_id].timestamp_ms:
            raise VisionProviderError("Vision-модель изменила временную метку кадра")

    adjacent_pairs = set(zip(expected_ids, expected_ids[1:]))
    for transition in description.frame_transitions:
        pair = (transition.from_frame_id, transition.to_frame_id)
        if pair not in adjacent_pairs:
            raise VisionProviderError("Переход ссылается не на соседние кадры")
        if (
            transition.start_ms != frame_by_id[transition.from_frame_id].timestamp_ms
            or transition.end_ms != frame_by_id[transition.to_frame_id].timestamp_ms
        ):
            raise VisionProviderError("Переход изменяет временные метки кадров")
        if not shot_start_ms <= transition.start_ms < transition.end_ms <= shot_end_ms:
            raise VisionProviderError("Переход выходит за границы плана")

    known_ids = set(expected_ids)
    for event in description.temporal_events:
        if not shot_start_ms <= event.start_ms < event.end_ms <= shot_end_ms:
            raise VisionProviderError("Событие выходит за границы плана")
        if not set(event.evidence_frame_ids) <= known_ids:
            raise VisionProviderError("Событие ссылается на неизвестный кадр")
    for evidence in description.field_evidence:
        if not set(evidence.frame_ids) <= known_ids:
            raise VisionProviderError("Evidence ссылается на неизвестный кадр")


class VisionDescriptionProvider:
    """Преобразовать результат Ollama в проверенное временное описание."""

    provider_name = OllamaProvider.provider_name

    def __init__(self, settings: VisionSettings) -> None:
        self.settings = settings
        self.ollama = OllamaProvider(settings.base_url, settings.timeout_seconds)

    def describe(
        self,
        frames: list[VisionFrame],
        *,
        shot_start_ms: int,
        shot_end_ms: int,
    ) -> tuple[VisionDescription, dict, str]:
        if not frames:
            raise VisionProviderError("Для описания не переданы кадры")
        if shot_start_ms < 0 or shot_end_ms <= shot_start_ms:
            raise VisionProviderError("Передан некорректный интервал плана")
        ordered_frames = sorted(frames, key=lambda frame: frame.timestamp_ms)
        if (
            ordered_frames != frames
            or len({frame.id for frame in frames}) != len(frames)
            or len({frame.timestamp_ms for frame in frames}) != len(frames)
        ):
            raise VisionProviderError("Кадры должны быть уникальны и упорядочены")
        if any(
            not shot_start_ms <= frame.timestamp_ms < shot_end_ms for frame in frames
        ):
            raise VisionProviderError("Кадр выходит за границы плана")
        try:
            encoded_images = [
                base64.b64encode(frame.image_path.read_bytes()).decode("ascii")
                for frame in frames
            ]
            prompt = load_prompt(SHOT_DESCRIPTION_PROMPT)
        except OSError as error:
            raise VisionProviderError("Репрезентативный кадр недоступен") from error
        except PromptError as error:
            raise VisionProviderError(str(error)) from error

        temporal_prompt = _build_temporal_prompt(
            prompt.text,
            frames,
            shot_start_ms,
            shot_end_ms,
        )
        try:
            result = self.ollama.generate(
                model=self.settings.model,
                prompt=temporal_prompt,
                images=encoded_images,
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
        _validate_temporal_contract(
            description,
            frames,
            shot_start_ms,
            shot_end_ms,
        )
        return description, result, prompt.version
