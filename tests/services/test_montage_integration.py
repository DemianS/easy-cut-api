"""Сквозная проверка API/БД/pgvector/FFmpeg с управляемыми ответами LLM."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text, update

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1" or not shutil.which("ffmpeg"),
    reason="Требуются PostgreSQL с миграциями и FFmpeg (Docker test target)",
)


def test_prompt_to_downloadable_mp4(tmp_path, monkeypatch):
    asyncio.run(_scenario(tmp_path, monkeypatch))


def test_manual_selection_and_version_bound_render(tmp_path, monkeypatch):
    asyncio.run(_scenario(tmp_path, monkeypatch, manual=True))


@pytest.mark.real_gateway
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_OLLAMA") != "1", reason="Опциональная локальная LLM-приёмка"
)
def test_prompt_to_downloadable_mp4_live(tmp_path, monkeypatch):
    asyncio.run(_scenario(tmp_path, monkeypatch, live=True))


async def _scenario(tmp_path: Path, monkeypatch, live=False, manual=False):
    from main import app
    from services.asset_embedding import TEXT_VERSION
    from services.montage_workflow import advance_montages, execute_montage_stage
    from sql.database import engine, session_scope
    from sql.enums import AssetStatus, DescriptionStatus, EmbeddingStatus
    from sql.models import (
        Asset,
        AssetLocation,
        Job,
        MediaRoot,
        MontageRender,
        MontageRun,
        Project,
    )
    from sql.models import Shot, ShotDescription, ShotEmbedding, TimelineVersion, User

    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("MEDIA_ROOT_PATH", str(media))
    monkeypatch.setenv("ARTIFACTS_PATH", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLANNER_PROVIDER", "ollama")
    if not live:
        monkeypatch.setenv("OLLAMA_PLANNER_MODEL", "fixture-planner")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    if not live:
        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "fixture-embedding")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    vector = [1.0] + [0.0] * 767
    if live:
        from services.embedding_provider import (
            TextEmbeddingProvider,
            get_embedding_settings,
        )

        vector = await asyncio.to_thread(
            TextEmbeddingProvider(get_embedding_settings()).embed,
            "Цветная тестовая таблица, геометрические узоры",
        )
    else:
        monkeypatch.setattr(
            "services.embedding_provider.TextEmbeddingProvider.embed",
            lambda *args: vector,
        )

    def generate(self, **kwargs):
        prompt = kwargs["prompt"]
        context = json.loads(prompt.split("ВХОДНЫЕ ДАННЫЕ JSON:\n")[1])
        if "candidates" not in context:
            result = {
                "sections": [
                    {
                        "section_id": "intro",
                        "role": "вступление",
                        "query": "общий вид",
                        "duration_ms": 1000,
                    },
                    {
                        "section_id": "detail",
                        "role": "деталь",
                        "query": "инструменты",
                        "duration_ms": 1000,
                    },
                ]
            }
        else:
            choices = []
            used = set()
            for section in context["plan"]["sections"]:
                candidate = next(
                    item
                    for item in context["candidates"]
                    if item["section_id"] == section["section_id"]
                    and item["asset_id"] not in used
                )
                used.add(candidate["asset_id"])
                choices.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "duration_ms": 1000,
                        "reason": section["role"],
                    }
                )
            result = {"clips": choices}
        return {"response": json.dumps(result), "model": "fixture-planner"}

    if not live:
        monkeypatch.setattr(
            "services.montage_planner.OllamaProvider.generate", generate
        )
    user_id, root_id = uuid4(), uuid4()
    asset_ids = [uuid4(), uuid4()]
    run_ids, parent_ids, project_ids = [], [], []
    try:
        async with session_scope() as session:
            session.add(User(id=user_id, username=f"montage-{user_id.hex}"))
            await session.flush()
            session.add(
                MediaRoot(
                    id=root_id,
                    name="montage-test",
                    container_path=str(media),
                    created_by_user_id=user_id,
                )
            )
            await session.flush()
            for index, asset_id in enumerate(asset_ids):
                path = media / f"source-{index}.mp4"
                command = [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=size={320 if index == 0 else 480}x240:"
                    f"rate={24 if index == 0 else 30}",
                ]
                if index:
                    command += [
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=44100",
                    ]
                command += ["-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
                if index:
                    command += ["-c:a", "aac"]
                subprocess.run(command + [str(path)], check=True, capture_output=True)
                stat = path.stat()
                session.add(
                    Asset(
                        id=asset_id,
                        media_kind="video",
                        status=AssetStatus.READY,
                        size_bytes=stat.st_size,
                        duration_ms=3000,
                        width=320 if index == 0 else 480,
                        height=240,
                        audio_codec="aac" if index else None,
                    )
                )
                await session.flush()
                session.add(
                    AssetLocation(
                        asset_id=asset_id,
                        media_root_id=root_id,
                        relative_path=path.name,
                        normalized_path=path.name,
                        size_bytes=stat.st_size,
                        modified_at_ns=stat.st_mtime_ns,
                        first_seen_at=datetime.now(UTC),
                        last_seen_at=datetime.now(UTC),
                    )
                )
                shot = Shot(
                    id=uuid4(),
                    asset_id=asset_id,
                    sequence=0,
                    start_ms=0,
                    end_ms=3000,
                    detector="fixture",
                    detector_version="1",
                )
                session.add(shot)
                await session.flush()
                description = ShotDescription(
                    id=uuid4(),
                    shot_id=shot.id,
                    status=DescriptionStatus.SUCCEEDED,
                    summary="Цветная тестовая таблица, геометрические узоры",
                )
                session.add(description)
                await session.flush()
                session.add(
                    ShotEmbedding(
                        shot_description_id=description.id,
                        status=EmbeddingStatus.SUCCEEDED,
                        provider="ollama",
                        model=os.getenv("OLLAMA_EMBEDDING_MODEL"),
                        text_version=TEXT_VERSION,
                        dimension=768,
                        indexed_text=description.summary,
                        embedding=vector,
                    )
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "prompt": "Собери ровно два клипа по одной секунде: сначала общий вид "
                "цветной тестовой таблицы, затем геометрические узоры.",
                "media_root_id": str(root_id),
                "target_duration_ms": 2000,
                "min_similarity": -1,
                "asset_ids": [str(value) for value in asset_ids],
                "auto_render": not manual,
            }
            headers = {"Idempotency-Key": f"test-{uuid4()}"}
            response = await client.post(
                "/api/v1/montages", json=payload, headers=headers
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["id"]
            run_ids.append(run_id)
            parent_ids.append(response.json()["job_id"])
            duplicate = await client.post(
                "/api/v1/montages", json=payload, headers=headers
            )
            assert duplicate.json() == response.json()
            conflict = await client.post(
                "/api/v1/montages",
                json={**payload, "prompt": "Другой"},
                headers=headers,
            )
            assert conflict.status_code == 409
            for _ in range(5):
                dispatches = await advance_montages()
                for dispatch in dispatches:
                    if str(dispatch.job_id) in parent_ids:
                        continue
                    async with session_scope() as session:
                        child = await session.get(Job, dispatch.job_id)
                        if str(child.parent_job_id) not in parent_ids:
                            continue
                    await execute_montage_stage(dispatch.job_id, dispatch.task_id)
                    # Повторная доставка переиспользует сохранённый результат.
                    await execute_montage_stage(dispatch.job_id, dispatch.task_id)
                    # Следующая стадия восстанавливает всё из БД в новом соединении.
                    await engine.dispose()
            state = (await client.get(f"/api/v1/montages/{run_id}")).json()
            project_ids.append(state["project_id"])
            assert state["job"]["status"] == "succeeded", [
                (item["error_code"], item["error_message"]) for item in state["stages"]
            ]
            assert len(state["timeline"]["clips"]) == 2
            assert len(state["candidates"]) >= 2
            if manual:
                state = await _exercise_manual_review(
                    client, run_id, state, monkeypatch
                )
            result = await client.get(state["download_url"])
            assert result.status_code == 200
            assert result.headers["content-type"] == "video/mp4"
            assert len(result.content) > 1000
            # Запрос вне библиотеки не подмешивает даже семантически близкие активы.
            empty = await client.post(
                "/api/v1/montages", json={**payload, "asset_ids": [str(uuid4())]}
            )
            empty_id = empty.json()["id"]
            run_ids.append(empty_id)
            parent_ids.append(empty.json()["job_id"])
            for _ in range(3):
                for dispatch in await advance_montages():
                    async with session_scope() as session:
                        child = await session.get(Job, dispatch.job_id)
                        if str(child.parent_job_id) not in parent_ids:
                            continue
                    await execute_montage_stage(dispatch.job_id, dispatch.task_id)
            state = (await client.get(f"/api/v1/montages/{empty_id}")).json()
            project_ids.append(state["project_id"])
            assert state["job"]["status"] == "failed"
            assert state["job"]["error_code"] == "insufficient_material"
            assert state["download_url"] is None
            if not live and not manual:
                broken = await client.post("/api/v1/montages", json=payload)
                broken_id = broken.json()["id"]
                run_ids.append(broken_id)
                parent_ids.append(broken.json()["job_id"])
                for _ in range(3):
                    for dispatch in await advance_montages():
                        async with session_scope() as session:
                            child = await session.get(Job, dispatch.job_id)
                            if str(child.parent_job_id) != broken.json()["job_id"]:
                                continue
                        await execute_montage_stage(dispatch.job_id, dispatch.task_id)
                # Меняем только синтетические исходники собственной тестовой папки.
                for source in media.glob("*.mp4"):
                    source.unlink()
                for dispatch in await advance_montages():
                    async with session_scope() as session:
                        child = await session.get(Job, dispatch.job_id)
                        if str(child.parent_job_id) != broken.json()["job_id"]:
                            continue
                    await execute_montage_stage(dispatch.job_id, dispatch.task_id)
                await advance_montages()
                state = (await client.get(f"/api/v1/montages/{broken_id}")).json()
                assert state["job"]["status"] == "failed"
                assert state["download_url"] is None
    finally:
        async with session_scope() as session:
            runs = (
                list(
                    await session.scalars(
                        select(MontageRun).where(
                            MontageRun.job_id.in_(
                                [UUID(value) for value in parent_ids]
                            ),
                        )
                    )
                )
                if parent_ids
                else []
            )
            for run in runs:
                await session.execute(
                    delete(MontageRender).where(
                        MontageRender.montage_run_id == run.id,
                    )
                )
                await session.execute(
                    update(MontageRun)
                    .where(
                        MontageRun.id == run.id,
                    )
                    .values(current_timeline_version_id=None)
                )
                await session.execute(
                    delete(TimelineVersion).where(
                        TimelineVersion.montage_run_id == run.id
                    )
                )
            await session.execute(
                delete(MontageRun).where(MontageRun.id.in_([run.id for run in runs]))
            )
            await session.execute(
                delete(Job).where(Job.parent_job_id.in_([run.job_id for run in runs]))
            )
            await session.execute(
                delete(Job).where(Job.id.in_([run.job_id for run in runs]))
            )
            await session.execute(
                delete(Project).where(Project.id.in_([run.project_id for run in runs]))
            )
            await session.execute(delete(Asset).where(Asset.id.in_(asset_ids)))
            await session.execute(delete(MediaRoot).where(MediaRoot.id == root_id))
            await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()


async def _exercise_manual_review(client, run_id, state, monkeypatch):
    from services.montage_workflow import advance_montages, execute_montage_stage
    from services.job_dispatcher import reconcile_jobs
    from sql.database import engine, session_scope
    from sql.models import Job

    assert state["state"] == "awaiting_selection"
    assert state["renders"] == []
    assert state["download_url"] is None
    for _ in range(2):
        await advance_montages()
    assert (await client.get(f"/api/v1/montages/{run_id}")).json()["renders"] == []
    base_url = f"/api/v1/montages/{run_id}"
    base_id = state["timeline_version_id"]
    original_timeline = state["timeline"]
    dispatched = []
    monkeypatch.setattr(
        "routers.montages.celery_app.send_task",
        lambda *args, **kwargs: dispatched.append((args, kwargs)),
    )
    first = await client.post(
        base_url + "/renders", json={"timeline_version_id": base_id}
    )
    assert first.status_code == 202, first.text
    first_render = first.json()
    duplicate = await client.post(
        base_url + "/renders", json={"timeline_version_id": base_id}
    )
    assert duplicate.json()["id"] == first_render["id"]
    assert len(dispatched) == 1
    assert dispatched[0][1]["queue"] == "user_render"
    assert (await client.get(base_url)).json()["state"] == "rendering"

    # Выбираем альтернативы вместо предложенных моделью и меняем порядок.
    original_ids = {clip["candidate_id"] for clip in state["selection"]["clips"]}
    alternatives = [
        item for item in state["candidates"] if item["candidate_id"] not in original_ids
    ]
    clips = [
        {"candidate_id": item["candidate_id"], "duration_ms": 600}
        for item in reversed(alternatives[:2])
    ]
    body = {"base_version_id": base_id, "clips": clips}
    concurrent = await asyncio.gather(
        client.patch(base_url + "/selection", json=body),
        client.patch(base_url + "/selection", json=body),
    )
    assert sorted(response.status_code for response in concurrent) == [200, 409]
    changed = next(response for response in concurrent if response.status_code == 200)
    assert changed.status_code == 200, changed.text
    version_id = changed.json()["timeline_version_id"]
    assert version_id != base_id
    assert changed.json()["timeline"]["duration_ms"] == 1200
    assert (
        changed.json()["timeline"]["clips"][0]["candidate_id"]
        == clips[0]["candidate_id"]
    )
    assert (await client.patch(base_url + "/selection", json=body)).status_code == 409
    invalid = await client.patch(
        base_url + "/selection",
        json={
            "base_version_id": version_id,
            "clips": [{"candidate_id": str(uuid4()), "duration_ms": 1000}],
        },
    )
    assert invalid.status_code == 422
    invalid = await client.patch(
        base_url + "/selection",
        json={
            "base_version_id": version_id,
            "clips": [clips[0], clips[0]],
        },
    )
    assert invalid.status_code == 422
    assert (
        await client.post(
            base_url + "/renders",
            json={
                "timeline_version_id": str(uuid4()),
            },
        )
    ).status_code == 404
    versions = (await client.get(base_url + "/versions")).json()
    assert len(versions["items"]) == 2
    assert (
        next(item for item in versions["items"] if item["id"] == base_id)["timeline"]
        == original_timeline
    )
    assert (
        next(item for item in versions["items"] if item["id"] == version_id)["source"]
        == "user"
    )

    with monkeypatch.context() as bounded:
        bounded.setattr("services.montage_review.MAX_ACTIVE_RENDERS", 1)
        full = await client.post(
            base_url + "/renders", json={"timeline_version_id": version_id}
        )
        assert full.status_code == 429
    assert len(dispatched) == 1

    # Ранее запрошенный рендер привязан к A, хотя текущая версия уже B.
    async with engine.begin() as lock_connection:
        await lock_connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int.from_bytes(UUID(base_id).bytes[:8], "big", signed=True),
            },
        )
        blocked = await execute_montage_stage(
            UUID(first_render["job_id"]),
            dispatched[0][1]["task_id"],
        )
        assert blocked == {"reused": True}
    await execute_montage_stage(
        UUID(first_render["job_id"]), dispatched[0][1]["task_id"]
    )
    result = (await client.get(base_url + f"/renders/{first_render['id']}")).json()
    assert result["job"]["status"] == "succeeded", result
    assert result["timeline_version_id"] == base_id
    assert result["job"]["result"]["timeline_version_id"] == base_id
    assert (await client.get(result["download_url"])).status_code == 200
    state = (await client.get(base_url)).json()
    assert state["state"] == "awaiting_selection"
    assert state["timeline_version_id"] == version_id
    assert state["download_url"] is None
    assert (await client.get(base_url + "/video")).status_code == 409

    def unavailable_broker(*args, **kwargs):
        dispatched.append((args, kwargs))
        raise ConnectionError("test broker unavailable")

    with monkeypatch.context() as unavailable:
        unavailable.setattr("routers.montages.celery_app.send_task", unavailable_broker)
        current = await client.post(
            base_url + "/renders", json={"timeline_version_id": version_id}
        )
    assert current.status_code == 202, current.text
    current_render = current.json()
    async with session_scope() as session:
        pending = await session.get(Job, UUID(current_render["job_id"]))
        assert pending.error_code == "queue_unavailable"
        pending.last_dispatched_at = datetime.now(UTC) - timedelta(seconds=1000)
    recovered = next(
        item
        for item in await reconcile_jobs()
        if str(item.job_id) == current_render["job_id"]
    )
    assert recovered.queue == "user_render"
    stale = await execute_montage_stage(
        recovered.job_id,
        dispatched[-1][1]["task_id"],
    )
    assert stale == {"stale_delivery": True}
    await execute_montage_stage(recovered.job_id, recovered.task_id)
    state = (await client.get(base_url)).json()
    assert state["state"] == "completed", state
    assert state["timeline_version_id"] == version_id
    duplicate = await client.post(
        base_url + "/renders", json={"timeline_version_id": version_id}
    )
    assert duplicate.json()["id"] == current_render["id"]
    assert len(dispatched) == 2
    return state
