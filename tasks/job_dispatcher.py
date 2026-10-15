"""Периодическая Celery-задача продвижения и восстановления jobs."""

import asyncio
import logging

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
    dispatches = asyncio.run(_reconcile_jobs())
    dispatched = 0
    failed = 0
    for request in dispatches:
        try:
            celery_app.send_task(
                request.task_name,
                args=[str(request.job_id)],
                task_id=request.task_id,
            )
            dispatched += 1
        except Exception as error:
            failed += 1
            logger.warning(
                "Не удалось отправить восстановленную задачу в broker",
                extra={
                    "job_id": str(request.job_id),
                    "job_type": request.task_name,
                    "error_type": type(error).__name__,
                },
            )
            asyncio.run(_record_dispatch_failure(request.job_id))
    return {"dispatched": dispatched, "failed": failed}
