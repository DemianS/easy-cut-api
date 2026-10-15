"""Схемы фоновых задач."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobResponse(BaseModel):
    """Публичное состояние фоновой задачи."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_type: str
    status: str
    media_root_id: UUID | None
    asset_id: UUID | None
    progress_current: int
    progress_total: int | None
    result: dict | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
