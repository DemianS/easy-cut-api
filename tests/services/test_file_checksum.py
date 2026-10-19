import hashlib

import pytest

from services.file_checksum import calculate_file_checksum, FileChecksumError


def test_calculate_file_checksum_reads_file_in_chunks(tmp_path) -> None:
    content = b"chunked-content"
    media_path = tmp_path / "clip.bin"
    media_path.write_bytes(content)
    progress: list[int] = []

    result = calculate_file_checksum(
        media_path,
        chunk_size_bytes=4,
        progress_callback=progress.append,
    )

    assert result.algorithm == "sha256"
    assert result.digest == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert progress == [4, 8, 12, len(content)]


def test_calculate_file_checksum_hides_unavailable_path(tmp_path) -> None:
    missing_path = tmp_path / "private-video.mp4"

    with pytest.raises(FileChecksumError) as error_info:
        calculate_file_checksum(missing_path)

    assert str(missing_path) not in str(error_info.value)
