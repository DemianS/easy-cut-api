from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from dependencies import get_session
from main import app
from sql.enums import AssetKind, AssetStatus


class ScalarItems:
    def __init__(self, items: list) -> None:
        self.items = items

    def __iter__(self):
        return iter(self.items)


class AssetSession:
    def __init__(self, asset=None, locations=None) -> None:
        self.asset = asset
        self.locations = locations or []
        self.scalar_statements = []
        self.scalars_statements = []
        self.scalars_calls = 0
        self.asset_loaded_by_get = False

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        return 1 if self.asset is not None else 0

    async def scalars(self, statement):
        self.scalars_statements.append(statement)
        self.scalars_calls += 1
        if self.scalars_calls == 1 and not self.asset_loaded_by_get:
            return ScalarItems([self.asset] if self.asset is not None else [])
        return ScalarItems(self.locations)

    async def get(self, model, record_id):
        self.asset_loaded_by_get = True
        if self.asset is not None and self.asset.id == record_id:
            return self.asset
        return None


def _asset():
    current_time = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        status=AssetStatus.INDEXED,
        media_kind=AssetKind.VIDEO.value,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        mime_type="video/mp4",
        checksum_algorithm=None,
        checksum=None,
        duplicate_of_asset_id=None,
        size_bytes=1024,
        duration_ms=2500,
        width=1920,
        height=1080,
        video_codec="h264",
        frame_rate_num=25,
        frame_rate_den=1,
        audio_codec="aac",
        sample_rate_hz=48000,
        audio_channels=2,
        probe_metadata={"format": {"duration": "2.5"}},
        last_error_code=None,
        last_error_message=None,
        last_attempt_at=current_time,
        created_at=current_time,
        updated_at=current_time,
    )


def _location(asset_id: UUID):
    current_time = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        asset_id=asset_id,
        media_root_id=uuid4(),
        relative_path="folder/video.mp4",
        normalized_path="folder/video.mp4",
        is_available=True,
        size_bytes=1024,
        modified_at_ns=123456789,
        first_seen_at=current_time,
        last_seen_at=current_time,
        missing_since=None,
    )


def _session_dependency(session: AssetSession):
    async def dependency() -> AsyncIterator[AssetSession]:
        yield session

    return dependency


def test_list_assets_returns_paginated_technical_summary() -> None:
    asset = _asset()
    location = _location(asset.id)
    session = AssetSession(asset, [location])
    app.dependency_overrides[get_session] = _session_dependency(session)
    try:
        response = TestClient(app).get(
            "/assets",
            params={
                "status": "indexed",
                "media_kind": "video",
                "is_available": "true",
                "search": "video",
                "sort_by": "relative_path",
                "sort_order": "asc",
                "limit": 10,
                "offset": 0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 10
    assert payload["offset"] == 0
    assert payload["items"][0] == {
        "id": str(asset.id),
        "status": "indexed",
        "media_kind": "video",
        "relative_path": "folder/video.mp4",
        "is_available": True,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "mime_type": "video/mp4",
        "size_bytes": 1024,
        "duration_ms": 2500,
        "width": 1920,
        "height": 1080,
        "video_codec": "h264",
        "frame_rate_num": 25,
        "frame_rate_den": 1,
        "audio_codec": "aac",
        "sample_rate_hz": 48000,
        "audio_channels": 2,
        "last_error_code": None,
        "last_error_message": None,
        "created_at": asset.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": asset.updated_at.isoformat().replace("+00:00", "Z"),
    }
    count_query = str(session.scalar_statements[0])
    assert "assets.status" in count_query
    assert "asset_locations.is_available" in count_query
    assert "lower(asset_locations.relative_path) LIKE lower" in count_query


def test_get_asset_returns_probe_metadata_and_locations() -> None:
    asset = _asset()
    location = _location(asset.id)
    session = AssetSession(asset, [location])
    app.dependency_overrides[get_session] = _session_dependency(session)
    try:
        response = TestClient(app).get(f"/assets/{asset.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["probe_metadata"] == {"format": {"duration": "2.5"}}
    assert payload["locations"][0]["relative_path"] == "folder/video.mp4"
    assert payload["locations"][0]["media_root_id"] == str(location.media_root_id)


def test_get_asset_returns_404_for_unknown_asset() -> None:
    session = AssetSession()
    app.dependency_overrides[get_session] = _session_dependency(session)
    try:
        response = TestClient(app).get(f"/assets/{uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Медиаактив не найден"}


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("status", "unknown-status"),
        ("media_kind", "document"),
        ("sort_by", "checksum"),
        ("sort_order", "sideways"),
        ("limit", "101"),
        ("offset", "-1"),
    ],
)
def test_list_assets_rejects_invalid_query_parameters(
    parameter: str,
    value: str,
) -> None:
    response = TestClient(app).get("/assets", params={parameter: value})

    assert response.status_code == 422
