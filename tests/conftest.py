"""Изоляция unit-тестов HTTP-provider от реального Redis admission control."""

from contextlib import nullcontext

import pytest


@pytest.fixture(autouse=True)
def isolate_model_admission(request, monkeypatch):
    if "real_gateway" in request.keywords:
        return
    monkeypatch.setattr(
        "services.model_gateway.ModelExecutionGateway.admit",
        lambda self: nullcontext(),
    )
