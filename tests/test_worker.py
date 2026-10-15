from tasks.asset_description import asset_description
from tasks.asset_probe import asset_probe
from tasks.diagnostics import healthcheck
from tasks.library_scan import library_scan
from tasks.shot_detection import shot_detection
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


def test_worker_registers_asset_probe_task() -> None:
    assert asset_probe.name in celery_app.tasks
    assert asset_probe.name == "easy_cut.asset_probe"


def test_worker_registers_shot_analysis_tasks() -> None:
    assert shot_detection.name in celery_app.tasks
    assert shot_detection.name == "easy_cut.shot_detection"
    assert asset_description.name in celery_app.tasks
    assert asset_description.name == "easy_cut.asset_description"
