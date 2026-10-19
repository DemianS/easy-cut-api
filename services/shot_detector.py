"""Детекция планов и извлечение репрезентативных кадров."""

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import subprocess
from time import monotonic

from scenedetect import ContentDetector, detect
from scenedetect import __version__ as scenedetect_version

from services.logging_config import log_event, LogStatus

logger = logging.getLogger(__name__)

DEFAULT_CONTENT_THRESHOLD = 27.0
DEFAULT_MIN_SHOT_LENGTH_SECONDS = 0.5
DEFAULT_FRAME_EXTRACTION_TIMEOUT_SECONDS = 120
REPRESENTATIVE_FRAME_MAX_WIDTH = 1280
FRAME_SAMPLING_VERSION = "multi-frame-v1"
DEFAULT_SHORT_SHOT_THRESHOLD_MS = 1500
DEFAULT_FRAME_SAMPLE_INTERVAL_MS = 5000
DEFAULT_FRAME_SAMPLE_MIN_STEP_MS = 500
DEFAULT_MAX_FRAMES_PER_SHOT = 8


class ShotDetectionError(RuntimeError):
    """Ошибка анализа видеоряда или извлечения кадра."""


@dataclass(frozen=True, slots=True)
class ShotBoundary:
    sequence: int
    start_ms: int
    end_ms: int

    @property
    def middle_ms(self) -> int:
        return self.start_ms + (self.end_ms - self.start_ms) // 2


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    artifact_path: str
    width: int
    height: int
    reused: bool = False


@dataclass(frozen=True, slots=True)
class FrameSamplingSettings:
    short_shot_threshold_ms: int = DEFAULT_SHORT_SHOT_THRESHOLD_MS
    sample_interval_ms: int = DEFAULT_FRAME_SAMPLE_INTERVAL_MS
    min_step_ms: int = DEFAULT_FRAME_SAMPLE_MIN_STEP_MS
    max_frames_per_shot: int = DEFAULT_MAX_FRAMES_PER_SHOT

    def __post_init__(self) -> None:
        values = (
            self.short_shot_threshold_ms,
            self.sample_interval_ms,
            self.min_step_ms,
            self.max_frames_per_shot,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Параметры выборки кадров должны быть положительными")
        if self.max_frames_per_shot < 3:
            raise ValueError("Бюджет кадров обычного плана не может быть меньше трёх")

    def as_dict(self) -> dict[str, int]:
        """Вернуть сериализуемый снимок фактической конфигурации."""
        return {
            "short_shot_threshold_ms": self.short_shot_threshold_ms,
            "sample_interval_ms": self.sample_interval_ms,
            "min_step_ms": self.min_step_ms,
            "max_frames_per_shot": self.max_frames_per_shot,
        }


@dataclass(frozen=True, slots=True)
class FrameSample:
    sequence: int
    timestamp_ms: int
    role: str
    selection_reason: str


def select_frame_samples(
    boundary: ShotBoundary,
    settings: FrameSamplingSettings,
) -> list[FrameSample]:
    """Детерминированно выбрать упорядоченные точки внутри полуинтервала."""
    duration_ms = boundary.end_ms - boundary.start_ms
    if duration_ms <= 0:
        raise ShotDetectionError("Невозможно выбрать кадры пустого плана")
    if duration_ms < settings.short_shot_threshold_ms:
        return [
            FrameSample(
                sequence=0,
                timestamp_ms=boundary.middle_ms,
                role="single",
                selection_reason="short_shot_middle",
            )
        ]

    edge_margin_ms = min(250, max(1, duration_ms // 10))
    first_ms = boundary.start_ms + edge_margin_ms
    last_ms = boundary.end_ms - edge_margin_ms
    usable_duration_ms = last_ms - first_ms
    interval_count = math.ceil(duration_ms / settings.sample_interval_ms)
    desired_count = max(3, interval_count + 1)
    spacing_limited_count = usable_duration_ms // settings.min_step_ms + 1
    if spacing_limited_count < 3:
        return [
            FrameSample(
                sequence=0,
                timestamp_ms=boundary.middle_ms,
                role="single",
                selection_reason="min_step_limited_middle",
            )
        ]
    frame_count = min(
        desired_count,
        settings.max_frames_per_shot,
        spacing_limited_count,
    )

    samples = []
    for sequence in range(frame_count):
        timestamp_ms = round(
            first_ms + usable_duration_ms * sequence / (frame_count - 1)
        )
        if sequence == 0:
            role = "start"
            reason = "shot_start_context"
        elif sequence == frame_count - 1:
            role = "end"
            reason = "shot_end_context"
        elif frame_count % 2 and sequence == frame_count // 2:
            role = "middle"
            reason = "shot_middle_context"
        else:
            role = "sample"
            reason = "long_shot_uniform_coverage"
        samples.append(FrameSample(sequence, timestamp_ms, role, reason))
    return samples


def detect_shots(
    media_path: Path,
    *,
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_shot_length_seconds: float = DEFAULT_MIN_SHOT_LENGTH_SECONDS,
) -> list[ShotBoundary]:
    """Найти непрерывные планы, включая видео без обнаруженных склеек."""
    started_at = monotonic()
    log_event(
        logger,
        logging.INFO,
        "scene_detection",
        LogStatus.STARTED,
        "Поиск границ планов начат",
        stage="pyscenedetect",
    )
    try:
        scenes = detect(
            str(media_path),
            ContentDetector(
                threshold=threshold,
                min_scene_len=min_shot_length_seconds,
            ),
            show_progress=False,
            start_in_scene=True,
        )
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "scene_detection",
            LogStatus.FAILED,
            "Поиск границ планов завершился ошибкой",
            stage="pyscenedetect",
            error_type=type(error).__name__,
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        raise ShotDetectionError("Не удалось определить границы планов") from error

    boundaries = []
    for sequence, (start_time, end_time) in enumerate(scenes):
        start_ms = round(start_time.get_seconds() * 1000)
        end_ms = round(end_time.get_seconds() * 1000)
        if start_ms < 0 or end_ms <= start_ms:
            raise ShotDetectionError("Детектор вернул некорректный интервал")
        boundaries.append(ShotBoundary(sequence, start_ms, end_ms))
    if not boundaries:
        raise ShotDetectionError("Детектор не вернул ни одного плана")
    log_event(
        logger,
        logging.INFO,
        "scene_detection",
        LogStatus.SUCCEEDED,
        "Поиск границ планов успешно завершён",
        stage="pyscenedetect",
        shots=len(boundaries),
        elapsed_ms=round((monotonic() - started_at) * 1000),
    )
    return boundaries


def detector_metadata(
    threshold: float,
    min_shot_length_seconds: float,
) -> tuple[str, str, dict]:
    """Вернуть воспроизводимые параметры текущего детектора."""
    return (
        "pyscenedetect.ContentDetector",
        scenedetect_version,
        {
            "threshold": threshold,
            "min_shot_length_seconds": min_shot_length_seconds,
        },
    )


def _scaled_dimensions(width: int, height: int) -> tuple[int, int]:
    if width <= REPRESENTATIVE_FRAME_MAX_WIDTH:
        return width, height
    scaled_height = round(height * REPRESENTATIVE_FRAME_MAX_WIDTH / width)
    if scaled_height % 2:
        scaled_height += 1
    return REPRESENTATIVE_FRAME_MAX_WIDTH, max(2, scaled_height)


def extract_representative_frame(
    media_path: Path,
    destination: Path,
    *,
    timestamp_ms: int,
    source_width: int,
    source_height: int,
    artifact_path: str,
    timeout_seconds: int = DEFAULT_FRAME_EXTRACTION_TIMEOUT_SECONDS,
) -> ExtractedFrame:
    """Идемпотентно и атомарно извлечь кадр через FFmpeg без shell."""
    started_at = monotonic()
    log_event(
        logger,
        logging.DEBUG,
        "frame_extraction",
        LogStatus.STARTED,
        "Извлечение репрезентативного кадра начато",
        stage="ffmpeg",
        timeout_seconds=timeout_seconds,
    )
    width, height = _scaled_dimensions(source_width, source_height)
    if destination.is_file() and destination.stat().st_size > 0:
        log_event(
            logger,
            logging.DEBUG,
            "frame_extraction",
            LogStatus.REUSED,
            "Существующий кадр переиспользован",
            stage="artifact",
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        return ExtractedFrame(artifact_path, width, height, reused=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_suffix(".tmp.jpg")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media_path),
        "-ss",
        f"{timestamp_ms / 1000:.3f}",
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({REPRESENTATIVE_FRAME_MAX_WIDTH},iw)':-2",
        "-q:v",
        "2",
        "-y",
        str(temporary_path),
    ]
    try:
        completed_process = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        temporary_path.unlink(missing_ok=True)
        log_event(
            logger,
            logging.ERROR,
            "frame_extraction",
            LogStatus.FAILED,
            "Извлечение репрезентативного кадра завершилось ошибкой",
            stage="ffmpeg",
            error_type=type(error).__name__,
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        raise ShotDetectionError("Не удалось запустить извлечение кадра") from error
    if completed_process.returncode != 0 or not temporary_path.is_file():
        temporary_path.unlink(missing_ok=True)
        log_event(
            logger,
            logging.ERROR,
            "frame_extraction",
            LogStatus.FAILED,
            "FFmpeg не создал репрезентативный кадр",
            stage="ffmpeg",
            return_code=completed_process.returncode,
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        raise ShotDetectionError("FFmpeg не смог извлечь репрезентативный кадр")

    os.replace(temporary_path, destination)
    log_event(
        logger,
        logging.DEBUG,
        "frame_extraction",
        LogStatus.SUCCEEDED,
        "Извлечение репрезентативного кадра успешно завершено",
        stage="ffmpeg",
        elapsed_ms=round((monotonic() - started_at) * 1000),
    )
    return ExtractedFrame(artifact_path, width, height)
