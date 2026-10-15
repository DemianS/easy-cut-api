FROM ghcr.io/astral-sh/uv:0.10.3 AS uv

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY . .

RUN uv sync --frozen --no-dev --no-editable


FROM base AS worker

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system easycut \
    && useradd --system --gid easycut --home-dir /app --no-create-home easycut

USER easycut

CMD ["celery", "-A", "worker:celery_app", "worker", "--loglevel=INFO"]


FROM base AS api

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

