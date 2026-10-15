"""Точка входа FastAPI-приложения."""

from fastapi import FastAPI

from routers.jobs import router as jobs_router

app = FastAPI(
    title="Easy Cut API",
    version="0.1.0",
)
app.include_router(jobs_router)


@app.get("/health", tags=["system"])
async def healthcheck() -> dict[str, str]:
    """Подтвердить, что процесс API принимает запросы."""
    return {"status": "ok"}
