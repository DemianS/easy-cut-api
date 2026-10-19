from pathlib import Path
from types import SimpleNamespace

from services.shot_detector import detect_shots, extract_representative_frame
from services.shot_detector import FrameSamplingSettings, ShotBoundary
from services.shot_detector import select_frame_samples


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


def test_frame_sampling_uses_one_frame_for_short_shot() -> None:
    samples = select_frame_samples(
        ShotBoundary(0, 1000, 2000),
        FrameSamplingSettings(),
    )

    assert [(item.timestamp_ms, item.role) for item in samples] == [(1500, "single")]


def test_frame_sampling_uses_start_middle_end_for_regular_shot() -> None:
    settings = FrameSamplingSettings()
    samples = select_frame_samples(
        ShotBoundary(0, 1000, 4000),
        settings,
    )

    assert [item.role for item in samples] == ["start", "middle", "end"]
    assert [item.sequence for item in samples] == [0, 1, 2]
    assert samples[0].timestamp_ms > 1000
    assert samples[-1].timestamp_ms < 4000
    assert settings.as_dict()["max_frames_per_shot"] == 8


def test_frame_sampling_caps_long_shot_and_respects_minimum_step() -> None:
    settings = FrameSamplingSettings(
        sample_interval_ms=1000,
        min_step_ms=2000,
        max_frames_per_shot=5,
    )

    samples = select_frame_samples(ShotBoundary(0, 0, 20000), settings)

    assert len(samples) == 5
    assert all(
        current.timestamp_ms - previous.timestamp_ms >= settings.min_step_ms
        for previous, current in zip(samples, samples[1:])
    )


def test_extract_representative_frame_reuses_non_empty_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "frames" / "500_middle.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing-jpeg")
    monkeypatch.setattr(
        "services.shot_detector.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    result = extract_representative_frame(
        tmp_path / "source.mp4",
        destination,
        timestamp_ms=500,
        source_width=640,
        source_height=360,
        artifact_path="frames/asset/shot/500_middle.jpg",
    )

    assert result.reused is True
    assert destination.read_bytes() == b"existing-jpeg"
