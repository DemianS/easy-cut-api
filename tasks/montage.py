"""Исполнение одной короткой стадии сохраняемого монтажного сценария."""

import asyncio
from uuid import UUID

from tasks.base import DatabaseJobTask
from worker import celery_app


async def _execute(job_id, task_id):
    from services.montage_workflow import execute_montage_stage
    from services.montage_planner import MontageError
    from sql.database import engine

    try:
        result = await execute_montage_stage(UUID(job_id), task_id)
        if result.get("failed"):
            raise MontageError("Стадия монтажа завершилась ошибкой; подробности в job")
        return result
    finally:
        await engine.dispose()


@celery_app.task(bind=True, base=DatabaseJobTask, name="easy_cut.montage_stage")
def montage_stage(task, job_id: str):
    return asyncio.run(_execute(job_id, getattr(task.request, "id", None)))
