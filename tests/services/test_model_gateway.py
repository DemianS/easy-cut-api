from concurrent.futures import ThreadPoolExecutor
import os
from threading import Lock
from time import sleep
from uuid import uuid4

import pytest

from services.model_gateway import ADMIT_SCRIPT, ModelAdmissionError
from services.model_gateway import ModelExecutionGateway, RELEASE_SCRIPT

pytestmark = [
    pytest.mark.real_gateway,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="Требуется Redis"
    ),
]


def test_gateway_serializes_concurrent_clients():
    endpoint = f"http://test-{uuid4()}.local"
    state = {"active": 0, "maximum": 0}
    mutex = Lock()

    def operation():
        with ModelExecutionGateway(endpoint, 10).admit():
            with mutex:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            sleep(0.05)
            with mutex:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: operation(), range(3)))
    assert state["maximum"] == 1
    assert state["active"] == 0


def test_admission_bounds_backlog_and_checks_release_owner():
    gateway = ModelExecutionGateway(f"http://test-{uuid4()}.local", 10)
    client = gateway.client
    keys = [gateway.queue_key, gateway.expiry_key, gateway.lease_key]
    try:
        assert (
            client.eval(ADMIT_SCRIPT, 3, *keys, 100, "owner", 100, 200, 60000, 1) == 1
        )
        assert (
            client.eval(ADMIT_SCRIPT, 3, *keys, 100, "waiter", 101, 200, 60000, 1) == 0
        )
        assert (
            client.eval(ADMIT_SCRIPT, 3, *keys, 100, "extra", 102, 200, 60000, 1) == -1
        )
        assert client.eval(RELEASE_SCRIPT, 1, keys[2], "not-owner") == 0
        assert client.eval(RELEASE_SCRIPT, 1, keys[2], "owner") == 1
        assert client.eval(ADMIT_SCRIPT, 3, *keys, 201, "new", 201, 300, 60000, 1) == 1
    finally:
        client.delete(*keys)
        client.close()


def test_gateway_rejects_full_queue(monkeypatch):
    gateway = ModelExecutionGateway(f"http://test-{uuid4()}.local", 10)
    monkeypatch.setattr(gateway.client, "eval", lambda *args: -1)
    with pytest.raises(ModelAdmissionError, match="заполнена"):
        with gateway.admit():
            raise AssertionError("unreachable")
