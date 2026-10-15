"""Диагностические задачи worker."""

from worker import celery_app


@celery_app.task(name="easy_cut.worker.healthcheck")
def healthcheck() -> dict[str, str]:
    """Подтвердить, что worker загрузил и исполняет задачи."""
    return {"status": "ok"}
