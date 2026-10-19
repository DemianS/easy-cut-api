"""Безопасная JSON-конфигурация логов API и worker."""

from contextvars import ContextVar, Token
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

EXTRA_FIELDS = (
    "component",
    "request_id",
    "http_method",
    "http_path",
    "http_status",
    "task_id",
    "task_name",
    "celery_state",
    "provider_status",
    "model",
    "attempt",
    "completed",
    "total",
    "elapsed_ms",
    "error_type",
    "error_code",
    "job_id",
    "job_type",
    "asset_id",
    "media_root_id",
    "user_id",
    "shot_id",
    "embedding_id",
    "prompt_version",
    "recovery_attempt",
    "assets_created",
    "assets_updated",
    "assets_unchanged",
    "assets_missing",
    "items_skipped",
    "items_created",
    "items_updated",
    "bytes_processed",
    "checksum_algorithm",
    "failed",
    "shots",
    "frames",
    "dispatches",
    "return_code",
    "timeout_seconds",
)

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


class LogStatus(StrEnum):
    """Допустимые состояния жизненного цикла операции."""

    STARTED = "started"
    PROGRESS = "progress"
    DISPATCHED = "dispatched"
    REUSED = "reused"
    RETRYING = "retrying"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def set_request_id(request_id: str) -> Token:
    """Привязать request_id ко всем логам текущего async-контекста."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    """Восстановить предыдущий контекст после завершения запроса."""
    _REQUEST_ID.reset(token)


def log_event(
    logger: logging.Logger,
    level: int,
    operation: str,
    status: LogStatus,
    message: str,
    *,
    stage: str | None = None,
    exc_info: bool = False,
    **context,
) -> None:
    """Записать событие по общему контракту `operation.status`."""
    extra = {
        "operation": operation,
        "event": f"{operation}.{status.value}",
        "status": status.value,
        **context,
    }
    if stage is not None:
        extra["stage"] = stage
    logger.log(level, message, extra=extra, exc_info=exc_info)


class JsonFormatter(logging.Formatter):
    """Сериализовать только известные безопасные поля записи."""

    def format(self, record: logging.LogRecord) -> str:
        operation = getattr(record, "operation", "runtime")
        status = getattr(record, "status", None)
        if status is None:
            status = (
                LogStatus.FAILED.value
                if record.levelno >= logging.ERROR
                else LogStatus.PROGRESS.value
            )
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "component": getattr(record, "component", None)
            or os.getenv("LOG_COMPONENT", "application"),
            "process_id": record.process,
            "operation": operation,
            "event": getattr(record, "event", f"{operation}.{status}"),
            "stage": getattr(record, "stage", "runtime"),
            "status": status,
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if "request_id" not in payload and _REQUEST_ID.get() is not None:
            payload["request_id"] = _REQUEST_ID.get()
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Настроить stdout и опциональный ротируемый JSONL-файл."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]

    log_file = os.getenv("LOG_FILE")
    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=int(os.getenv("LOG_MAX_BYTES", str(DEFAULT_LOG_MAX_BYTES))),
                backupCount=int(
                    os.getenv("LOG_BACKUP_COUNT", str(DEFAULT_LOG_BACKUP_COUNT))
                ),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except (OSError, ValueError):
            stream_handler.handle(
                logging.LogRecord(
                    name=__name__,
                    level=logging.ERROR,
                    pathname=__file__,
                    lineno=0,
                    msg="Не удалось настроить файловое логирование",
                    args=(),
                    exc_info=None,
                )
            )

    logging.basicConfig(level=level, handlers=handlers, force=True)
