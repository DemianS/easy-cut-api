import asyncio
import os
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_task_guard_marks_job_failed_without_overwriting_error() -> None:
    asyncio.run(_run_task_guard_scenario())


async def _run_task_guard_scenario() -> None:
    from sqlalchemy import delete

    from sql.database import engine, session_scope
    from sql.enums import JobStatus
    from sql.models import Job
    from tasks.base import mark_unhandled_job_failure, UNHANDLED_ERROR_MESSAGE

    job_id = uuid4()
    try:
        async with session_scope() as session:
            session.add(Job(id=job_id))

        assert await mark_unhandled_job_failure(job_id, "celery-task-id") is True
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            assert job.status == JobStatus.FAILED
            assert job.error_code == "unhandled_task_error"
            assert job.error_message == UNHANDLED_ERROR_MESSAGE
            assert job.celery_task_id == "celery-task-id"
            assert job.heartbeat_at is not None
            assert job.finished_at is not None

        assert await mark_unhandled_job_failure(job_id, "other-task-id") is False
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            assert job.error_message == UNHANDLED_ERROR_MESSAGE
            assert job.celery_task_id == "celery-task-id"
    finally:
        async with session_scope() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
        await engine.dispose()
