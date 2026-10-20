"""Ограниченный admission control для общего локального model endpoint."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import os
from threading import Event, Thread
from time import monotonic, time
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

from services.logging_config import log_event, LogStatus

logger = logging.getLogger(__name__)
MODEL_REQUEST_CLASS = ContextVar("model_request_class", default="interactive")

ADMIT_SCRIPT = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
for _, token in ipairs(expired) do
    redis.call('ZREM', KEYS[1], token)
    redis.call('ZREM', KEYS[2], token)
end
if not redis.call('ZSCORE', KEYS[1], ARGV[2]) then
    if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[6]) then return -1 end
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
    redis.call('ZADD', KEYS[2], ARGV[4], ARGV[2])
end
local first = redis.call('ZRANGE', KEYS[1], 0, 0)[1]
if first == ARGV[2] and redis.call('SET', KEYS[3], ARGV[2],
                                'NX', 'PX', ARGV[5]) then
    redis.call('ZREM', KEYS[1], ARGV[2])
    redis.call('ZREM', KEYS[2], ARGV[2])
    return 1
end
return 0
"""
RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class ModelAdmissionError(RuntimeError):
    """Endpoint перегружен, недоступен или потерян lease."""


class ModelExecutionGateway:
    def __init__(self, endpoint: str, timeout_seconds: int):
        digest = hashlib.sha256(endpoint.encode()).hexdigest()[:20]
        prefix = f"easycut:model:{digest}"
        self.queue_key = f"{prefix}:queue"
        self.expiry_key = f"{prefix}:expiry"
        self.lease_key = f"{prefix}:lease"
        self.lease_ms = (timeout_seconds + 60) * 1000
        self.client = Redis(
            host=os.getenv("REDIS_HOST", "cache"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            socket_timeout=5,
            socket_connect_timeout=5,
        )

    @contextmanager
    def admit(self):
        token = str(uuid4())
        wait_seconds = max(1, int(os.getenv("MODEL_QUEUE_TIMEOUT_SECONDS", "120")))
        capacity = max(1, int(os.getenv("MODEL_QUEUE_MAX_WAITING", "32")))
        started = monotonic()
        now = time()
        score = now + (30 if MODEL_REQUEST_CLASS.get() == "background" else 0)
        stop = Event()
        lost = Event()
        thread = None
        acquired = False
        try:
            while monotonic() - started < wait_seconds:
                outcome = self.client.eval(
                    ADMIT_SCRIPT,
                    3,
                    self.queue_key,
                    self.expiry_key,
                    self.lease_key,
                    time(),
                    token,
                    score,
                    now + wait_seconds,
                    self.lease_ms,
                    capacity,
                )
                if outcome == -1:
                    raise ModelAdmissionError("Очередь модели заполнена")
                if outcome == 1:
                    acquired = True
                    break
                stop.wait(0.25)
            if not acquired:
                raise ModelAdmissionError("Истёк срок ожидания модели")

            def renew():
                while not stop.wait(10):
                    try:
                        result = self.client.eval(
                            RENEW_SCRIPT,
                            1,
                            self.lease_key,
                            token,
                            self.lease_ms,
                        )
                        if not result:
                            lost.set()
                            return
                    except RedisError:
                        lost.set()
                        return

            thread = Thread(target=renew, daemon=True)
            thread.start()
            log_event(
                logger,
                logging.INFO,
                "model_admission",
                LogStatus.STARTED,
                "Запрос допущен к локальной модели",
                stage="inference",
                elapsed_ms=round((monotonic() - started) * 1000),
            )
            yield
            if lost.is_set():
                raise ModelAdmissionError("Потеряно разрешение на выполнение модели")
        except RedisError as error:
            raise ModelAdmissionError("Admission control модели недоступен") from error
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=6)
            try:
                self.client.zrem(self.queue_key, token)
                self.client.zrem(self.expiry_key, token)
                if acquired:
                    self.client.eval(RELEASE_SCRIPT, 1, self.lease_key, token)
            except RedisError:
                pass
            self.client.close()
