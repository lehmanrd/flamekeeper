# syntax=docker/dockerfile:1

# --- UI build -------------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Controller -----------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
COPY --from=ui /ui/dist ./app/static

ARG VERSION=dev
ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/srv/flamekeeper \
    FLAMEKEEPER_VERSION=$VERSION
LABEL org.opencontainers.image.title="FlameKeeper" \
      org.opencontainers.image.description="Enshrouded dedicated server manager" \
      org.opencontainers.image.version=$VERSION

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080' + os.environ.get('BASE_PATH','').rstrip('/') + '/api/health', timeout=4)"
# Runs as root: it needs the Docker socket and must chown world folders to PUID:PGID.
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
