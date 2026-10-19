"""Потоковый checksum локального файла без загрузки целиком в память."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path

CHECKSUM_ALGORITHM = "sha256"
DEFAULT_CHECKSUM_CHUNK_SIZE_BYTES = 8 * 1024 * 1024


class FileChecksumError(RuntimeError):
    """Checksum не удалось вычислить без раскрытия локального пути."""

    error_code = "checksum_failed"


@dataclass(frozen=True, slots=True)
class FileChecksum:
    algorithm: str
    digest: str
    size_bytes: int


def calculate_file_checksum(
    file_path: Path,
    *,
    chunk_size_bytes: int = DEFAULT_CHECKSUM_CHUNK_SIZE_BYTES,
    progress_callback: Callable[[int], None] | None = None,
) -> FileChecksum:
    """Вычислить полный SHA-256, читая файл ограниченными блоками."""
    if chunk_size_bytes <= 0:
        raise ValueError("Размер блока checksum должен быть положительным")

    digest = hashlib.sha256()
    processed_bytes = 0
    try:
        with file_path.open("rb") as media_file:
            while chunk := media_file.read(chunk_size_bytes):
                digest.update(chunk)
                processed_bytes += len(chunk)
                if progress_callback is not None:
                    progress_callback(processed_bytes)
    except OSError as error:
        raise FileChecksumError("Не удалось прочитать файл для checksum") from error

    return FileChecksum(
        algorithm=CHECKSUM_ALGORITHM,
        digest=digest.hexdigest(),
        size_bytes=processed_bytes,
    )
