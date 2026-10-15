from pathlib import Path
from types import SimpleNamespace

import pytest

from services.media_probe import FfprobeExecutionError
from services.media_probe import InvalidProbeOutputError
from services.media_probe import parse_ffprobe_output, probe_media_file


def test_parse_ffprobe_output_extracts_video_and_audio_metadata() -> None:
    result = parse_ffprobe_output(
        {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "12.3456",
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }
    )

    assert result.duration_ms == 12346
    assert (result.width, result.height) == (1920, 1080)
    assert (result.frame_rate_num, result.frame_rate_den) == (30000, 1001)
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.sample_rate_hz == 48000
    assert result.audio_channels == 2


def test_parse_ffprobe_output_rejects_incomplete_payload() -> None:
    with pytest.raises(InvalidProbeOutputError):
        parse_ffprobe_output({"streams": []})


def test_probe_media_file_uses_argument_list_without_shell(monkeypatch) -> None:
    captured_call = {}

    def fake_run(command, **kwargs):
        captured_call["command"] = command
        captured_call["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout='{"format": {"format_name": "wav"}, "streams": []}',
            stderr="",
        )

    monkeypatch.setattr("services.media_probe.subprocess.run", fake_run)

    result = probe_media_file(Path("media.wav"))

    assert result.format_name == "wav"
    assert captured_call["command"][-1] == "media.wav"
    assert "shell" not in captured_call["kwargs"]
    assert captured_call["kwargs"]["timeout"] == 120


def test_probe_error_hides_absolute_media_path(monkeypatch, tmp_path: Path) -> None:
    media_path = tmp_path / "private-name.mp4"

    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"{media_path}: invalid data",
        )

    monkeypatch.setattr("services.media_probe.subprocess.run", fake_run)

    with pytest.raises(FfprobeExecutionError) as error_info:
        probe_media_file(media_path)

    assert str(media_path) not in str(error_info.value)
    assert "<media>" in str(error_info.value)
