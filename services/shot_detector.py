"""Детекция планов и извлечение репрезентативных кадров."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from scenedetect import ContentDetector, detect
from scenedetect import __version__ as scenedetect_version

DEFAULT_CONTENT_THRESHOLD = 27.0
DEFAULT_MIN_SHOT_LENGTH_SECONDS = 0.5
DEFAULT_FRAME_EXTRACTION_TIMEOUT_SECONDS = 120
REPRESENTATIVE_FRAME_MAX_WIDTH = 1280


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


def detect_shots(
    media_path: Path,
    *,
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_shot_length_seconds: float = DEFAULT_MIN_SHOT_LENGTH_SECONDS,
) -> list[ShotBoundary]:
    """Найти непрерывные планы, включая видео без обнаруженных склеек."""
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
    """Атомарно извлечь средний кадр плана через FFmpeg без shell."""
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
        raise ShotDetectionError("Не удалось запустить извлечение кадра") from error
    if completed_process.returncode != 0 or not temporary_path.is_file():
        temporary_path.unlink(missing_ok=True)
        raise ShotDetectionError("FFmpeg не смог извлечь репрезентативный кадр")

    os.replace(temporary_path, destination)
    width, height = _scaled_dimensions(source_width, source_height)
    return ExtractedFrame(artifact_path, width, height)
