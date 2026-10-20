"""Строгие контракты планировщика и канонического монтажа."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

FRAME_MS = 40
MIN_DURATION_MS = 1000
MAX_DURATION_MS = 120_000
RENDER_PROFILE = "mp4-1080p25-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MontageCreate(StrictModel):
    prompt: str = Field(min_length=1, max_length=4000)
    media_root_id: UUID
    asset_ids: list[UUID] = Field(default_factory=list, max_length=200)
    target_duration_ms: int = Field(
        default=30000, ge=MIN_DURATION_MS, le=MAX_DURATION_MS
    )
    min_similarity: float = Field(default=0.2, ge=-1, le=1)
    auto_render: bool = True


class SearchSection(StrictModel):
    section_id: str = Field(min_length=1, max_length=40)
    role: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=1000)
    duration_ms: int = Field(ge=FRAME_MS, le=MAX_DURATION_MS)


class SearchPlan(StrictModel):
    sections: list[SearchSection] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_sections(self):
        identifiers = [item.section_id for item in self.sections]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Повторяющиеся ID секций")
        return self


class Candidate(StrictModel):
    candidate_id: UUID
    section_id: str
    asset_id: UUID
    shot_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    summary: str
    similarity: float
    preview_frame_id: UUID | None = None
    size_bytes: int
    modified_at_ns: int
    checksum: str | None = None
    canonical_asset_id: UUID


class ClipChoice(StrictModel):
    candidate_id: UUID
    duration_ms: int = Field(ge=FRAME_MS, le=MAX_DURATION_MS)
    reason: str = Field(min_length=1, max_length=1000)


class ClipSelection(StrictModel):
    clips: list[ClipChoice] = Field(min_length=1, max_length=24)


class ManualClipChoice(ClipChoice):
    reason: str = Field(default="Выбрано пользователем", min_length=1, max_length=1000)


class SelectionUpdate(StrictModel):
    base_version_id: UUID
    clips: list[ManualClipChoice] = Field(min_length=1, max_length=24)


class RenderCreate(StrictModel):
    timeline_version_id: UUID


class TimelineClip(StrictModel):
    clip_id: UUID
    candidate_id: UUID
    asset_id: UUID
    shot_id: UUID
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    timeline_start_ms: int = Field(ge=0)
    reason: str


class Timeline(StrictModel):
    schema_version: Literal["timeline-v1"] = "timeline-v1"
    profile: Literal["mp4-1080p25-v1"] = RENDER_PROFILE
    duration_ms: int = Field(gt=0, le=MAX_DURATION_MS)
    clips: list[TimelineClip] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_sequence(self):
        cursor = 0
        identifiers = set()
        for clip in self.clips:
            duration = clip.source_end_ms - clip.source_start_ms
            if duration <= 0 or duration % FRAME_MS:
                raise ValueError("Длительность клипа должна быть кратна кадру")
            if clip.timeline_start_ms != cursor or clip.clip_id in identifiers:
                raise ValueError("Нарушена последовательность клипов")
            identifiers.add(clip.clip_id)
            cursor += duration
        if cursor != self.duration_ms:
            raise ValueError("Длительность timeline не совпадает с клипами")
        return self
