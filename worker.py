"""Точка входа Celery worker."""

import os
import logging
from time import monotonic
from urllib.parse import quote

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun, task_retry

from services.logging_config import configure_logging, log_event, LogStatus
from services.model_gateway import MODEL_REQUEST_CLASS

configure_logging()
logger = logging.getLogger(__name__)
_TASK_STARTED_AT: dict[str, float] = {}

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
            "tasks.asset_description",
            "tasks.asset_embedding",
            "tasks.asset_probe",
            "tasks.diagnostics",
            "tasks.library_scan",
            "tasks.job_dispatcher",
            "tasks.shot_detection",
            "tasks.montage",
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
        worker_hijack_root_logger=False,
        task_routes={
            "easy_cut.reconcile_jobs": {"queue": "control"},
            "easy_cut.asset_description": {"queue": "model_background"},
            "easy_cut.asset_embedding": {"queue": "model_background"},
        },
    )
    app.conf.beat_schedule = {
        "reconcile-background-jobs": {
            "task": "easy_cut.reconcile_jobs",
            "schedule": float(os.getenv("JOB_DISPATCH_INTERVAL_SECONDS", "15")),
        }
    }
    return app


celery_app = create_celery_app()


def _task_context(sender, task_id: str | None, args) -> dict:
    context = {
        "task_id": task_id,
        "task_name": getattr(sender, "name", None),
    }
    if args:
        context["job_id"] = str(args[0])
    return context


@task_prerun.connect
def log_task_started(sender=None, task_id=None, args=None, **kwargs) -> None:
    """Записать начало любой Celery-задачи."""
    if task_id:
        _TASK_STARTED_AT[task_id] = monotonic()
    MODEL_REQUEST_CLASS.set(
        "background"
        if getattr(sender, "name", "")
        in {
            "easy_cut.asset_description",
            "easy_cut.asset_embedding",
        }
        else "interactive"
    )
    log_event(
        logger,
        logging.INFO,
        "celery_task",
        LogStatus.STARTED,
        "Celery-задача запущена",
        stage="execution",
        **_task_context(sender, task_id, args),
    )


@task_postrun.connect
def log_task_finished(
    sender=None,
    task_id=None,
    args=None,
    state=None,
    **kwargs,
) -> None:
    """Записать итог и длительность любой Celery-задачи."""
    started_at = _TASK_STARTED_AT.pop(task_id, None)
    elapsed_ms = (
        round((monotonic() - started_at) * 1000) if started_at is not None else None
    )
    lifecycle_status = {
        "SUCCESS": LogStatus.SUCCEEDED,
        "RETRY": LogStatus.RETRYING,
    }.get(state, LogStatus.FAILED)
    log_event(
        logger,
        logging.INFO,
        "celery_task",
        lifecycle_status,
        "Celery-задача завершена",
        stage="execution",
        celery_state=state,
        elapsed_ms=elapsed_ms,
        **_task_context(sender, task_id, args),
    )


@task_failure.connect
def log_task_failure(
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    **kwargs,
) -> None:
    """Записать классифицированную ошибку Celery-задачи."""
    log_event(
        logger,
        logging.ERROR,
        "celery_task",
        LogStatus.FAILED,
        "Celery-задача завершилась ошибкой",
        stage="execution",
        error_type=type(exception).__name__,
        **_task_context(sender, task_id, args),
    )


@task_retry.connect
def log_task_retry(sender=None, request=None, reason=None, **kwargs) -> None:
    """Записать повторную доставку задачи без сериализации аргументов."""
    log_event(
        logger,
        logging.WARNING,
        "celery_task",
        LogStatus.RETRYING,
        "Celery-задача будет запущена повторно",
        stage="retry",
        task_id=getattr(request, "id", None),
        task_name=getattr(sender, "name", None),
        error_type=type(reason).__name__,
    )
