from pathlib import Path

import pytest

from services.media_scanner import MediaRootOutsideAllowedPathError
from services.media_scanner import MediaRootUnavailableError
from services.media_scanner import discover_media_files, normalize_relative_path


def test_discover_media_files_classifies_supported_files(tmp_path: Path) -> None:
    nested_directory = tmp_path / "Каталог"
    nested_directory.mkdir()
    (nested_directory / "КЛИП.MP4").write_bytes(b"video")
    (nested_directory / "звук.wav").write_bytes(b"audio")
    (nested_directory / "кадр.PNG").write_bytes(b"image")
    (nested_directory / "заметка.txt").write_text("skip", encoding="utf-8")

    result = discover_media_files(tmp_path, tmp_path)

    assert {item.relative_path: item.media_kind for item in result.files} == {
        "Каталог/КЛИП.MP4": "video",
        "Каталог/звук.wav": "audio",
        "Каталог/кадр.PNG": "image",
    }
    assert {item.relative_path for item in result.files} == {
        "Каталог/КЛИП.MP4",
        "Каталог/звук.wav",
        "Каталог/кадр.PNG",
    }
    assert all(item.size_bytes > 0 for item in result.files)
    assert result.skipped_count == 0
    assert result.issues == ()


def test_normalized_path_is_case_insensitive_and_unicode_normalized() -> None:
    assert normalize_relative_path("ПАПКА/Ёлка.MOV") == normalize_relative_path(
        "папка/Ёлка.mov"
    )


def test_discover_media_files_rejects_root_outside_allowed_path(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    media_root = tmp_path / "outside"
    allowed_root.mkdir()
    media_root.mkdir()

    with pytest.raises(MediaRootOutsideAllowedPathError):
        discover_media_files(media_root, allowed_root)


def test_discover_media_files_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(MediaRootUnavailableError):
        discover_media_files(tmp_path / "missing", tmp_path)
