"""Ограниченный FFmpeg-renderer канонического timeline."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from time import monotonic
from uuid import UUID

from schemas.montage import Timeline
from services.logging_config import log_event, LogStatus
from services.montage_planner import MontageError

logger = logging.getLogger(__name__)
MAX_WORK_BYTES = 4 * 1024**3
RENDER_TIMEOUT_SECONDS = 1800
OUTPUT_TOLERANCE_MS = 120


@dataclass(frozen=True)
class RenderSource:
    path: Path
    size_bytes: int
    modified_at_ns: int
    has_audio: bool


def artifact_root() -> Path:
    return Path(os.getenv("ARTIFACTS_PATH", "/data/artifacts")).resolve()


def managed_artifact(relative_path: str, *, must_exist: bool = True) -> Path:
    root = artifact_root()
    try:
        path = (root / relative_path).resolve(strict=must_exist)
        path.relative_to(root)
        if path == root or (must_exist and not path.is_file()):
            raise ValueError("not an artifact")
        return path
    except (OSError, ValueError) as error:
        raise MontageError("Артефакт недоступен") from error


def _run(command: list[str], work: Path, deadline: float) -> bytes:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as error:
        raise MontageError("Не удалось запустить FFmpeg/ffprobe") from error
    try:
        while True:
            if monotonic() > deadline:
                raise MontageError("Превышено время рендера")
            size = sum(
                path.stat().st_size for path in work.rglob("*") if path.is_file()
            )
            if size > MAX_WORK_BYTES:
                raise MontageError("Превышен лимит временных файлов рендера")
            try:
                stdout, _stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            raise MontageError("FFmpeg/ffprobe завершился с ошибкой")
        return stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def validate_output(
    path: Path, timeline: Timeline, work: Path, deadline: float
) -> None:
    output = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        work,
        deadline,
    )
    try:
        metadata = json.loads(output)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        audio = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
        actual_duration = float(metadata["format"]["duration"]) * 1000
        valid = (
            video["codec_name"] == "h264"
            and video["width"] == 1920
            and video["height"] == 1080
            and video["pix_fmt"] == "yuv420p"
            and video["avg_frame_rate"] == "25/1"
            and audio["codec_name"] == "aac"
            and audio["sample_rate"] == "48000"
            and audio["channels"] == 2
            and abs(actual_duration - timeline.duration_ms) <= OUTPUT_TOLERANCE_MS
        )
    except (ValueError, KeyError, StopIteration, TypeError) as error:
        raise MontageError("Не удалось проверить выходное видео") from error
    if not valid or path.stat().st_size == 0:
        raise MontageError("Результат рендера не соответствует профилю")


def _check_sources(sources: dict[UUID, RenderSource]) -> None:
    for source in sources.values():
        stat = source.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (
            source.size_bytes,
            source.modified_at_ns,
        ):
            raise MontageError("Источник изменился после построения timeline")


def render_timeline(
    version_id: UUID,
    timeline: Timeline,
    sources: dict[UUID, RenderSource],
) -> str:
    relative_path = f"renders/{version_id}/final.mp4"
    destination = managed_artifact(relative_path, must_exist=False)
    work = managed_artifact(f"render-work/{version_id}", must_exist=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + RENDER_TIMEOUT_SECONDS
    _check_sources(sources)
    if destination.is_file():
        validate_output(destination, timeline, destination.parent, deadline)
        return relative_path
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        for index, clip in enumerate(timeline.clips):
            source = sources[clip.asset_id]
            duration = (clip.source_end_ms - clip.source_start_ms) / 1000
            part = work / f"part-{index:03d}.mp4"
            command = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{clip.source_start_ms / 1000:.3f}",
                "-i",
                str(source.path),
            ]
            if not source.has_audio:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            command += [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if source.has_audio else "1:a:0",
                "-t",
                f"{duration:.3f}",
                "-vf",
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,"
                "setpts=PTS-STARTPTS",
                "-af",
                "aresample=48000:async=1:first_pts=0,apad",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "25",
                "-video_track_timescale",
                "25000",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-threads",
                "2",
                str(part),
            ]
            _run(command, work, deadline)
            log_event(
                logger,
                logging.INFO,
                "montage_render",
                LogStatus.PROGRESS,
                "Фрагмент нормализован для склейки",
                stage="encode",
                completed=index + 1,
                total=len(timeline.clips),
            )
        # Все имена сгенерированы backend, пользовательские пути в manifest не входят.
        manifest = work / "concat.txt"
        manifest.write_text(
            "".join(
                f"file 'part-{index:03d}.mp4'\n" for index in range(len(timeline.clips))
            ),
            encoding="utf-8",
        )
        temporary = work / "final.mp4"
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(manifest),
                "-t",
                f"{timeline.duration_ms / 1000:.3f}",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            work,
            deadline,
        )
        _check_sources(sources)
        validate_output(temporary, timeline, work, deadline)
        os.replace(temporary, destination)
        return relative_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
