import json

import pytest
import requests

from services.ollama_provider import OllamaProvider, OllamaProviderError


class FakeResponse:
    def __init__(self, payload=None, lines=None, status_code=200) -> None:
        self.payload = payload
        self.lines = lines or []
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("request failed")

    def json(self):
        return self.payload

    def iter_lines(self):
        return iter(self.lines)


def test_wait_for_ollama_retries_without_sleeping(monkeypatch) -> None:
    responses = [requests.ConnectionError(), FakeResponse({"models": []})]

    def fake_get(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    sleeps = []
    monkeypatch.setattr("services.ollama_provider.requests.get", fake_get)
    provider = OllamaProvider("http://ollama:11434", 30)

    assert provider.wait_for_ollama(2, 0.25, sleep=sleeps.append) is True
    assert sleeps == [0.25]


def test_pull_model_reads_streaming_progress(monkeypatch) -> None:
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(
            lines=[
                json.dumps({"status": "pulling manifest"}).encode(),
                json.dumps({"status": "success"}).encode(),
            ]
        )

    monkeypatch.setattr("services.ollama_provider.requests.post", fake_post)
    provider = OllamaProvider("http://ollama:11434", 30)

    provider.pull_model("vision-model")

    assert captured["stream"] is True
    assert captured["json"] == {"name": "vision-model"}


def test_generate_uses_complete_non_streaming_response(monkeypatch) -> None:
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse({"response": '{"summary":"кадр"}'})

    monkeypatch.setattr("services.ollama_provider.requests.post", fake_post)
    provider = OllamaProvider("http://ollama:11434", 30)

    result = provider.generate(
        model="vision-model",
        prompt="prompt",
        images=["encoded-image"],
        response_format={"type": "object"},
    )

    assert result["response"] == '{"summary":"кадр"}'
    assert captured["json"]["stream"] is False


def test_embed_rejects_malformed_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.ollama_provider.requests.post",
        lambda *args, **kwargs: FakeResponse({"embeddings": []}),
    )
    provider = OllamaProvider("http://ollama:11434", 30)

    with pytest.raises(OllamaProviderError):
        provider.embed(model="embedding-model", text="query", dimensions=3)
