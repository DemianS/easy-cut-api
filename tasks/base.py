"""Общие защитные механизмы Celery-задач, связанных с jobs."""

import asyncio
from datetime import UTC, datetime
import logging
from uuid import UUID

from celery import Task

from services.logging_config import log_event, LogStatus

logger = logging.getLogger(__name__)
UNHANDLED_ERROR_CODE = "unhandled_task_error"
UNHANDLED_ERROR_MESSAGE = "Фоновая задача завершилась необработанной ошибкой"


async def mark_unhandled_job_failure(
    job_id: UUID,
    celery_task_id: str | None,
) -> bool:
    """Атомарно завершить потерянную job, не затирая предметную ошибку."""
    from sqlalchemy import select

    from sql.database import session_scope
    from sql.enums import JobStatus
    from sql.models import Job

    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None or job.status in {
            JobStatus.SUCCEEDED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }:
            return False

        if (
            celery_task_id
            and job.celery_task_id
            and job.celery_task_id != celery_task_id
        ):
            return False

        finished_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.celery_task_id = celery_task_id or job.celery_task_id
        job.error_code = UNHANDLED_ERROR_CODE
        job.error_message = UNHANDLED_ERROR_MESSAGE
        job.heartbeat_at = finished_at
        job.finished_at = finished_at
        return True


async def _mark_failure_and_dispose(
    job_id: UUID,
    celery_task_id: str | None,
) -> bool:
    from sql.database import engine

    try:
        return await mark_unhandled_job_failure(job_id, celery_task_id)
    finally:
        await engine.dispose()


class DatabaseJobTask(Task):
    """Гарантировать терминальное состояние job при необработанной ошибке."""

    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        raw_job_id = args[0] if args else kwargs.get("job_id")
        try:
            job_id = UUID(str(raw_job_id))
        except (TypeError, ValueError, AttributeError):
            log_event(
                logger,
                logging.ERROR,
                "job_failure_guard",
                LogStatus.FAILED,
                "Не удалось связать ошибку Celery-задачи с job",
                stage="identify",
                task_id=task_id,
                task_name=self.name,
                error_type=type(exc).__name__,
                error_code="invalid_job_id",
            )
            return super().on_failure(exc, task_id, args, kwargs, einfo)

        try:
            state_changed = asyncio.run(_mark_failure_and_dispose(job_id, task_id))
        except Exception as persistence_error:
            log_event(
                logger,
                logging.ERROR,
                "job_failure_guard",
                LogStatus.FAILED,
                "Не удалось сохранить аварийное состояние фоновой задачи",
                stage="persist",
                exc_info=True,
                task_id=task_id,
                task_name=self.name,
                job_id=str(job_id),
                error_type=type(persistence_error).__name__,
                error_code="failure_persistence_failed",
            )
        else:
            log_event(
                logger,
                logging.WARNING,
                "job_failure_guard",
                LogStatus.SUCCEEDED,
                "Аварийное состояние фоновой задачи обработано",
                stage="persist",
                task_id=task_id,
                task_name=self.name,
                job_id=str(job_id),
                error_type=type(exc).__name__,
                error_code=UNHANDLED_ERROR_CODE,
                items_updated=int(state_changed),
            )

        return super().on_failure(exc, task_id, args, kwargs, einfo)
