"""Безопасный запуск ffprobe и преобразование его JSON-ответа."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import json
from pathlib import Path
import subprocess

DEFAULT_FFPROBE_TIMEOUT_SECONDS = 120
MAX_DIAGNOSTIC_LENGTH = 2000
MAX_DATABASE_INTEGER = 2_147_483_647


class MediaProbeError(RuntimeError):
    """Базовая диагностическая ошибка ffprobe."""

    error_code = "asset_probe_failed"


class FfprobeUnavailableError(MediaProbeError):
    error_code = "ffprobe_unavailable"


class FfprobeTimeoutError(MediaProbeError):
    error_code = "ffprobe_timeout"


class FfprobeExecutionError(MediaProbeError):
    error_code = "ffprobe_execution_failed"


class InvalidProbeOutputError(MediaProbeError):
    error_code = "invalid_ffprobe_output"


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    """Технические характеристики, извлечённые из ffprobe."""

    format_name: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    video_codec: str | None
    frame_rate_num: int | None
    frame_rate_den: int | None
    audio_codec: str | None
    sample_rate_hz: int | None
    audio_channels: int | None
    metadata: dict


def _positive_integer(value: object) -> int | None:
    try:
        parsed_value = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 0 < parsed_value <= MAX_DATABASE_INTEGER:
        return parsed_value
    return None


def _optional_string(value: object, max_length: int = 100) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:max_length]


def _duration_to_milliseconds(value: object) -> int | None:
    if value is None:
        return None
    try:
        duration = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not duration.is_finite() or duration < 0:
        return None
    return int((duration * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _frame_rate(stream: dict | None) -> tuple[int | None, int | None]:
    if stream is None:
        return None, None
    for field_name in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(field_name)
        if not value or value == "0/0":
            continue
        try:
            frame_rate = Fraction(str(value))
        except (ValueError, ZeroDivisionError):
            continue
        if frame_rate > 0:
            if (
                frame_rate.numerator <= MAX_DATABASE_INTEGER
                and frame_rate.denominator <= MAX_DATABASE_INTEGER
            ):
                return frame_rate.numerator, frame_rate.denominator
    return None, None


def parse_ffprobe_output(payload: dict) -> MediaProbeResult:
    """Преобразовать недоверенный JSON ffprobe в типизированный результат."""
    format_metadata = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_metadata, dict) or not isinstance(streams, list):
        raise InvalidProbeOutputError("ffprobe вернул неполную структуру JSON")

    valid_streams = [stream for stream in streams if isinstance(stream, dict)]
    video_stream = next(
        (stream for stream in valid_streams if stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in valid_streams if stream.get("codec_type") == "audio"),
        None,
    )
    frame_rate_num, frame_rate_den = _frame_rate(video_stream)
    duration_ms = _duration_to_milliseconds(format_metadata.get("duration"))
    if duration_ms is None:
        stream_durations = [
            duration
            for duration in (
                _duration_to_milliseconds(stream.get("duration"))
                for stream in valid_streams
            )
            if duration is not None
        ]
        duration_ms = max(stream_durations, default=None)

    return MediaProbeResult(
        format_name=_optional_string(format_metadata.get("format_name")),
        duration_ms=duration_ms,
        width=_positive_integer(video_stream.get("width")) if video_stream else None,
        height=(
            _positive_integer(video_stream.get("height")) if video_stream else None
        ),
        video_codec=(
            _optional_string(video_stream.get("codec_name")) if video_stream else None
        ),
        frame_rate_num=frame_rate_num,
        frame_rate_den=frame_rate_den,
        audio_codec=(
            _optional_string(audio_stream.get("codec_name")) if audio_stream else None
        ),
        sample_rate_hz=(
            _positive_integer(audio_stream.get("sample_rate")) if audio_stream else None
        ),
        audio_channels=(
            _positive_integer(audio_stream.get("channels")) if audio_stream else None
        ),
        metadata=payload,
    )


def probe_media_file(
    media_path: Path,
    timeout_seconds: int = DEFAULT_FFPROBE_TIMEOUT_SECONDS,
) -> MediaProbeResult:
    """Запустить ffprobe без shell и вернуть проверенные метаданные."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "format=format_name,duration,size:"
            "stream=index,codec_type,codec_name,width,height,duration,"
            "avg_frame_rate,r_frame_rate,sample_rate,channels"
        ),
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed_process = subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as error:
        raise FfprobeUnavailableError("Исполняемый файл ffprobe не найден") from error
    except subprocess.TimeoutExpired as error:
        raise FfprobeTimeoutError("ffprobe превысил допустимое время работы") from error

    if completed_process.returncode != 0:
        diagnostic = completed_process.stderr.replace(str(media_path), "<media>")
        diagnostic = diagnostic.strip()[:MAX_DIAGNOSTIC_LENGTH]
        raise FfprobeExecutionError(diagnostic or "ffprobe завершился с ошибкой")

    try:
        payload = json.loads(completed_process.stdout)
    except json.JSONDecodeError as error:
        raise InvalidProbeOutputError("ffprobe вернул некорректный JSON") from error
    if not isinstance(payload, dict):
        raise InvalidProbeOutputError("Корнем JSON ffprobe должен быть объект")
    return parse_ffprobe_output(payload)
