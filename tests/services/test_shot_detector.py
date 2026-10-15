from pathlib import Path
from types import SimpleNamespace

from services.shot_detector import detect_shots, extract_representative_frame


class FakeTimecode:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def get_seconds(self) -> float:
        return self.seconds


def test_detect_shots_converts_boundaries_to_milliseconds(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.shot_detector.detect",
        lambda *args, **kwargs: [
            (FakeTimecode(0), FakeTimecode(1.234)),
            (FakeTimecode(1.234), FakeTimecode(3.5)),
        ],
    )

    shots = detect_shots(Path("video.mp4"))

    assert [(shot.sequence, shot.start_ms, shot.end_ms) for shot in shots] == [
        (0, 0, 1234),
        (1, 1234, 3500),
    ]
    assert shots[1].middle_ms == 2367


def test_extract_representative_frame_uses_argument_list_and_atomic_file(
    tmp_path,
    monkeypatch,
) -> None:
    captured_command = []

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        Path(command[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("services.shot_detector.subprocess.run", fake_run)
    destination = tmp_path / "frames" / "middle.jpg"

    result = extract_representative_frame(
        tmp_path / "source.mp4",
        destination,
        timestamp_ms=1500,
        source_width=1920,
        source_height=1080,
        artifact_path="frames/asset/shot/middle.jpg",
    )

    assert captured_command[0] == "ffmpeg"
    assert captured_command[captured_command.index("-ss") + 1] == "1.500"
    assert destination.read_bytes() == b"jpeg"
    assert result.width == 1280
    assert result.height == 720
