"""Периодическая Celery-задача продвижения и восстановления jobs."""

import asyncio
import logging

from services.logging_config import log_event, LogStatus
from worker import celery_app

logger = logging.getLogger(__name__)


async def _reconcile_jobs() -> list:
    from services.job_dispatcher import reconcile_jobs
    from sql.database import engine

    try:
        return await reconcile_jobs()
    finally:
        await engine.dispose()


async def _record_dispatch_failure(job_id) -> None:
    from services.job_dispatcher import record_dispatch_failure
    from sql.database import engine

    try:
        await record_dispatch_failure(job_id)
    finally:
        await engine.dispose()


@celery_app.task(name="easy_cut.reconcile_jobs")
def reconcile_background_jobs() -> dict:
    """Выполнить проход диспетчера и отправить подготовленные задачи."""
    log_event(
        logger,
        logging.DEBUG,
        "job_dispatch",
        LogStatus.STARTED,
        "Отправка подготовленных задач начата",
        stage="broker",
    )
    dispatches = asyncio.run(_reconcile_jobs())
    dispatched = 0
    failed = 0
    for request in dispatches:
        try:
            celery_app.send_task(
                request.task_name,
                args=[str(request.job_id)],
                task_id=request.task_id,
                **({"queue": request.queue} if request.queue else {}),
            )
            dispatched += 1
        except Exception as error:
            failed += 1
            log_event(
                logger,
                logging.WARNING,
                "job_dispatch",
                LogStatus.PROGRESS,
                "Не удалось отправить восстановленную задачу в broker",
                stage="broker",
                job_id=str(request.job_id),
                job_type=request.task_name,
                error_type=type(error).__name__,
            )
            asyncio.run(_record_dispatch_failure(request.job_id))
    result = {"dispatched": dispatched, "failed": failed}
    final_status = LogStatus.FAILED if failed else LogStatus.SUCCEEDED
    log_level = (
        logging.WARNING if failed else (logging.INFO if dispatched else logging.DEBUG)
    )
    log_event(
        logger,
        log_level,
        "job_dispatch",
        final_status,
        "Отправка подготовленных задач завершена",
        stage="broker",
        dispatches=dispatched,
        failed=failed,
    )
    return result
