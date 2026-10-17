# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.21 AS uv

FROM python:3.12.15-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.15-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app config ./config
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_CONFIG=/app/config/gateway.yaml LITELLM_LOCAL_MODEL_COST_MAP=True \
    GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8103
USER app
EXPOSE 8103
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8103/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "gateway.app"]
