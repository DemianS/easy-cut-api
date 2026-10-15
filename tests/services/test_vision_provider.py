import json

import pytest

from services.vision_provider import OllamaVisionProvider, VisionProviderError
from services.vision_provider import VisionProviderNotConfiguredError
from services.vision_provider import VisionSettings, get_vision_settings


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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


def test_ollama_provider_validates_structured_response(tmp_path, monkeypatch) -> None:
    captured_payload = {}
    content = {
        "summary": "Человек собирает металлическую деталь.",
        "visible_objects": ["человек", "деталь"],
        "action": "собирает деталь",
        "setting": "мастерская",
        "shot_type": "средний план",
        "camera_motion": None,
        "visible_text": [],
        "uncertainty": None,
    }

    def fake_urlopen(request, timeout):
        captured_payload.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse({"message": {"content": json.dumps(content)}})

    monkeypatch.setattr("services.vision_provider.urlopen", fake_urlopen)
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    provider = OllamaVisionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    description, raw_response = provider.describe(image_path)

    assert description.summary == content["summary"]
    assert captured_payload["model"] == "vision-model"
    assert captured_payload["stream"] is False
    assert captured_payload["messages"][0]["images"]
    assert captured_payload["format"]["additionalProperties"] is False
    assert "message" in raw_response


def test_ollama_provider_rejects_invalid_model_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "services.vision_provider.urlopen",
        lambda *args, **kwargs: FakeResponse(
            {"message": {"content": '{"invented": true}'}}
        ),
    )
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    provider = OllamaVisionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    with pytest.raises(VisionProviderError):
        provider.describe(image_path)
