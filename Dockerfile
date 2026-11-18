# Multi-stage Dockerfile for app_inspector_service
#
# Single-context build: w2v-common is resolved from pypiserver.test
# via [tool.uv.sources] pointing at the pypiserver-internal index.
# Build with: docker buildx build -f Dockerfile .

ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /uvx /bin/

# ============================================================================
# Stage 1: Builder
# ============================================================================
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    curl \
    ca-certificates \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock README.md ./

# Resolve and install deps; w2v-common comes from pypiserver.test
RUN uv sync --frozen --no-dev --no-install-project

# Now copy source and install the project itself
COPY src/ ./src/
COPY alembic.ini ./alembic.ini

RUN uv sync --frozen --no-dev

# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS production

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 inspectorservice && \
    mkdir -p /app /app/logs /app/data && \
    chown -R inspectorservice:inspectorservice /app

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=inspectorservice:inspectorservice /app/.venv /app/.venv

# Copy application code
COPY --chown=inspectorservice:inspectorservice src/ ./src/
COPY --chown=inspectorservice:inspectorservice alembic.ini ./alembic.ini

ENV PATH="/app/.venv/bin:$PATH"

USER 1000:1000

# Port 6311 (App Inspector Service)
EXPOSE 6311

# Kubernetes livenessProbe / readinessProbe in clusters/strato/services/w2v/
# are the source of truth for health. A Dockerfile HEALTHCHECK is dead
# weight under k8s and only causes drift if the probe paths change.

CMD ["python", "-m", "src.asgi"]
