"""Точка входа FastAPI-приложения."""

from time import monotonic
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
import logging

from routers.assets import router as assets_router
from routers.jobs import router as jobs_router
from routers.search import router as search_router
from routers.montages import router as montages_router
from services.logging_config import configure_logging, log_event, LogStatus
from services.logging_config import reset_request_id, set_request_id

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Easy Cut API",
    version="0.1.0",
)
app.include_router(jobs_router)
app.include_router(assets_router)
app.include_router(search_router)
app.include_router(montages_router)


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    """Записать безопасный итог HTTP-запроса без query и тела."""
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    request_id_token = set_request_id(request_id)
    started_at = monotonic()
    context = {
        "request_id": request_id,
        "http_method": request.method,
        "http_path": request.url.path,
    }
    log_event(
        logger,
        logging.DEBUG,
        "http_request",
        LogStatus.STARTED,
        "HTTP-запрос начат",
        stage="request",
        **context,
    )
    try:
        response = await call_next(request)
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "http_request",
            LogStatus.FAILED,
            "HTTP-запрос завершился необработанной ошибкой",
            stage="response",
            exc_info=True,
            error_type=type(error).__name__,
            **context,
        )
        reset_request_id(request_id_token)
        raise
    response.headers["X-Request-ID"] = request_id
    elapsed_ms = round((monotonic() - started_at) * 1000)
    response_status = (
        LogStatus.FAILED if response.status_code >= 400 else LogStatus.SUCCEEDED
    )
    log_event(
        logger,
        logging.WARNING if response.status_code >= 400 else logging.INFO,
        "http_request",
        response_status,
        "HTTP-запрос завершён",
        stage="response",
        http_status=response.status_code,
        elapsed_ms=elapsed_ms,
        **context,
    )
    reset_request_id(request_id_token)
    return response


@app.get("/health", tags=["system"])
async def healthcheck() -> dict[str, str]:
    """Подтвердить, что процесс API принимает запросы."""
    return {"status": "ok"}
