"""Точка входа FastAPI-приложения."""

from fastapi import FastAPI


app = FastAPI(
    title="Easy Cut API",
    version="0.1.0",
)


@app.get("/health", tags=["system"])
async def healthcheck() -> dict[str, str]:
    """Подтвердить, что процесс API принимает запросы."""
    return {"status": "ok"}
