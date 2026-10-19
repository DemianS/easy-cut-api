from uuid import uuid4

from tasks.asset_description import asset_description
from tasks.asset_embedding import asset_embedding
from tasks.asset_probe import asset_probe
from tasks.diagnostics import healthcheck
from tasks.library_scan import library_scan
from tasks.job_dispatcher import reconcile_background_jobs
from tasks.shot_detection import shot_detection
from tasks.base import DatabaseJobTask
from worker import build_broker_url, celery_app, create_celery_app


def test_broker_url_uses_escaped_password(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.setenv("REDIS_PASSWORD", "password:@")

    assert build_broker_url() == "redis://:password%3A%40@redis:6380/2"


def test_worker_registers_diagnostic_task(monkeypatch) -> None:
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    configured_app = create_celery_app()

    assert healthcheck.name in celery_app.tasks
    assert healthcheck.run() == {"status": "ok"}
    assert configured_app.conf.worker_concurrency == 1


def test_worker_registers_library_scan_task() -> None:
    assert library_scan.name in celery_app.tasks
    assert library_scan.name == "easy_cut.library_scan"
    assert reconcile_background_jobs.name in celery_app.tasks


def test_worker_registers_asset_probe_task() -> None:
    assert asset_probe.name in celery_app.tasks
    assert asset_probe.name == "easy_cut.asset_probe"


def test_worker_registers_shot_analysis_tasks() -> None:
    assert shot_detection.name in celery_app.tasks
    assert shot_detection.name == "easy_cut.shot_detection"
    assert asset_description.name in celery_app.tasks
    assert asset_description.name == "easy_cut.asset_description"
    assert asset_embedding.name in celery_app.tasks
    assert asset_embedding.name == "easy_cut.asset_embedding"


def test_database_jobs_use_common_failure_guard() -> None:
    guarded_tasks = (
        library_scan,
        asset_probe,
        shot_detection,
        asset_description,
        asset_embedding,
    )

    assert all(isinstance(task, DatabaseJobTask) for task in guarded_tasks)


def test_common_failure_guard_handles_error_before_service(monkeypatch) -> None:
    job_id = uuid4()
    recorded: dict[str, str | None] = {}

    async def record_failure(parsed_job_id, celery_task_id) -> bool:
        recorded["job_id"] = str(parsed_job_id)
        recorded["task_id"] = celery_task_id
        return True

    monkeypatch.setattr(
        "tasks.base._mark_failure_and_dispose",
        record_failure,
    )

    asset_probe.on_failure(
        RuntimeError("sensitive details must not be persisted"),
        "celery-task-id",
        (str(job_id),),
        {},
        None,
    )

    assert recorded == {
        "job_id": str(job_id),
        "task_id": "celery-task-id",
    }
