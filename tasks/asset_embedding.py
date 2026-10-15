"""Celery-задача построения embeddings описаний планов."""

import asyncio
from uuid import UUID

from worker import celery_app


async def _run_asset_embedding(job_id: UUID, celery_task_id: str | None) -> dict:
    from services.asset_embedding import execute_asset_embedding
    from sql.database import engine

    try:
        return await execute_asset_embedding(job_id, celery_task_id)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="easy_cut.asset_embedding")
def asset_embedding(task, job_id: str) -> dict:
    """Построить embeddings успешно описанных планов актива."""
    return asyncio.run(
        _run_asset_embedding(UUID(job_id), getattr(task.request, "id", None))
    )
