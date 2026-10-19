"""Публичные схемы библиотеки медиаактивов."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sql.enums import AssetKind, AssetStatus
from sql.enums import DescriptionStatus


class AssetLocationResponse(BaseModel):
    """Физическое расположение актива в медиатеке."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    media_root_id: UUID
    relative_path: str
    is_available: bool
    size_bytes: int
    modified_at_ns: int | None
    first_seen_at: datetime
    last_seen_at: datetime
    missing_since: datetime | None


class AssetListItem(BaseModel):
    """Краткая карточка актива для списка библиотеки."""

    id: UUID
    status: AssetStatus
    media_kind: AssetKind
    relative_path: str | None
    is_available: bool
    format_name: str | None
    mime_type: str | None
    size_bytes: int
    duration_ms: int | None
    width: int | None
    height: int | None
    video_codec: str | None
    frame_rate_num: int | None
    frame_rate_den: int | None
    audio_codec: str | None
    sample_rate_hz: int | None
    audio_channels: int | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime


class AssetPageResponse(BaseModel):
    """Страница библиотеки с общим числом найденных активов."""

    items: list[AssetListItem]
    total: int
    limit: int
    offset: int


class AssetDetailResponse(AssetListItem):
    """Полная техническая карточка медиаактива."""

    checksum_algorithm: str | None
    checksum: str | None
    duplicate_of_asset_id: UUID | None
    probe_metadata: dict | None
    last_attempt_at: datetime | None
    locations: list[AssetLocationResponse]


class ShotFrameResponse(BaseModel):
    """Ссылка на производный кадр плана."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sequence: int
    role: str
    timestamp_ms: int
    selection_reason: str
    sampling_version: str
    sampling_settings: dict
    artifact_path: str
    thumbnail_path: str | None
    width: int
    height: int


class ShotDescriptionResponse(BaseModel):
    """Проверенное структурированное описание плана."""

    model_config = ConfigDict(from_attributes=True)

    status: DescriptionStatus
    summary: str | None
    visible_objects: list[str] | None
    stable_objects: list[str] | None
    action: str | None
    setting: str | None
    shot_type: str | None
    camera_motion: str | None
    object_motion: str | None
    visible_text: list[str] | None
    uncertainty: str | None
    frame_observations: list[dict] | None
    frame_transitions: list[dict] | None
    temporal_events: list[dict] | None
    field_evidence: list[dict] | None
    schema_version: str | None
    sampling_version: str | None
    sampling_settings: dict | None
    language: str
    provider: str | None
    model: str | None
    prompt_version: str | None
    error_code: str | None
    error_message: str | None


class ShotResponse(BaseModel):
    """План с временными границами и производными данными."""

    id: UUID
    sequence: int
    start_ms: int
    end_ms: int
    detector: str
    detector_version: str
    detector_settings: dict
    frames: list[ShotFrameResponse]
    description: ShotDescriptionResponse | None


class ShotListResponse(BaseModel):
    """Текущий набор планов медиаактива."""

    asset_id: UUID
    items: list[ShotResponse]
