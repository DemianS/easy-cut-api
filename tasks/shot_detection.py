"""Celery-задача детекции планов и кадров."""

import asyncio
from uuid import UUID

from worker import celery_app


async def _run_shot_detection(job_id: UUID, celery_task_id: str | None) -> dict:
    from services.shot_detection import execute_shot_detection
    from sql.database import engine

    try:
        return await execute_shot_detection(job_id, celery_task_id)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="easy_cut.shot_detection")
def shot_detection(task, job_id: str) -> dict:
    """Определить планы и сохранить репрезентативные кадры видео."""
    return asyncio.run(
        _run_shot_detection(UUID(job_id), getattr(task.request, "id", None))
    )
