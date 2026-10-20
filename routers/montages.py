"""Запуск, подборка, timeline и скачивание результата монтажа."""

import asyncio
from datetime import UTC, datetime
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from schemas.jobs import JobResponse
from schemas.montage import MontageCreate, RenderCreate, SelectionUpdate
from services.embedding_provider import get_embedding_settings
from services.montage_planner import MontageError, planner_settings
from services.montage_review import create_render_job, montage_state, require_prepared
from services.montage_review import RenderCapacityError
from services.montage_review import (
    ReviewConflict,
    update_selection,
    validate_version_assets,
)
from services.logging_config import log_event, LogStatus
from services.video_renderer import managed_artifact
from sql.enums import JobStatus, JobType
from sql.models import (
    Job,
    MediaRoot,
    MontageRender,
    MontageRun,
    Project,
    Shot,
    ShotFrame,
)
from sql.models import TimelineVersion
from worker import celery_app

router = APIRouter(prefix="/api/v1/montages", tags=["montages"])
logger = logging.getLogger(__name__)


async def _find_run(run_id: UUID, session: AsyncSession):
    run = await session.get(MontageRun, run_id)
    if run is None:
        raise HTTPException(404, "Запрос монтажа не найден")
    return run


async def _locked_run(run_id, session):
    run = await session.scalar(
        select(MontageRun)
        .where(
            MontageRun.id == run_id,
        )
        .with_for_update()
    )
    if run is None:
        raise HTTPException(404, "Запрос монтажа не найден")
    return run


def _render_response(render, job):
    return {
        "id": render.id,
        "job_id": job.id,
        "timeline_version_id": render.timeline_version_id,
        "job": JobResponse.model_validate(job),
        "download_url": (
            f"/api/v1/montages/{render.montage_run_id}/renders/{render.id}/video"
            if render.output_path and job.status == JobStatus.SUCCEEDED
            else None
        ),
    }


@router.post("", status_code=202)
async def create_montage(
    body: MontageCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, max_length=160),
    session: AsyncSession = Depends(get_session),
):
    payload = body.model_dump(mode="json")
    if idempotency_key:
        import hashlib

        digest = hashlib.sha256(idempotency_key.encode()).digest()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int.from_bytes(digest[:8], "big", signed=True),
            },
        )
        previous = await session.scalar(
            select(MontageRun)
            .join(
                Job,
                Job.id == MontageRun.job_id,
            )
            .where(Job.idempotency_key == f"montage:{idempotency_key}")
        )
        if previous is not None:
            if (
                MontageCreate.model_validate(previous.request).model_dump(mode="json")
                != payload
            ):
                raise HTTPException(409, "Idempotency-Key уже связан с другим запросом")
            return {"id": previous.id, "job_id": previous.job_id}
    root = await session.get(MediaRoot, body.media_root_id)
    if root is None or not root.is_enabled:
        raise HTTPException(404, "Активный медиакорень не найден")
    try:
        planner_settings()
        get_embedding_settings()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    # Ограничение сохраняемого backlog; сериализуется между API-процессами.
    await session.execute(text("SELECT pg_advisory_xact_lock(70202026)"))
    active = list(
        await session.scalars(
            select(Job.id)
            .where(
                Job.job_type == JobType.MONTAGE,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
            .limit(16)
        )
    )
    if len(active) >= 16:
        raise HTTPException(429, "Очередь монтажей заполнена; повторите позже")
    project = Project(id=uuid4(), name="Монтаж по запросу")
    job = Job(
        id=uuid4(),
        job_type=JobType.MONTAGE,
        media_root_id=body.media_root_id,
        status=JobStatus.PENDING,
        idempotency_key=f"montage:{idempotency_key}" if idempotency_key else None,
    )
    session.add_all([project, job])
    await session.flush()
    run = MontageRun(
        id=uuid4(),
        job_id=job.id,
        project_id=project.id,
        request=payload,
        review_required=not body.auto_render,
        provenance={
            "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(run)
    await session.commit()
    return {"id": run.id, "job_id": job.id}


@router.get("/{run_id}")
async def get_montage(run_id: UUID, session: AsyncSession = Depends(get_session)):
    run = await _find_run(run_id, session)
    job = await session.get(Job, run.job_id)
    children = list(
        await session.scalars(
            select(Job)
            .where(
                Job.parent_job_id == job.id,
            )
            .order_by(Job.created_at)
        )
    )
    version = (
        await session.get(TimelineVersion, run.current_timeline_version_id)
        if run.current_timeline_version_id
        else None
    )
    renders = (
        await session.execute(
            select(MontageRender, Job)
            .join(
                Job,
                Job.id == MontageRender.job_id,
            )
            .where(MontageRender.montage_run_id == run.id)
            .order_by(
                MontageRender.created_at,
                MontageRender.id,
            )
        )
    ).all()
    return {
        "id": run.id,
        "project_id": run.project_id,
        "state": montage_state(
            job,
            run.current_timeline_version_id,
            renders,
            review_required=run.review_required,
        ),
        "request": run.request,
        "job": JobResponse.model_validate(job),
        "stages": [JobResponse.model_validate(child) for child in children],
        "search_plan": run.search_plan,
        "candidates": [
            {
                **item,
                "thumbnail_url": (
                    f"/api/v1/montages/{run.id}/frames/"
                    f"{item['preview_frame_id']}/thumbnail"
                    if item.get("preview_frame_id")
                    else None
                ),
            }
            for item in (run.candidates or [])
        ],
        "selection": run.selection,
        "provenance": run.provenance,
        "timeline_version_id": version.id if version else None,
        "timeline": version.timeline if version else None,
        "renders": [
            _render_response(render, render_job) for render, render_job in renders
        ],
        "download_url": f"/api/v1/montages/{run.id}/video" if run.output_path else None,
    }


@router.patch("/{run_id}/selection")
async def change_selection(
    run_id: UUID,
    body: SelectionUpdate,
    session: AsyncSession = Depends(get_session),
):
    run = await _locked_run(run_id, session)
    try:
        version = await update_selection(session, run, body)
        await validate_version_assets(session, run, version)
    except ReviewConflict as error:
        raise HTTPException(409, str(error)) from error
    except MontageError as error:
        raise HTTPException(422, str(error)) from error
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "montage_selection",
        LogStatus.SUCCEEDED,
        "Ручной выбор сохранён новой версией timeline",
        stage="save_version",
        job_id=str(run.job_id),
    )
    return {
        "timeline_version_id": version.id,
        "parent_version_id": version.parent_version_id,
        "timeline": version.timeline,
        "selection": version.selection,
        "state": "awaiting_selection",
    }


@router.get("/{run_id}/versions")
async def list_versions(
    run_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    run = await _find_run(run_id, session)
    versions = list(
        await session.scalars(
            select(TimelineVersion)
            .where(
                TimelineVersion.montage_run_id == run.id,
            )
            .order_by(TimelineVersion.created_at.desc(), TimelineVersion.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return {
        "current_version_id": run.current_timeline_version_id,
        "items": [
            {
                "id": version.id,
                "parent_version_id": version.parent_version_id,
                "source": version.source,
                "timeline": version.timeline,
                "selection": version.selection,
                "created_at": version.created_at,
            }
            for version in versions
        ],
    }


@router.post("/{run_id}/renders", status_code=202)
async def start_render(
    run_id: UUID,
    body: RenderCreate,
    session: AsyncSession = Depends(get_session),
):
    run = await _locked_run(run_id, session)
    version = await session.get(TimelineVersion, body.timeline_version_id)
    if version is None or version.montage_run_id != run.id:
        raise HTTPException(404, "Версия timeline не принадлежит этому монтажу")
    try:
        await require_prepared(session, run)
        render, new_job = await create_render_job(session, run, version)
        if new_job is not None:
            await validate_version_assets(session, run, version)
    except RenderCapacityError as error:
        raise HTTPException(429, str(error)) from error
    except ReviewConflict as error:
        raise HTTPException(409, str(error)) from error
    await session.commit()
    if new_job is not None:
        try:
            await asyncio.to_thread(
                celery_app.send_task,
                "easy_cut.montage_stage",
                args=[str(new_job.id)],
                task_id=new_job.celery_task_id,
                queue="user_render",
            )
        except Exception as error:
            from services.job_dispatcher import record_dispatch_failure

            await record_dispatch_failure(new_job.id)
            log_event(
                logger,
                logging.WARNING,
                "montage_render_submission",
                LogStatus.FAILED,
                "Рендер сохранён; диспетчер повторит отправку в очередь",
                stage="broker",
                job_id=str(new_job.id),
                error_type=type(error).__name__,
            )
    job = await session.get(Job, render.job_id)
    return _render_response(render, job)


async def _get_render(run_id, render_id, session):
    render = await session.scalar(
        select(MontageRender).where(
            MontageRender.id == render_id,
            MontageRender.montage_run_id == run_id,
        )
    )
    if render is None:
        raise HTTPException(404, "Рендер не найден в этом монтаже")
    return render


@router.get("/{run_id}/renders/{render_id}")
async def get_render(
    run_id: UUID, render_id: UUID, session: AsyncSession = Depends(get_session)
):
    render = await _get_render(run_id, render_id, session)
    job = await session.get(Job, render.job_id)
    return _render_response(render, job)


@router.get("/{run_id}/renders/{render_id}/video")
async def download_render(
    run_id: UUID, render_id: UUID, session: AsyncSession = Depends(get_session)
):
    render = await _get_render(run_id, render_id, session)
    job = await session.get(Job, render.job_id)
    if job.status != JobStatus.SUCCEEDED or not render.output_path:
        raise HTTPException(409, "Рендер ещё не готов")
    try:
        path = managed_artifact(render.output_path)
    except MontageError as error:
        raise HTTPException(404, "Файл рендера недоступен") from error
    return FileResponse(
        path, media_type="video/mp4", filename=f"montage-{render.id}.mp4"
    )


@router.get("/{run_id}/video")
async def download_montage(run_id: UUID, session: AsyncSession = Depends(get_session)):
    run = await _find_run(run_id, session)
    if not run.output_path:
        raise HTTPException(409, "Результат монтажа ещё не готов")
    try:
        path = managed_artifact(run.output_path)
    except MontageError as error:
        raise HTTPException(404, "Файл результата недоступен") from error
    return FileResponse(path, media_type="video/mp4", filename=f"montage-{run.id}.mp4")


@router.get("/{run_id}/frames/{frame_id}/thumbnail")
async def candidate_thumbnail(
    run_id: UUID,
    frame_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    run = await _find_run(run_id, session)
    allowed = {item.get("preview_frame_id") for item in (run.candidates or [])}
    if str(frame_id) not in allowed:
        raise HTTPException(404, "Кадр не входит в подборку")
    frame = await session.scalar(
        select(ShotFrame)
        .join(
            Shot,
            Shot.id == ShotFrame.shot_id,
        )
        .where(ShotFrame.id == frame_id)
    )
    if frame is None or not frame.thumbnail_path:
        raise HTTPException(404, "Превью недоступно")
    try:
        path = managed_artifact(frame.thumbnail_path)
    except MontageError as error:
        raise HTTPException(404, "Превью недоступно") from error
    return FileResponse(path, media_type="image/webp")
