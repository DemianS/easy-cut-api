import json

import pytest

from services.vision_provider import VisionDescriptionProvider, VisionProviderError
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

    def fake_generate(self, **payload):
        captured_payload.update(payload)
        return {"response": json.dumps(content)}

    monkeypatch.setattr(
        "services.vision_provider.OllamaProvider.generate",
        fake_generate,
    )
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg")
    provider = VisionDescriptionProvider(
        VisionSettings("http://ollama:11434", "vision-model", 30)
    )

    description, raw_response, prompt_version = provider.describe(image_path)

    assert description.summary == content["summary"]
    assert captured_payload["model"] == "vision-model"
    assert captured_payload["images"]
    assert captured_payload["response_format"]["additionalProperties"] is False
    assert "response" in raw_response
    assert prompt_version.startswith("shot_description.ru:")


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
        provider.describe(image_path)
