from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi.testclient import TestClient

from main import app
from routers.jobs import get_session


class MissingRecordsSession:
    async def get(self, model, record_id):
        return None


async def missing_records_session() -> AsyncIterator[MissingRecordsSession]:
    yield MissingRecordsSession()


def test_start_library_scan_returns_404_for_unknown_root() -> None:
    app.dependency_overrides[get_session] = missing_records_session
    try:
        response = TestClient(app).post(f"/media-roots/{uuid4()}/scan")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Корень медиатеки не найден"}


def test_get_job_returns_404_for_unknown_job() -> None:
    app.dependency_overrides[get_session] = missing_records_session
    try:
        response = TestClient(app).get(f"/jobs/{uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Задача не найдена"}
