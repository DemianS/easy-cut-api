"""Точка входа Celery worker."""

import os
from urllib.parse import quote

from celery import Celery

DEFAULT_REDIS_HOST = "cache"
DEFAULT_REDIS_PORT = 6379
DEFAULT_REDIS_DATABASE = 0
DEFAULT_WORKER_CONCURRENCY = 1


def build_broker_url() -> str:
    """Собрать Redis URL из переменных окружения без раскрытия пароля."""
    host = os.getenv("REDIS_HOST", DEFAULT_REDIS_HOST)
    port = int(os.getenv("REDIS_PORT", str(DEFAULT_REDIS_PORT)))
    database = int(os.getenv("REDIS_DB", str(DEFAULT_REDIS_DATABASE)))
    password = os.getenv("REDIS_PASSWORD")
    credentials = f":{quote(password, safe='')}@" if password else ""

    return f"redis://{credentials}{host}:{port}/{database}"


def create_celery_app() -> Celery:
    """Создать Celery-приложение для фоновых задач Easy Cut."""
    app = Celery(
        "easy_cut_worker",
        broker=build_broker_url(),
        include=(
            "tasks.asset_probe",
            "tasks.diagnostics",
            "tasks.library_scan",
        ),
    )
    app.conf.update(
        accept_content=("json",),
        broker_connection_retry_on_startup=True,
        enable_utc=True,
        task_acks_late=True,
        task_ignore_result=True,
        task_reject_on_worker_lost=True,
        task_serializer="json",
        task_track_started=True,
        timezone="UTC",
        worker_concurrency=int(
            os.getenv("WORKER_CONCURRENCY", str(DEFAULT_WORKER_CONCURRENCY))
        ),
        worker_prefetch_multiplier=1,
    )
    return app


celery_app = create_celery_app()
