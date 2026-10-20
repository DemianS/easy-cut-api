"""Сохраняемый диспетчер и выполнение стадий монтажа без ожидания дочерних jobs."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import logging
from uuid import UUID, uuid4

from sqlalchemy import select, text

from schemas.montage import Candidate, MontageCreate, SearchPlan, Timeline
from services.logging_config import log_event, LogStatus
from services.logging_config import reset_request_id, set_request_id
from services.montage_planner import MontageError, choose_clips, create_search_plan
from services.montage_search import retrieve_candidates, source_for_asset
from services.video_renderer import RenderSource, render_timeline
from sql.database import engine, session_scope
from sql.enums import JobStatus, JobType
from sql.models import Job, MontageRender, MontageRun, Shot, TimelineVersion

logger = logging.getLogger(__name__)
STAGES = (
    JobType.MONTAGE_PLAN,
    JobType.MONTAGE_SEARCH,
    JobType.MONTAGE_SELECT,
    JobType.MONTAGE_RENDER,
)
ACTIVE = {JobStatus.PENDING, JobStatus.RUNNING}


async def advance_montages():
    from services.job_dispatcher import DispatchRequest, montage_queue
    from services.montage_review import create_render_job, RenderCapacityError

    dispatches = []
    async with session_scope() as session:
        parent_ids = list(
            await session.scalars(
                select(Job.id).where(
                    Job.job_type == JobType.MONTAGE.value,
                    Job.status.in_(ACTIVE),
                )
            )
        )
        for parent_id in parent_ids:
            # Единый порядок блокировок с API: сначала run, затем parent.
            run = await session.scalar(
                select(MontageRun)
                .where(
                    MontageRun.job_id == parent_id,
                )
                .with_for_update(skip_locked=True)
            )
            if run is None:
                continue
            parent = await session.scalar(
                select(Job)
                .where(
                    Job.id == parent_id,
                    Job.status.in_(ACTIVE),
                )
                .with_for_update(skip_locked=True)
            )
            if parent is None:
                continue
            children = list(
                await session.scalars(
                    select(Job)
                    .where(
                        Job.parent_job_id == parent.id,
                    )
                    .order_by(Job.created_at)
                )
            )
            by_stage = {job.job_type: job for job in children}
            automatic = (
                MontageCreate.model_validate(run.request).auto_render
                and not run.review_required
            )
            stages = STAGES if automatic else STAGES[:-1]
            now = datetime.now(UTC)
            parent.status = JobStatus.RUNNING
            parent.started_at = parent.started_at or now
            parent.heartbeat_at = now
            parent.progress_total = len(stages)
            completed = 0
            for stage in stages:
                child = by_stage.get(stage.value)
                if child is None:
                    if stage == JobType.MONTAGE_RENDER:
                        version = await session.get(
                            TimelineVersion,
                            run.current_timeline_version_id,
                        )
                        if version is None:
                            parent.status = JobStatus.FAILED
                            parent.error_code = "timeline_missing"
                            parent.error_message = "Сохранённый timeline не найден"
                            parent.finished_at = now
                            break
                        try:
                            _, child = await create_render_job(session, run, version)
                        except RenderCapacityError:
                            parent.result = {"stage": "waiting_render_capacity"}
                            break
                        if child is None:
                            break
                    else:
                        child = Job(
                            id=uuid4(),
                            job_type=stage.value,
                            parent_job_id=parent.id,
                            media_root_id=parent.media_root_id,
                            status=JobStatus.PENDING,
                            celery_task_id=str(uuid4()),
                            last_dispatched_at=now,
                        )
                        session.add(child)
                    dispatches.append(
                        DispatchRequest(
                            child.id,
                            child.celery_task_id,
                            "easy_cut.montage_stage",
                            montage_queue(stage),
                        )
                    )
                    parent.result = {"stage": stage.value}
                    break
                if child.status in ACTIVE:
                    parent.result = {"stage": stage.value}
                    break
                if child.status != JobStatus.SUCCEEDED:
                    parent.status = (
                        JobStatus.CANCELLED
                        if child.status == JobStatus.CANCELLED
                        else JobStatus.FAILED
                    )
                    parent.error_code = child.error_code or "montage_stage_failed"
                    parent.error_message = (
                        child.error_message or "Стадия монтажа прервана"
                    )
                    parent.result = {"stage": stage.value}
                    parent.finished_at = now
                    break
                completed += 1
            else:
                parent.status = JobStatus.SUCCEEDED
                parent.result = {
                    "stage": "completed" if automatic else "awaiting_selection",
                }
                parent.finished_at = now
            parent.progress_current = completed
    return dispatches


async def _heartbeat(job_id: UUID, task_id: str | None):
    while True:
        await asyncio.sleep(5)
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return
            if task_id is not None and job.celery_task_id != task_id:
                return
            job.heartbeat_at = datetime.now(UTC)


async def _perform_stage(stage: str, run: MontageRun, job_id: UUID) -> dict:
    request = MontageCreate.model_validate(run.request)
    if stage == JobType.MONTAGE_PLAN:
        plan, metadata = await asyncio.to_thread(create_search_plan, request)
        return {"search_plan": plan.model_dump(mode="json"), "metadata": metadata}
    if stage == JobType.MONTAGE_SEARCH:
        plan = SearchPlan.model_validate(run.search_plan)
        candidates = await retrieve_candidates(request, plan)
        return {"candidates": [item.model_dump(mode="json") for item in candidates]}
    if stage == JobType.MONTAGE_SELECT:
        plan = SearchPlan.model_validate(run.search_plan)
        candidates = [Candidate.model_validate(item) for item in run.candidates]
        selection, timeline, metadata = await asyncio.to_thread(
            choose_clips,
            request,
            plan,
            candidates,
        )
        return {
            "selection": selection.model_dump(mode="json"),
            "timeline": timeline.model_dump(mode="json"),
            "metadata": metadata,
        }
    if stage != JobType.MONTAGE_RENDER:
        raise MontageError("Неизвестная стадия монтажа")
    async with session_scope() as session:
        render = await session.scalar(
            select(MontageRender).where(
                MontageRender.job_id == job_id,
                MontageRender.montage_run_id == run.id,
            )
        )
        version = (
            await session.get(TimelineVersion, render.timeline_version_id)
            if render
            else None
        )
    if version is None:
        raise MontageError("Сохранённый timeline не найден")
    timeline = Timeline.model_validate(version.timeline)
    by_id = {
        item["candidate_id"]: Candidate.model_validate(item)
        for item in version.candidates
    }
    sources = {}
    for clip in timeline.clips:
        candidate = by_id[str(clip.candidate_id)]
        async with session_scope() as session:
            shot = await session.get(Shot, clip.shot_id)
        if (
            shot is None
            or shot.asset_id != clip.asset_id
            or clip.source_start_ms < shot.start_ms
            or clip.source_end_ms > shot.end_ms
        ):
            raise MontageError("Исходный план изменён или удалён")
        path, stat, asset = await source_for_asset(clip.asset_id, request)
        if (
            stat.st_size != candidate.size_bytes
            or stat.st_mtime_ns != candidate.modified_at_ns
            or asset.checksum != candidate.checksum
            or clip.source_start_ms < candidate.start_ms
            or clip.source_end_ms > candidate.end_ms
        ):
            raise MontageError("Источник или разрешённый интервал изменился")
        sources[clip.asset_id] = RenderSource(
            path,
            stat.st_size,
            stat.st_mtime_ns,
            bool(asset.audio_codec),
        )
    output_path = await asyncio.to_thread(
        render_timeline, version.id, timeline, sources
    )
    return {
        "output_path": output_path,
        "timeline_version_id": str(version.id),
        "render_id": str(render.id),
    }


async def execute_montage_stage(job_id: UUID, task_id: str | None = None) -> dict:
    # Повторные render jobs одной версии разделяют рабочий каталог и lock.
    async with session_scope() as session:
        render_version_id = await session.scalar(
            select(MontageRender.timeline_version_id).where(
                MontageRender.job_id == job_id,
            )
        )
    lock_identity = render_version_id or job_id
    lock_key = int.from_bytes(lock_identity.bytes[:8], "big", signed=True)
    async with engine.begin() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": lock_key},
        )
        if not acquired:
            return {"reused": True}
        async with session_scope() as session:
            job = await session.scalar(
                select(Job).where(Job.id == job_id).with_for_update()
            )
            if job is None or job.job_type not in STAGES:
                raise MontageError("Стадия монтажа не найдена")
            if job.status not in ACTIVE:
                return job.result or {}
            if task_id is not None and job.celery_task_id != task_id:
                return {"stale_delivery": True}
            run = await session.scalar(
                select(MontageRun).where(
                    MontageRun.job_id == job.parent_job_id,
                )
            )
            if run is None:
                raise MontageError("Запрос монтажа не найден")
            stage = job.job_type
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or datetime.now(UTC)
            job.heartbeat_at = datetime.now(UTC)
            request_id = run.provenance.get("request_id")
        log_event(
            logger,
            logging.INFO,
            "montage_stage",
            LogStatus.STARTED,
            "Стадия монтажа начата",
            stage=stage,
            job_id=str(job_id),
            request_id=request_id,
        )
        request_token = set_request_id(request_id)
        heartbeat = asyncio.create_task(_heartbeat(job_id, task_id))
        try:
            result = await _perform_stage(stage, run, job_id)
            async with session_scope() as session:
                job = await session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update()
                )
                if job.status not in ACTIVE or (
                    task_id is not None and job.celery_task_id != task_id
                ):
                    return {"stale_delivery": True}
                stored = await session.scalar(
                    select(MontageRun)
                    .where(
                        MontageRun.id == run.id,
                    )
                    .with_for_update()
                )
                if "search_plan" in result:
                    stored.search_plan = result["search_plan"]
                if "candidates" in result:
                    stored.candidates = result["candidates"]
                if "selection" in result:
                    stored.selection = result["selection"]
                    if stored.current_timeline_version_id is None:
                        version = TimelineVersion(
                            id=uuid4(),
                            project_id=run.project_id,
                            montage_run_id=run.id,
                            timeline=result["timeline"],
                            selection=result["selection"],
                            candidates=stored.candidates or [],
                            source="llm",
                        )
                        session.add(version)
                        await session.flush()
                        stored.current_timeline_version_id = version.id
                if "output_path" in result:
                    render = await session.get(MontageRender, UUID(result["render_id"]))
                    render.output_path = result["output_path"]
                    if stored.current_timeline_version_id == render.timeline_version_id:
                        stored.output_path = result["output_path"]
                if "metadata" in result:
                    stored.provenance = {**stored.provenance, stage: result["metadata"]}
                job.result = {"stage": stage}
                if "render_id" in result:
                    job.result = {
                        **job.result,
                        "render_id": result["render_id"],
                        "timeline_version_id": result["timeline_version_id"],
                    }
                job.status = JobStatus.SUCCEEDED
                job.finished_at = datetime.now(UTC)
                job.heartbeat_at = job.finished_at
                job.error_code = None
                job.error_message = None
            log_event(
                logger,
                logging.INFO,
                "montage_stage",
                LogStatus.SUCCEEDED,
                "Стадия монтажа завершена",
                stage=stage,
                job_id=str(job_id),
                request_id=request_id,
            )
            return {"stage": stage}
        except Exception as error:
            async with session_scope() as session:
                job = await session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update()
                )
                if job.status in ACTIVE and (
                    task_id is None or job.celery_task_id == task_id
                ):
                    job.status = JobStatus.FAILED
                    job.error_code = getattr(
                        error, "error_code", "montage_stage_failed"
                    )
                    job.error_message = (
                        str(error)[:1000]
                        if isinstance(error, MontageError)
                        else "Ошибка стадии монтажа; проверьте модель и сервисы"
                    )
                    job.finished_at = datetime.now(UTC)
                    job.heartbeat_at = job.finished_at
                    if hasattr(error, "candidates"):
                        stored = await session.get(MontageRun, run.id)
                        stored.candidates = error.candidates
            log_event(
                logger,
                logging.ERROR,
                "montage_stage",
                LogStatus.FAILED,
                "Стадия монтажа завершилась ошибкой",
                stage=stage,
                job_id=str(job_id),
                request_id=request_id,
                error_type=type(error).__name__,
            )
            # Предметная ошибка сохранена; не выводим ответ модели в Celery traceback.
            return {"failed": True}
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            reset_request_id(request_token)
