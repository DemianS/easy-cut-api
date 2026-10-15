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


async def _record_probe_dispatch(
    scan_job_id: UUID,
    dispatched_job_ids: list[str],
    failed_job_ids: list[str],
) -> None:
    from services.job_dispatcher import record_dispatch_failure
    from services.library_scan import record_probe_dispatch_result
    from sql.database import engine

    try:
        for failed_job_id in failed_job_ids:
            await record_dispatch_failure(UUID(failed_job_id))
        await record_probe_dispatch_result(
            scan_job_id,
            dispatched_job_ids,
            failed_job_ids,
        )
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="easy_cut.library_scan")
def library_scan(task, job_id: str) -> dict:
    """Просканировать корень, указанный в существующей записи jobs."""
    parsed_job_id = UUID(job_id)
    result = asyncio.run(
        _run_library_scan(parsed_job_id, getattr(task.request, "id", None))
    )
    from tasks.asset_probe import asset_probe

    dispatched_job_ids: list[str] = []
    failed_job_ids: list[str] = []
    for probe_job_id in result.get("probe_job_ids", []):
        try:
            asset_probe.apply_async(
                args=[probe_job_id],
                task_id=probe_job_id,
            )
            dispatched_job_ids.append(probe_job_id)
        except Exception:
            failed_job_ids.append(probe_job_id)

    asyncio.run(
        _record_probe_dispatch(
            parsed_job_id,
            dispatched_job_ids,
            failed_job_ids,
        )
    )
    result["probe_jobs_dispatched"] = len(dispatched_job_ids)
    result["probe_dispatch_failed_ids"] = failed_job_ids
    return result
