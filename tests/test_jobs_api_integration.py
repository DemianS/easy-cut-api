import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1",
    reason="Требуется локальная PostgreSQL с применёнными миграциями",
)
def test_start_library_scan_reuses_active_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sent_tasks: list[tuple[str, list[str], str]] = []

    def fake_send_task(task_name, args, task_id):
        sent_tasks.append((task_name, args, task_id))

    monkeypatch.setattr("routers.jobs.celery_app.send_task", fake_send_task)
    first_job_id, second_job_id = asyncio.run(_run_start_scan_scenario(tmp_path))

    assert first_job_id == second_job_id
    assert sent_tasks == [
        ("easy_cut.library_scan", [str(first_job_id)], str(first_job_id))
    ]


async def _run_start_scan_scenario(media_path: Path):
    from sqlalchemy import delete

    from routers.jobs import start_library_scan
    from sql.database import engine, session_factory, session_scope
    from sql.models import Job, MediaRoot, User

    user_id = uuid4()
    media_root_id = uuid4()
    try:
        async with session_scope() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"jobs-api-{user_id.hex}",
                )
            )
            await session.flush()
            session.add(
                MediaRoot(
                    id=media_root_id,
                    name="jobs-api-integration-test",
                    container_path=str(media_path),
                    created_by_user_id=user_id,
                )
            )

        async with session_factory() as session:
            first_job = await start_library_scan(media_root_id, session)
        async with session_factory() as session:
            second_job = await start_library_scan(media_root_id, session)

        return first_job.id, second_job.id
    finally:
        async with session_scope() as session:
            await session.execute(delete(Job).where(Job.media_root_id == media_root_id))
            await session.execute(
                delete(MediaRoot).where(MediaRoot.id == media_root_id)
            )
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
