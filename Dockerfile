FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY uv.lock* ./

RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project

FROM python:3.12-slim AS worker

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system easycut \
    && useradd --system --gid easycut --home-dir /app --no-create-home easycut

RUN groupadd --system user \
    && useradd --system --gid user --home-dir /app --shell /usr/sbin/nologin user

COPY --from=builder --chown=user:user /opt/venv /opt/venv
COPY --chown=user:user . .

USER user:user

CMD ["celery", "-A", "worker:celery_app", "worker", "--loglevel=INFO"]

FROM python:3.12-slim AS api

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

WORKDIR /app

RUN groupadd --system user \
    && useradd --system --gid user --home-dir /app --shell /usr/sbin/nologin user

COPY --from=builder --chown=user:user /opt/venv /opt/venv
COPY --chown=user:user . .

RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/logs \
    && chown -R user:user /app/logs

EXPOSE 8000

USER user:user

ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM worker AS test

USER root
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --group dev
CMD ["pytest"]
