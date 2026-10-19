import json
from uuid import uuid4

import pytest

from services.vision_provider import VisionDescriptionProvider, VisionFrame
from services.vision_provider import VisionProviderError
from services.vision_provider import VisionProviderNotConfiguredError
from services.vision_provider import VisionSettings, get_vision_settings


def test_vision_settings_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VISION_PROVIDER", raising=False)
    with pytest.raises(VisionProviderNotConfiguredError):
        get_vision_settings()


def test_vision_settings_reject_remote_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("VISION_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "vision-model")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://example.com")
    with pytest.raises(VisionProviderNotConfiguredError):
        get_vision_settings()


def _description_payload(frames: list[VisionFrame]) -> dict:
    observations = [
        {
            "frame_id": str(frame.id),
            "timestamp_ms": frame.timestamp_ms,
            "summary": "Человек работает с деталью.",
            "visible_objects": ["человек", "деталь"],
            "action": "работает с деталью",
            "setting": "мастерская",
            "composition": "средний план",
            "visible_text": [],
            "confidence": 0.9,
        }
        for frame in frames
    ]
    transitions = [
        {
            "from_frame_id": str(previous.id),
            "to_frame_id": str(current.id),
            "start_ms": previous.timestamp_ms,
            "end_ms": current.timestamp_ms,
            "changes": ["рука сместилась"],
            "object_motion": None,
            "confidence": 0.7,
        }
        for previous, current in zip(frames, frames[1:])
    ]
    return {
        "summary": "Человек собирает металлическую деталь.",
        "visible_objects": ["человек", "деталь"],
        "stable_objects": ["человек", "деталь"],
        "action": "собирает деталь",
        "setting": "мастерская",
        "shot_type": "средний план",
        "camera_motion": None,
        "object_motion": None,
        "visible_text": [],
        "uncertainty": None,
        "frame_observations": observations,
        "frame_transitions": transitions,
        "temporal_events": [
            {
                "start_ms": frames[0].timestamp_ms,
                "end_ms": frames[-1].timestamp_ms + 1,
                "description": "Человек работает с деталью",
                "evidence_frame_ids": [str(frame.id) for frame in frames],
                "confidence": 0.8,
            }
        ],
        "field_evidence": [
            {
                "field": "summary",
                "frame_ids": [str(frame.id) for frame in frames],
                "confidence": 0.9,
            }
        ],
    }


def test_ollama_provider_validates_structured_response(tmp_path, monkeypatch) -> None:
    captured_payload = {}
    frames = []
    for index, timestamp_ms in enumerate((100, 500, 900)):
        image_path = tmp_path / f"frame-{index}.jpg"
        image_path.write_bytes(b"jpeg")
        frames.append(VisionFrame(uuid4(), timestamp_ms, image_path))
    content = _description_payload(frames)

    def fake_generate(self, **payload):
        captured_payload.update(payload)
        return {"response": json.dumps(content)}

    monkeypatch.setattr(
        "services.vision_provider.OllamaProvider.generate",
        fake_generate,
    )
    provider = VisionDescriptionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    description, raw_response, prompt_version = provider.describe(
        frames,
        shot_start_ms=0,
        shot_end_ms=1000,
    )

    assert description.summary == content["summary"]
    assert len(description.frame_observations) == 3
    assert captured_payload["model"] == "vision-model"
    assert len(captured_payload["images"]) == 3
    assert str(frames[0].id) in captured_payload["prompt"]
    assert captured_payload["response_format"]["additionalProperties"] is False
    assert "response" in raw_response
    assert prompt_version.startswith("shot_description.ru:")


def test_ollama_provider_rejects_unknown_evidence_frame(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    frames = [VisionFrame(uuid4(), 500, image_path)]
    content = _description_payload(frames)
    content["temporal_events"][0]["evidence_frame_ids"] = [str(uuid4())]
    monkeypatch.setattr(
        "services.vision_provider.OllamaProvider.generate",
        lambda *args, **kwargs: {"response": json.dumps(content)},
    )
    provider = VisionDescriptionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    with pytest.raises(VisionProviderError, match="неизвестный кадр"):
        provider.describe(frames, shot_start_ms=0, shot_end_ms=1000)


def test_ollama_provider_rejects_unmeasured_motion(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    frames = [VisionFrame(uuid4(), 500, image_path)]
    content = _description_payload(frames)
    content["camera_motion"] = "панорамирование"
    monkeypatch.setattr(
        "services.vision_provider.OllamaProvider.generate",
        lambda *args, **kwargs: {"response": json.dumps(content)},
    )
    provider = VisionDescriptionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    with pytest.raises(VisionProviderError, match="алгоритмического анализа"):
        provider.describe(frames, shot_start_ms=0, shot_end_ms=1000)


def test_ollama_provider_rejects_invalid_model_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "services.vision_provider.OllamaProvider.generate",
        lambda *args, **kwargs: {"response": '{"invented": true}'},
    )
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    provider = VisionDescriptionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    with pytest.raises(VisionProviderError):
        provider.describe(
            [VisionFrame(uuid4(), 500, image_path)],
            shot_start_ms=0,
            shot_end_ms=1000,
        )
