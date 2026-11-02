# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app config ./config
# Fail-safe defaults: the image is PROD unless told otherwise (prod refuses chaos, local providers, file
# spend ledger and plaintext Redis). docker-compose sets ENV=dev and the dev config explicitly.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ENV=prod GATEWAY_CONFIG=/app/config/gateway.prod.yaml LITELLM_LOCAL_MODEL_COST_MAP=True \
    GATEWAY_HOST=0.0.0.0
USER app
EXPOSE 8103
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; p=os.environ.get('GATEWAY_PORT') or os.environ.get('PORT') or '8103'; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "gateway.app"]
