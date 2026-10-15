FROM ghcr.io/astral-sh/uv:0.10.3 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY sql ./sql
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --frozen --no-dev --no-editable

EXPOSE 8000

CMD ["uvicorn", "easy_cut_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

