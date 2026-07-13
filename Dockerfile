# syntax=docker/dockerfile:1
# Hugging Face Space / Docker — grok-free-register
# Hardware: 2 vCPU + 16 GB RAM recommended (Chromium Turnstile)
#
# Space settings: SDK=Docker, PORT is injected (default 7860)

ARG PYTHON_VERSION=3.11-bookworm

# ========== Stage 1: Python deps ==========
FROM python:${PYTHON_VERSION} AS pydeps
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Core deps are pinned inline so HF Space builds work even if requirements.txt
# is missing from a partial Space git tree.
RUN pip install --upgrade pip wheel \
    && pip install \
        'cloakbrowser>=0.3.0' \
        'requests>=2.31.0' \
        'PySocks>=1.7.1' \
        'python-dotenv>=1.0.0' \
        'httpx>=0.28' \
        'playwright>=1.55' \
        'curl_cffi>=0.6'
# Optional: vendor tree (CF-Ares / turnstile extras) when present in build context
COPY vendor /build/vendor
RUN if [ -f /build/vendor/CF-Ares/pyproject.toml ] || [ -f /build/vendor/CF-Ares/setup.py ]; then \
      pip install /build/vendor/CF-Ares || true; \
    fi \
    && if [ -f /build/vendor/turnstile-solver/requirements.txt ]; then \
      pip install -r /build/vendor/turnstile-solver/requirements.txt || true; \
    fi

# ========== Stage 2a: Go natives ==========
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src
COPY native/proxy-worker /src/proxy-worker
COPY native/register-worker /src/register-worker
COPY native/solver-gateway /src/solver-gateway
RUN mkdir -p /out \
    && (cd /src/proxy-worker && go build -o /out/proxy-worker .) \
    && (cd /src/register-worker && go build -o /out/register-worker .) \
    && (cd /src/solver-gateway && go build -o /out/solver-gateway .)

# ========== Stage 2b: Rust natives ==========
FROM rust:1-bookworm AS rustbuild
WORKDIR /src
COPY native/inventory-worker /src/inventory-worker
COPY native/solver-watchdog /src/solver-watchdog
RUN mkdir -p /out \
    && (cd /src/inventory-worker && cargo build --release && cp target/release/inventory-worker /out/) \
    && (cd /src/solver-watchdog && cargo build --release && cp target/release/solver-watchdog /out/)

# ========== Stage 2c: C++ util ==========
FROM debian:bookworm-slim AS cppbuild
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /out
WORKDIR /src
COPY native/solver-util /src/solver-util
RUN g++ -O2 -std=c++17 -o /out/solver-util /src/solver-util/solver_util.cpp

# ========== Stage 3: Runtime ==========
FROM python:${PYTHON_VERSION}
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    DASHBOARD_PORT=7860 \
    REGISTER_ENGINE=protocol \
    TURNSTILE_SOLVER=hybrid \
    TURNSTILE_SOLVER_ON_DEMAND=1 \
    TURNSTILE_API_URL=http://127.0.0.1:5080 \
    TURNSTILE_SOLVER_HEADLESS=1 \
    TURNSTILE_SOLVER_THREADS=2 \
    GO_REGISTER_WORKERS=4 \
    CONTROL_PLANE_ALLOW_ACTIONS=1 \
    KEY_EXPORT_DIR=/data/keys \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PROJECT_ROOT=/app \
    SOLVER_PYTHON=python

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=pydeps /usr/local/bin /usr/local/bin

WORKDIR /app
COPY grok_register /app/grok_register
COPY xai_enroller /app/xai_enroller
COPY native/solver-hybrid /app/native/solver-hybrid
COPY scripts /app/scripts
COPY vendor /app/vendor
COPY docker/entrypoint.sh /entrypoint.sh
# Keep a minimal requirements.txt in the image for tooling / docs (deps already installed)
RUN printf '%s\n' \
      'cloakbrowser>=0.3.0' \
      'requests>=2.31.0' \
      'PySocks>=1.7.1' \
      'python-dotenv>=1.0.0' \
      'httpx>=0.28' \
      'playwright>=1.55' \
      'curl_cffi>=0.6' \
      > /app/requirements.txt

RUN mkdir -p \
      /app/native/proxy-worker \
      /app/native/register-worker \
      /app/native/solver-gateway \
      /app/native/inventory-worker \
      /app/native/solver-watchdog \
      /app/native/solver-util \
      /data/keys /data/logs /app/logs \
    && ln -sfn /data/keys /app/keys \
    && ln -sfn /data/logs /app/logs

COPY --from=gobuild /out/proxy-worker /app/native/proxy-worker/proxy-worker
COPY --from=gobuild /out/register-worker /app/native/register-worker/register-worker
COPY --from=gobuild /out/solver-gateway /app/native/solver-gateway/solver-gateway
COPY --from=rustbuild /out/inventory-worker /app/native/inventory-worker/inventory-worker
COPY --from=rustbuild /out/solver-watchdog /app/native/solver-watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/native/solver-util/solver-util

RUN chmod +x /entrypoint.sh \
      /app/native/proxy-worker/proxy-worker \
      /app/native/register-worker/register-worker \
      /app/native/solver-gateway/solver-gateway \
      /app/native/inventory-worker/inventory-worker \
      /app/native/solver-watchdog/solver-watchdog \
      /app/native/solver-util/solver-util \
    && python -m playwright install chromium \
    && python -m playwright install-deps chromium || true

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
