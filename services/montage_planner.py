"""Два ограниченных вызова LLM и построение проверенного timeline."""

import json
import os
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError

from schemas.montage import Candidate, ClipSelection, FRAME_MS
from schemas.montage import MontageCreate, SearchPlan, Timeline, TimelineClip
from services.embedding_provider import _is_local_hostname
from services.ollama_provider import OllamaProvider
from services.prompt_loader import load_prompt


class MontageError(RuntimeError):
    error_code = "montage_failed"


class InsufficientMaterialError(MontageError):
    error_code = "insufficient_material"


def planner_settings() -> tuple[str, str, int]:
    model = os.getenv("OLLAMA_PLANNER_MODEL", "").strip()
    endpoint = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    parsed = urlparse(endpoint)
    if not model or os.getenv("PLANNER_PROVIDER", "disabled") != "ollama":
        raise MontageError("Задайте PLANNER_PROVIDER=ollama и OLLAMA_PLANNER_MODEL")
    if parsed.scheme not in {"http", "https"} or not _is_local_hostname(
        parsed.hostname
    ):
        raise MontageError("Планировщику разрешён только локальный Ollama endpoint")
    try:
        timeout = int(os.getenv("PLANNER_TIMEOUT_SECONDS", "180"))
    except ValueError as error:
        raise MontageError("Некорректный PLANNER_TIMEOUT_SECONDS") from error
    if not 1 <= timeout <= 600:
        raise MontageError("PLANNER_TIMEOUT_SECONDS должен быть от 1 до 600")
    return endpoint, model, timeout


def target_duration(request: MontageCreate) -> int:
    return round(request.target_duration_ms / FRAME_MS) * FRAME_MS


def generate_structured(filename: str, schema, context: dict, validate=None):
    endpoint, model, timeout = planner_settings()
    template = load_prompt(filename)
    provider = OllamaProvider(endpoint, timeout)
    response_schema = schema.model_json_schema()
    if schema is ClipSelection:
        response_schema["$defs"]["ClipChoice"]["properties"]["candidate_id"]["enum"] = [
            item["candidate_id"] for item in context["candidates"]
        ]
    prompt = (
        template.text
        + "\nВХОДНЫЕ ДАННЫЕ JSON:\n"
        + json.dumps(
            context,
            ensure_ascii=False,
        )
    )
    for attempt in range(2):
        response = provider.generate(
            model=model,
            prompt=prompt,
            images=[],
            response_format=response_schema,
            options={"num_ctx": 8192, "num_predict": 4096, "temperature": 0.1},
        )
        try:
            result = schema.model_validate_json(response.get("response", ""))
            if validate is not None:
                validate(result)
        except (ValidationError, TypeError, MontageError) as error:
            if attempt == 0:
                diagnostic = (
                    str(error)
                    if isinstance(error, MontageError)
                    else "Нарушена схема JSON"
                )
                prompt += f"\nИсправь ответ: {diagnostic}. Верни только валидный JSON."
                continue
            if isinstance(error, MontageError):
                raise
            raise MontageError("Планировщик дважды вернул ответ неверной схемы")
        provenance = {
            "provider": "ollama",
            "model": response.get("model", model),
            "prompt_version": template.version,
            "schema": schema.__name__,
            "attempts": attempt + 1,
            "total_duration_ns": response.get("total_duration"),
            "load_duration_ns": response.get("load_duration"),
        }
        return result, provenance
    raise MontageError("Не получен ответ планировщика")


def create_search_plan(request: MontageCreate):
    plan, provenance = generate_structured(
        "montage_search.ru.txt",
        SearchPlan,
        {"prompt": request.prompt, "target_duration_ms": target_duration(request)},
    )
    provenance["suggested_durations_ms"] = [item.duration_ms for item in plan.sections]
    plan = normalize_search_budgets(plan, target_duration(request))
    return plan, provenance


def normalize_search_budgets(plan: SearchPlan, duration_ms: int) -> SearchPlan:
    """Нормализовать пожелания модели обычной арифметикой на кадровой сетке."""
    remaining = duration_ms // FRAME_MS - len(plan.sections)
    total_weight = sum(item.duration_ms for item in plan.sections)
    frames = [
        1 + item.duration_ms * remaining // total_weight for item in plan.sections
    ]
    order = sorted(
        range(len(frames)),
        key=lambda index: (
            -(plan.sections[index].duration_ms * remaining % total_weight),
            index,
        ),
    )
    for index in order[: duration_ms // FRAME_MS - sum(frames)]:
        frames[index] += 1
    return SearchPlan(
        sections=[
            section.model_copy(update={"duration_ms": count * FRAME_MS})
            for section, count in zip(plan.sections, frames)
        ]
    )


def choose_clips(request: MontageCreate, plan: SearchPlan, candidates: list[Candidate]):
    # Ограничить контекст, сохранив достаточную длительность каждой секции.
    offered = []
    for section in plan.sections:
        options = [item for item in candidates if item.section_id == section.section_id]
        available_ms = 0
        chosen = []
        for item in options:
            chosen.append(item)
            available_ms += (item.end_ms - item.start_ms) // FRAME_MS * FRAME_MS
            if len(chosen) >= 3 and available_ms >= section.duration_ms:
                break
        offered.extend(chosen)
    context_candidates = [
        {
            **item.model_dump(
                mode="json",
                include={
                    "candidate_id",
                    "section_id",
                    "asset_id",
                    "start_ms",
                    "end_ms",
                },
            ),
            "summary": item.summary[:300],
            "similarity": round(item.similarity, 4),
        }
        for item in offered
    ]
    if len(json.dumps(context_candidates, ensure_ascii=False)) > 24000:
        raise MontageError("Подборка превышает контекст планировщика; сузьте запрос")
    selection, provenance = generate_structured(
        "montage_select.ru.txt",
        ClipSelection,
        {
            "prompt": request.prompt,
            "plan": plan.model_dump(mode="json"),
            "candidates": context_candidates,
        },
        validate=lambda result: build_timeline(request, plan, offered, result),
    )
    provenance["offered_candidate_ids"] = [str(item.candidate_id) for item in offered]
    provenance["context_version"] = "montage-candidates-v1"
    timeline = build_timeline(request, plan, candidates, selection)
    return selection, timeline, provenance


def build_timeline(
    request: MontageCreate,
    plan: SearchPlan,
    candidates: list[Candidate],
    selection: ClipSelection,
) -> Timeline:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    source_counts = {}
    for choice in selection.clips:
        item = by_id.get(choice.candidate_id)
        if item is not None:
            key = item.canonical_asset_id
            source_counts[key] = source_counts.get(key, 0) + 1
    budgets = {section.section_id: section.duration_ms for section in plan.sections}
    covered = {section: 0 for section in budgets}
    used = set()
    intervals: dict = {}
    clips = []
    cursor = 0
    for choice in selection.clips:
        candidate = by_id.get(choice.candidate_id)
        if candidate is None or choice.candidate_id in used:
            raise MontageError("Выбран неизвестный или повторный кандидат")
        duration = choice.duration_ms
        if duration % FRAME_MS or duration > candidate.end_ms - candidate.start_ms:
            raise MontageError("Длительность клипа выходит за разрешённые границы")
        section = candidate.section_id
        if section not in covered:
            raise MontageError("Кандидат относится к неизвестной секции")
        covered[section] += duration
        canonical = candidate.canonical_asset_id
        start, end = allocate_source_interval(
            candidate,
            duration,
            intervals.get(canonical, []),
            repeated_source=source_counts[canonical] > 1,
        )
        intervals.setdefault(canonical, []).append((start, end))
        used.add(choice.candidate_id)
        clips.append(
            TimelineClip(
                clip_id=uuid4(),
                candidate_id=candidate.candidate_id,
                asset_id=candidate.asset_id,
                shot_id=candidate.shot_id,
                source_start_ms=start,
                source_end_ms=end,
                timeline_start_ms=cursor,
                reason=choice.reason,
            )
        )
        cursor += duration
    if covered != budgets or cursor != target_duration(request):
        raise InsufficientMaterialError(
            "Выбор не покрывает бюджеты всех монтажных секций"
        )
    return Timeline(duration_ms=cursor, clips=clips)


def allocate_source_interval(candidate, duration, used, *, repeated_source=False):
    free = [(candidate.start_ms, candidate.end_ms)]
    for occupied_start, occupied_end in used:
        remaining = []
        for start, end in free:
            if occupied_end <= start or occupied_start >= end:
                remaining.append((start, end))
            else:
                if start < occupied_start:
                    remaining.append((start, occupied_start))
                if occupied_end < end:
                    remaining.append((occupied_end, end))
        free = remaining
    suitable = [(start, end) for start, end in free if end - start >= duration]
    if not suitable:
        raise MontageError("Выбранным клипам не хватает неперекрывающихся интервалов")
    left, right = suitable[0]
    # При повторном источнике заполняем слева, не дробя свободное время по краям.
    start = left if repeated_source else left + (right - left - duration) // 2
    return start, start + duration
