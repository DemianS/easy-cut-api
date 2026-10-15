"""Точка входа FastAPI-приложения."""
import logging

from fastapi import FastAPI

from routers.assets import router as assets_router
from routers.jobs import router as jobs_router
from routers.search import router as search_router

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)
logger.info("API started")

app = FastAPI(
    title="Easy Cut API",
    version="0.1.0",
)
app.include_router(jobs_router)
app.include_router(assets_router)
app.include_router(search_router)


@app.get("/health", tags=["system"])
async def healthcheck() -> dict[str, str]:
    """Подтвердить, что процесс API принимает запросы."""
    return {"status": "ok"}
