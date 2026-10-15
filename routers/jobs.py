"""Запуск и просмотр фоновых задач."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.jobs import JobResponse
from sql.enums import JobStatus, JobType
from sql.models import Job, MediaRoot
from worker import celery_app

router = APIRouter(tags=["jobs"])
ACTIVE_JOB_STATUSES = (JobStatus.PENDING.value, JobStatus.RUNNING.value)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Лениво создать DB-сессию, не связывая `/health` с конфигурацией БД."""
    from sql.database import session_factory

    async with session_factory() as session:
        yield session


@router.post(
    "/media-roots/{media_root_id}/scan",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_library_scan(
    media_root_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Создать или вернуть активную задачу сканирования медиакорня."""
    media_root = await session.get(MediaRoot, media_root_id)
    if media_root is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Корень медиатеки не найден",
        )
    if not media_root.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Корень медиатеки отключён",
        )

    active_job = await session.scalar(
        select(Job)
        .where(
            Job.media_root_id == media_root_id,
            Job.job_type == JobType.LIBRARY_SCAN.value,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if active_job is not None:
        return active_job

    job_id = uuid4()
    job = Job(
        id=job_id,
        job_type=JobType.LIBRARY_SCAN.value,
        status=JobStatus.PENDING.value,
        media_root_id=media_root_id,
        celery_task_id=str(job_id),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    try:
        await asyncio.to_thread(
            celery_app.send_task,
            "easy_cut.library_scan",
            args=[str(job_id)],
            task_id=str(job_id),
        )
    except Exception as error:
        failed_at = datetime.now(UTC)
        job.status = JobStatus.FAILED.value
        job.error_code = "queue_unavailable"
        job.error_message = "Не удалось отправить задачу в очередь"
        job.finished_at = failed_at
        job.heartbeat_at = failed_at
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Очередь фоновых задач недоступна",
                "job_id": str(job_id),
            },
        ) from error

    return job


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Вернуть текущее состояние фоновой задачи."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Задача не найдена",
        )
    return job
