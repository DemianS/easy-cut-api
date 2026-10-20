"""Ручной выбор, неизменяемые версии и привязка рендера к версии."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import or_, select, text

from schemas.montage import Candidate, ClipSelection, MontageCreate, SearchPlan
from schemas.montage import SelectionUpdate, Timeline
from schemas.montage import MAX_DURATION_MS, MIN_DURATION_MS
from services.montage_planner import MontageError, build_timeline
from sql.enums import JobStatus, JobType
from sql.models import Asset, AssetLocation, Job, MediaRoot, MontageRender
from sql.models import MontageRun, Shot, TimelineVersion

ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
MAX_ACTIVE_RENDERS = 32
RENDER_BACKLOG_LOCK_KEY = 70202027


class ReviewConflict(MontageError):
    """Конфликт текущей версии или ещё не завершённая подготовка."""


class RenderCapacityError(ReviewConflict):
    error_code = "render_queue_full"


def build_manual_timeline(request, plan, candidates, update: SelectionUpdate):
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    budgets = {}
    for choice in update.clips:
        candidate = by_id.get(choice.candidate_id)
        if candidate is None:
            raise MontageError("Кандидат отсутствует в подборке этого монтажа")
        budgets[candidate.section_id] = (
            budgets.get(candidate.section_id, 0) + choice.duration_ms
        )
    duration = sum(budgets.values())
    if not MIN_DURATION_MS <= duration <= MAX_DURATION_MS:
        raise MontageError("Ручной монтаж должен длиться от 1 до 120 секунд")
    manual_plan = SearchPlan(
        sections=[
            section.model_copy(update={"duration_ms": budgets[section.section_id]})
            for section in plan.sections
            if section.section_id in budgets
        ]
    )
    selection = ClipSelection(clips=update.clips)
    timeline = build_timeline(
        request.model_copy(update={"target_duration_ms": duration}),
        manual_plan,
        candidates,
        selection,
    )
    return selection, timeline


async def update_selection(session, run: MontageRun, update: SelectionUpdate):
    parent = await session.get(Job, run.job_id)
    await require_prepared(session, run)
    if run.current_timeline_version_id is None:
        raise ReviewConflict("Подготовка монтажа ещё не завершена")
    if update.base_version_id != run.current_timeline_version_id:
        raise ReviewConflict("Timeline уже изменён; перечитайте текущую версию")
    base = await session.get(TimelineVersion, update.base_version_id)
    if base is None or base.montage_run_id != run.id:
        raise ReviewConflict("Базовая версия не принадлежит монтажу")
    selection, timeline = build_manual_timeline(
        MontageCreate.model_validate(run.request),
        SearchPlan.model_validate(run.search_plan),
        [Candidate.model_validate(item) for item in run.candidates],
        update,
    )
    version = TimelineVersion(
        id=uuid4(),
        project_id=run.project_id,
        montage_run_id=run.id,
        parent_version_id=base.id,
        timeline=timeline.model_dump(mode="json"),
        selection=selection.model_dump(mode="json"),
        candidates=run.candidates,
        source="user",
    )
    session.add(version)
    await session.flush()
    run.current_timeline_version_id = version.id
    run.selection = version.selection
    run.output_path = None
    run.review_required = True
    parent.status = JobStatus.SUCCEEDED
    parent.result = {"stage": "awaiting_selection"}
    parent.error_code = None
    parent.error_message = None
    parent.progress_current = 3
    parent.progress_total = 3
    parent.finished_at = datetime.now(UTC)
    return version


async def require_prepared(session, run):
    prepared = await session.scalar(
        select(Job.id)
        .where(
            Job.parent_job_id == run.job_id,
            Job.job_type == JobType.MONTAGE_SELECT,
            Job.status == JobStatus.SUCCEEDED,
        )
        .limit(1)
    )
    if prepared is None:
        raise ReviewConflict("Подготовка подборки и timeline ещё не завершена")


async def validate_version_assets(session, run, version):
    timeline = Timeline.model_validate(version.timeline)
    request = MontageCreate.model_validate(run.request)
    candidates = {
        item["candidate_id"]: Candidate.model_validate(item)
        for item in version.candidates
    }
    root = await session.get(MediaRoot, request.media_root_id)
    if root is None or not root.is_enabled:
        raise ReviewConflict("Медиакорень отключён или удалён")
    for clip in timeline.clips:
        candidate = candidates.get(str(clip.candidate_id))
        if (
            candidate is None
            or candidate.asset_id != clip.asset_id
            or candidate.shot_id != clip.shot_id
            or clip.source_start_ms < candidate.start_ms
            or clip.source_end_ms > candidate.end_ms
        ):
            raise ReviewConflict("Версия содержит недопустимый источник или интервал")
        asset = await session.get(Asset, clip.asset_id)
        if (
            asset is None
            or asset.checksum != candidate.checksum
            or asset.media_kind != "video"
            or asset.duration_ms is None
            or clip.source_end_ms > asset.duration_ms
            or (request.asset_ids and asset.id not in request.asset_ids)
        ):
            raise ReviewConflict(
                "Источник изменился или недоступен в выбранной области"
            )
        shot = await session.get(Shot, clip.shot_id)
        if (
            shot is None
            or shot.asset_id != asset.id
            or clip.source_start_ms < shot.start_ms
            or clip.source_end_ms > shot.end_ms
        ):
            raise ReviewConflict("Исходный план изменён или удалён")
        location = await session.scalar(
            select(AssetLocation)
            .where(
                AssetLocation.asset_id == clip.asset_id,
                AssetLocation.media_root_id == request.media_root_id,
                AssetLocation.is_available.is_(True),
                AssetLocation.size_bytes == candidate.size_bytes,
                or_(
                    AssetLocation.modified_at_ns.is_(None),
                    AssetLocation.modified_at_ns == candidate.modified_at_ns,
                ),
            )
            .limit(1)
        )
        if location is None or (
            location.modified_at_ns is not None
            and location.modified_at_ns != candidate.modified_at_ns
        ):
            raise ReviewConflict(
                "Расположение источника изменилось; повторите индексацию"
            )


async def create_render_job(session, run, version):
    existing = await session.scalar(
        select(MontageRender)
        .join(
            Job,
            Job.id == MontageRender.job_id,
        )
        .where(
            MontageRender.montage_run_id == run.id,
            MontageRender.timeline_version_id == version.id,
            Job.status.in_((*ACTIVE_STATUSES, JobStatus.SUCCEEDED)),
        )
        .order_by(MontageRender.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing, None
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {
            "key": RENDER_BACKLOG_LOCK_KEY,
        },
    )
    active = list(
        await session.scalars(
            select(Job.id)
            .where(
                Job.job_type == JobType.MONTAGE_RENDER,
                Job.status.in_(ACTIVE_STATUSES),
            )
            .limit(MAX_ACTIVE_RENDERS)
        )
    )
    if len(active) >= MAX_ACTIVE_RENDERS:
        raise RenderCapacityError("Очередь рендеров заполнена; повторите позже")
    job = Job(
        id=uuid4(),
        job_type=JobType.MONTAGE_RENDER,
        parent_job_id=run.job_id,
        media_root_id=MontageCreate.model_validate(run.request).media_root_id,
        status=JobStatus.PENDING,
        celery_task_id=str(uuid4()),
        last_dispatched_at=datetime.now(UTC),
    )
    session.add(job)
    await session.flush()
    render = MontageRender(
        id=uuid4(),
        montage_run_id=run.id,
        timeline_version_id=version.id,
        job_id=job.id,
    )
    session.add(render)
    await session.flush()
    return render, job


def montage_state(parent, current_version_id, renders, *, review_required=False):
    current = [
        (render, job)
        for render, job in renders
        if render.timeline_version_id == current_version_id
    ]
    if any(job.status in ACTIVE_STATUSES for _, job in current):
        return "rendering"
    if any(job.status == JobStatus.SUCCEEDED for _, job in current):
        return "completed"
    if current and current[-1][1].status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        return "failed"
    if current_version_id and review_required:
        return "awaiting_selection"
    if parent.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        return "failed"
    if current_version_id and parent.status == JobStatus.SUCCEEDED:
        return "awaiting_selection"
    return "preparing"
