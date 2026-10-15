"""Celery-задача индексирования файлов медиатеки."""

import asyncio
from uuid import UUID

from worker import celery_app


async def _run_library_scan(job_id: UUID, celery_task_id: str | None) -> dict:
    """Запустить async SQL-слой и освободить его pool перед следующим event loop."""
    from services.library_scan import execute_library_scan
    from sql.database import engine

    try:
        return await execute_library_scan(job_id, celery_task_id)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="easy_cut.library_scan")
def library_scan(task, job_id: str) -> dict:
    """Просканировать корень, указанный в существующей записи jobs."""
    parsed_job_id = UUID(job_id)
    return asyncio.run(
        _run_library_scan(parsed_job_id, getattr(task.request, "id", None))
    )
