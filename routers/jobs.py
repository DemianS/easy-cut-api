"""Запуск и просмотр фоновых задач."""

import asyncio
from datetime import UTC, datetime
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.jobs import JobResponse
from services.logging_config import log_event, LogStatus
from services.embedding_provider import EmbeddingProviderNotConfiguredError
from services.embedding_provider import get_embedding_settings
from services.vision_provider import VisionProviderNotConfiguredError
from services.vision_provider import get_vision_settings
from sql.enums import JobStatus, JobType
from sql.models import Job, MediaRoot
from worker import celery_app

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])
ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)


async def _get_enabled_media_root(
    media_root_id: UUID,
    session: AsyncSession,
) -> MediaRoot:
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
    return media_root


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
    log_event(
        logger,
        logging.INFO,
        "job_submission",
        LogStatus.STARTED,
        "Постановка задачи сканирования начата",
        stage="library_scan",
        media_root_id=str(media_root_id),
    )
    await _get_enabled_media_root(media_root_id, session)

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
        log_event(
            logger,
            logging.INFO,
            "job_submission",
            LogStatus.REUSED,
            "Возвращена уже активная задача сканирования",
            stage="library_scan",
            job_id=str(active_job.id),
            job_type=active_job.job_type,
            media_root_id=str(media_root_id),
        )
        return active_job

    job_id = uuid4()
    job = Job(
        id=job_id,
        job_type=JobType.LIBRARY_SCAN.value,
        status=JobStatus.PENDING,
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
        job.last_dispatched_at = datetime.now(UTC)
        await session.commit()
        log_event(
            logger,
            logging.INFO,
            "job_submission",
            LogStatus.DISPATCHED,
            "Задача сканирования отправлена в очередь",
            stage="library_scan",
            job_id=str(job_id),
            job_type=job.job_type,
            media_root_id=str(media_root_id),
        )
    except Exception as error:
        failed_at = datetime.now(UTC)
        job.error_code = "queue_unavailable"
        job.error_message = "Не удалось отправить задачу в очередь"
        job.last_dispatched_at = failed_at
        await session.commit()
        log_event(
            logger,
            logging.WARNING,
            "job_submission",
            LogStatus.FAILED,
            "Не удалось отправить задачу сканирования в очередь",
            stage="library_scan",
            job_id=str(job_id),
            job_type=job.job_type,
            media_root_id=str(media_root_id),
            error_type=type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Очередь фоновых задач недоступна",
                "job_id": str(job_id),
            },
        ) from error

    return job


@router.post(
    "/media-roots/{media_root_id}/prepare",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_library_preparation(
    media_root_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Запустить полную подготовку медиакорня одной составной задачей."""
    log_event(
        logger,
        logging.INFO,
        "job_submission",
        LogStatus.STARTED,
        "Постановка подготовки медиатеки начата",
        stage="library_preparation",
        media_root_id=str(media_root_id),
    )
    await _get_enabled_media_root(media_root_id, session)
    try:
        get_vision_settings()
        get_embedding_settings()
    except (
        VisionProviderNotConfiguredError,
        EmbeddingProviderNotConfiguredError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    active_job = await session.scalar(
        select(Job)
        .where(
            Job.media_root_id == media_root_id,
            Job.job_type == JobType.LIBRARY_PREPARATION.value,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if active_job is not None:
        log_event(
            logger,
            logging.INFO,
            "job_submission",
            LogStatus.REUSED,
            "Возвращена уже активная подготовка медиатеки",
            stage="library_preparation",
            job_id=str(active_job.id),
            job_type=active_job.job_type,
            media_root_id=str(media_root_id),
        )
        return active_job

    job = Job(
        id=uuid4(),
        job_type=JobType.LIBRARY_PREPARATION.value,
        status=JobStatus.PENDING,
        media_root_id=media_root_id,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    try:
        await asyncio.to_thread(
            celery_app.send_task,
            "easy_cut.reconcile_jobs",
        )
        log_event(
            logger,
            logging.INFO,
            "job_submission",
            LogStatus.DISPATCHED,
            "Подготовка медиатеки передана диспетчеру",
            stage="library_preparation",
            job_id=str(job.id),
            job_type=job.job_type,
            media_root_id=str(media_root_id),
        )
    except Exception as error:
        job.error_code = "queue_unavailable"
        job.error_message = "Не удалось запустить диспетчер фоновых задач"
        job.last_dispatched_at = datetime.now(UTC)
        await session.commit()
        log_event(
            logger,
            logging.WARNING,
            "job_submission",
            LogStatus.FAILED,
            "Не удалось запустить диспетчер подготовки медиатеки",
            stage="library_preparation",
            job_id=str(job.id),
            job_type=job.job_type,
            media_root_id=str(media_root_id),
            error_type=type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": job.error_message, "job_id": str(job.id)},
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
