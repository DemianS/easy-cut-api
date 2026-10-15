"""Безопасная JSON-конфигурация логов API и worker."""

from datetime import UTC, datetime
import json
import logging
import os

EXTRA_FIELDS = (
    "operation",
    "model",
    "status",
    "attempt",
    "completed",
    "total",
    "elapsed_ms",
    "error_type",
    "job_id",
    "job_type",
    "shot_id",
    "embedding_id",
    "prompt_version",
    "recovery_attempt",
)


class JsonFormatter(logging.Formatter):
    """Сериализовать только известные безопасные поля записи."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Настроить корневой logger без вывода секретных payload-ов."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
