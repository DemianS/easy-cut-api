"""Celery-задача локального описания планов."""

import asyncio
from uuid import UUID

from tasks.base import DatabaseJobTask
from worker import celery_app


async def _run_asset_description(job_id: UUID, celery_task_id: str | None) -> dict:
    from services.asset_description import execute_asset_description
    from sql.database import engine

    try:
        return await execute_asset_description(job_id, celery_task_id)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, base=DatabaseJobTask, name="easy_cut.asset_description")
def asset_description(task, job_id: str) -> dict:
    """Описать планы актива через явно настроенную локальную vision-модель."""
    return asyncio.run(
        _run_asset_description(UUID(job_id), getattr(task.request, "id", None))
    )
