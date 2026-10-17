"""Безопасное обнаружение поддерживаемых медиафайлов."""

from dataclasses import dataclass
import logging
import mimetypes
import os
from pathlib import Path
from time import monotonic
import unicodedata

from services.logging_config import log_event, LogStatus
from sql.enums import AssetKind

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
MAX_REPORTED_ISSUES = 20


class MediaScanError(RuntimeError):
    """Базовая ошибка чтения медиатеки с машинным кодом."""

    error_code = "media_scan_failed"


class MediaRootUnavailableError(MediaScanError):
    """Корень медиатеки не существует или не является каталогом."""

    error_code = "media_root_unavailable"


class MediaRootOutsideAllowedPathError(MediaScanError):
    """Корень медиатеки выходит за границы разрешённого каталога."""

    error_code = "media_root_outside_allowed_path"


@dataclass(frozen=True, slots=True)
class DiscoveredMediaFile:
    """Метаданные файла, доступные без вызова ffprobe."""

    relative_path: str
    normalized_path: str
    size_bytes: int
    modified_at_ns: int
    media_kind: str
    format_name: str
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """Ошибка отдельного файла, не прерывающая всё сканирование."""

    relative_path: str
    message: str


@dataclass(frozen=True, slots=True)
class MediaScanResult:
    """Результат прохода по файловой системе."""

    files: tuple[DiscoveredMediaFile, ...]
    skipped_count: int
    issues: tuple[ScanIssue, ...]


def normalize_relative_path(relative_path: str) -> str:
    """Нормализовать путь для идемпотентного сравнения."""
    return unicodedata.normalize("NFC", relative_path).casefold()


def classify_media_file(extension: str) -> str:
    """Определить тип медиа по уже проверенному расширению."""
    if extension in VIDEO_EXTENSIONS:
        return AssetKind.VIDEO.value
    if extension in AUDIO_EXTENSIONS:
        return AssetKind.AUDIO.value
    if extension in IMAGE_EXTENSIONS:
        return AssetKind.IMAGE.value
    return AssetKind.UNKNOWN.value


def resolve_media_root(root_path: Path, allowed_root_path: Path) -> Path:
    """Проверить существование корня и его принадлежность разрешённой области."""
    try:
        allowed_root = allowed_root_path.resolve(strict=True)
        media_root = root_path.resolve(strict=True)
    except OSError as error:
        raise MediaRootUnavailableError(
            "Корень медиатеки или разрешённый каталог недоступен"
        ) from error

    if not allowed_root.is_dir() or not media_root.is_dir():
        raise MediaRootUnavailableError("Корень медиатеки должен быть каталогом")

    try:
        media_root.relative_to(allowed_root)
    except ValueError as error:
        raise MediaRootOutsideAllowedPathError(
            "Корень медиатеки находится вне разрешённого каталога"
        ) from error

    return media_root


def discover_media_files(
    root_path: Path,
    allowed_root_path: Path,
) -> MediaScanResult:
    """Рекурсивно найти медиа, не переходя по символическим ссылкам."""
    started_at = monotonic()
    log_event(
        logger,
        logging.INFO,
        "media_discovery",
        LogStatus.STARTED,
        "Обход файлов медиатеки начат",
        stage="filesystem",
    )
    try:
        media_root = resolve_media_root(root_path, allowed_root_path)
    except MediaScanError as error:
        log_event(
            logger,
            logging.ERROR,
            "media_discovery",
            LogStatus.FAILED,
            "Подготовка обхода медиатеки завершилась ошибкой",
            stage="root_validation",
            error_type=type(error).__name__,
        )
        raise
    discovered_files: list[DiscoveredMediaFile] = []
    discovered_normalized_paths: set[str] = set()
    issues: list[ScanIssue] = []
    skipped_count = 0

    def report_issue(relative_path: str, error: OSError) -> None:
        nonlocal skipped_count
        skipped_count += 1
        if len(issues) < MAX_REPORTED_ISSUES:
            error_message = error.strerror or str(error)
            issues.append(ScanIssue(relative_path, error_message))

    def handle_walk_error(error: OSError) -> None:
        error_path = Path(error.filename) if error.filename else media_root
        try:
            relative_path = error_path.relative_to(media_root).as_posix()
        except ValueError:
            relative_path = "."
        report_issue(relative_path, error)

    for current_path, directory_names, file_names in os.walk(
        media_root,
        followlinks=False,
        onerror=handle_walk_error,
    ):
        current_directory = Path(current_path)
        directory_names.sort(key=str.casefold)
        file_names.sort(key=str.casefold)
        directory_names[:] = [
            name
            for name in directory_names
            if not (current_directory / name).is_symlink()
        ]

        for file_name in file_names:
            file_path = current_directory / file_name
            extension = file_path.suffix.casefold()
            if extension not in SUPPORTED_EXTENSIONS:
                continue

            relative_path = file_path.relative_to(media_root).as_posix()
            normalized_path = normalize_relative_path(relative_path)
            if normalized_path in discovered_normalized_paths:
                report_issue(
                    relative_path,
                    OSError("Путь совпадает с другим файлом после нормализации"),
                )
                continue
            if file_path.is_symlink():
                report_issue(
                    relative_path,
                    OSError("Символические ссылки на медиафайлы не поддерживаются"),
                )
                continue

            try:
                file_stat = file_path.stat()
            except OSError as error:
                report_issue(relative_path, error)
                continue

            if not file_path.is_file():
                continue

            mime_type, _ = mimetypes.guess_type(file_name)
            discovered_normalized_paths.add(normalized_path)
            discovered_files.append(
                DiscoveredMediaFile(
                    relative_path=relative_path,
                    normalized_path=normalized_path,
                    size_bytes=file_stat.st_size,
                    modified_at_ns=file_stat.st_mtime_ns,
                    media_kind=classify_media_file(extension),
                    format_name=extension.removeprefix("."),
                    mime_type=mime_type,
                )
            )

    discovered_files.sort(key=lambda item: item.normalized_path)
    result = MediaScanResult(
        files=tuple(discovered_files),
        skipped_count=skipped_count,
        issues=tuple(issues),
    )
    log_event(
        logger,
        logging.INFO,
        "media_discovery",
        LogStatus.SUCCEEDED,
        "Обход файлов медиатеки успешно завершён",
        stage="filesystem",
        total=len(result.files),
        items_skipped=result.skipped_count,
        elapsed_ms=round((monotonic() - started_at) * 1000),
    )
    return result
