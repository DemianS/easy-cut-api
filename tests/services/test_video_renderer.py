from pathlib import Path
import subprocess
from time import monotonic
from types import SimpleNamespace

import pytest

from services.montage_planner import MontageError
from services.video_renderer import _check_sources, _run, RenderSource


def test_renderer_rejects_changed_source(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    stat = path.stat()
    source = RenderSource(path, stat.st_size, stat.st_mtime_ns, False)
    path.write_bytes(b"changed-source")
    with pytest.raises(MontageError, match="изменился"):
        _check_sources({"source": source})


def test_renderer_reports_nonzero_exit_without_leaking_stderr(tmp_path, monkeypatch):
    process = SimpleNamespace(
        communicate=lambda **kwargs: (b"", b"private/path and private content"),
        returncode=1,
        poll=lambda: 1,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(MontageError, match="FFmpeg/ffprobe") as caught:
        _run(["ffmpeg"], tmp_path, monotonic() + 10)
    assert "private" not in str(caught.value)


def test_renderer_kills_child_after_deadline(tmp_path, monkeypatch):
    state = {"killed": False}

    def kill():
        state["killed"] = True

    process = SimpleNamespace(
        poll=lambda: None,
        kill=kill,
        communicate=lambda **kwargs: (b"", b""),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(MontageError, match="время"):
        _run(["ffmpeg"], Path(tmp_path), monotonic() - 1)
    assert state["killed"]
