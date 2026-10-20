from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.montage import Candidate, ClipChoice, ClipSelection, MontageCreate
from schemas.montage import SearchPlan, SearchSection, Timeline
from schemas.montage import SelectionUpdate
from services.montage_planner import MontageError, build_timeline
from services.montage_planner import normalize_search_budgets
from services.video_renderer import managed_artifact
from services.montage_review import build_manual_timeline


def scenario():
    request = MontageCreate(
        prompt="Мастерская", media_root_id=uuid4(), target_duration_ms=2000
    )
    plan = SearchPlan(
        sections=[
            SearchSection(
                section_id="intro",
                role="вступление",
                query="мастерская",
                duration_ms=2000,
            )
        ]
    )
    asset_id = uuid4()
    candidate = Candidate(
        candidate_id=uuid4(),
        section_id="intro",
        asset_id=asset_id,
        shot_id=uuid4(),
        start_ms=1000,
        end_ms=5000,
        summary="Мастерская",
        similarity=0.8,
        size_bytes=10,
        modified_at_ns=20,
        canonical_asset_id=asset_id,
    )
    return request, plan, candidate


def test_selection_builds_centered_bounded_timeline():
    request, plan, candidate = scenario()
    selection = ClipSelection(
        clips=[
            ClipChoice(
                candidate_id=candidate.candidate_id,
                duration_ms=2000,
                reason="общий план",
            )
        ]
    )
    timeline = build_timeline(request, plan, [candidate], selection)
    assert timeline.clips[0].source_start_ms == 2000
    assert timeline.clips[0].source_end_ms == 4000
    assert timeline.duration_ms == 2000
    assert Timeline.model_validate_json(timeline.model_dump_json()) == timeline


@pytest.mark.parametrize("failure", ["unknown", "too_long", "off_grid", "duplicate"])
def test_selection_rejects_untrusted_choices(failure):
    request, plan, candidate = scenario()
    choice = ClipChoice(
        candidate_id=uuid4() if failure == "unknown" else candidate.candidate_id,
        duration_ms={"too_long": 6000, "off_grid": 2001}.get(failure, 2000),
        reason="общий план",
    )
    selection = ClipSelection(
        clips=[choice, choice] if failure == "duplicate" else [choice]
    )
    with pytest.raises(MontageError):
        build_timeline(request, plan, [candidate], selection)


def test_timeline_rejects_gaps_and_wrong_duration():
    request, plan, candidate = scenario()
    timeline = build_timeline(
        request,
        plan,
        [candidate],
        ClipSelection(
            clips=[
                ClipChoice(
                    candidate_id=candidate.candidate_id,
                    duration_ms=2000,
                    reason="общий",
                )
            ]
        ),
    )
    payload = timeline.model_dump(mode="json")
    payload["clips"][0]["timeline_start_ms"] = 40
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)


def test_output_path_cannot_escape_artifact_root(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACTS_PATH", str(tmp_path))
    with pytest.raises(MontageError):
        managed_artifact("../outside.mp4", must_exist=False)


def test_backend_normalizes_model_duration_arithmetic():
    plan = SearchPlan(
        sections=[
            SearchSection(
                section_id="a", role="общий", query="мастерская", duration_ms=7000
            ),
            SearchSection(
                section_id="b", role="деталь", query="инструменты", duration_ms=7000
            ),
            SearchSection(
                section_id="c", role="финал", query="результат", duration_ms=3000
            ),
        ]
    )
    normalized = normalize_search_budgets(plan, 30000)
    assert sum(item.duration_ms for item in normalized.sections) == 30000
    assert all(
        item.duration_ms > 0 and item.duration_ms % 40 == 0
        for item in normalized.sections
    )
    assert (
        abs(normalized.sections[0].duration_ms - normalized.sections[1].duration_ms)
        <= 40
    )


def test_manual_choice_can_change_duration_without_calling_model():
    request, plan, candidate = scenario()
    update = SelectionUpdate(
        base_version_id=uuid4(),
        clips=[
            {
                "candidate_id": candidate.candidate_id,
                "duration_ms": 1000,
            }
        ],
    )
    selection, timeline = build_manual_timeline(request, plan, [candidate], update)
    assert timeline.duration_ms == 1000
    assert selection.clips[0].reason == "Выбрано пользователем"
    assert request.target_duration_ms == 2000


def test_manual_choice_rejects_fractional_frame_and_unknown_candidate():
    request, plan, candidate = scenario()
    for candidate_id, duration in [(candidate.candidate_id, 1001), (uuid4(), 1000)]:
        update = SelectionUpdate(
            base_version_id=uuid4(),
            clips=[
                {
                    "candidate_id": candidate_id,
                    "duration_ms": duration,
                }
            ],
        )
        with pytest.raises(MontageError):
            build_manual_timeline(request, plan, [candidate], update)
