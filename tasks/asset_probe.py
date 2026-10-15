"""Celery-задача технического анализа медиа через ffprobe."""

import asyncio
from uuid import UUID

from worker import celery_app


async def _run_asset_probe(job_id: UUID, celery_task_id: str | None) -> dict:
    from services.asset_probe import execute_asset_probe
    from sql.database import engine

    try:
        return await execute_asset_probe(job_id, celery_task_id)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="easy_cut.asset_probe")
def asset_probe(task, job_id: str) -> dict:
    """Проанализировать актив, указанный в существующей записи jobs."""
    return asyncio.run(
        _run_asset_probe(UUID(job_id), getattr(task.request, "id", None))
    )
