# =============================================================================
# QwenPaw Maritime image
#
# Maritime (https://maritime.sh) builds the Dockerfile at the repo root with
# no build args, so this is a self-contained variant of deploy/Dockerfile:
#   - public base images (Docker Hub / GHCR, no private registry needed)
#   - persistent state under /data (survives restarts and sleep/wake)
#   - adapter on 0.0.0.0:$PORT speaking the Maritime contract
#     (GET /health, POST /chat), QwenPaw app on loopback only
#   - no console frontend build: the web console is unreachable behind the
#     adapter anyway, and skipping the vite build keeps the image buildable
#     on builders with limited memory (the app degrades gracefully without it)
# =============================================================================

# Alias for uv binary so COPY --from can reference it.
FROM ghcr.io/astral-sh/uv:latest AS uv-src

# -----------------------------------------------------------------------------
# Runtime image with Python, Chromium, desktop, and app.
# -----------------------------------------------------------------------------
FROM node:slim

# ENV variables
ENV NODE_ENV=production
ENV WORKSPACE_DIR=/app
# Maritime persists /data across restarts, redeploys, and sleep/wake.
# Keep every piece of mutable state there.
ENV QWENPAW_WORKING_DIR=/data/working
ENV QWENPAW_SECRET_DIR=/data/working.secret
ENV QWENPAW_BACKUP_DIR=/data/working.backups
ENV HF_HOME=/data/cache/huggingface
ENV MODELSCOPE_CACHE=/data/cache/modelscope

# Channel filtering: use QWENPAW_DISABLED_CHANNELS (exclusion, recommended)
# or QWENPAW_ENABLED_CHANNELS (whitelist). Override at runtime with -e.
# Note: sleeping agents cannot hold channel connections open; prefer
# Maritime channels (Telegram etc.) or the always-on add-on for these.
ENV QWENPAW_DISABLED_CHANNELS="imessage"

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --fix-missing \
    ca-certificates \
    curl  \
    python3  \
    python3-pip  \
    python3-venv \
    build-essential  \
    libssl-dev  \
    git  \
    supervisor  \
    vim  \
    gettext-base \
    xfce4 \
    xfce4-terminal \
    xvfb \
    dbus-x11 \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get clean


RUN apt-get update && apt-get install -y --fix-missing \
    chromium \
    chromium-sandbox \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxtst6 \
    libnss3 \
    libglib2.0-0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    fonts-liberation \
    libu2f-udev \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get clean


RUN sed -i 's/^CHROMIUM_FLAGS=""/CHROMIUM_FLAGS="--no-sandbox"/' /usr/bin/chromium

# Playwright: use system Chromium (already installed above).
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
# Avoid Playwright downloading its own browser when executable_path is used.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
# Indicate running in container (used e.g. for Chromium --no-sandbox).
ENV QWENPAW_RUNNING_IN_CONTAINER=1

WORKDIR ${WORKSPACE_DIR}

# Keep Debian's Python for system services; use the desktop runtime for QwenPaw.
COPY scripts/pack-tauri/stage_python_runtime.py /tmp/stage_python_runtime.py
RUN python3 /tmp/stage_python_runtime.py \
    --dest /opt/qwenpaw-python --python-version 3.11 \
  && /opt/qwenpaw-python/python/bin/python3 -m venv /app/venv \
  && rm /tmp/stage_python_runtime.py
ENV PATH="/app/venv/bin:$PATH"

COPY pyproject.toml setup.py README.md ./
COPY src ./src
COPY plugins/hub ./plugins/hub
COPY packages/qwenpawmail-mcp/src/qwenpawmail_mcp ./packages/qwenpawmail-mcp/src/qwenpawmail_mcp
COPY website/public/docs/ ./src/qwenpaw/docs/
# Speed up Python package installation with uv.
COPY --from=uv-src /uv /bin/uv
# Pin setuptools <82: lark-oapi still calls pkg_resources.declare_namespace
# at import time; setuptools >= 82 removes pkg_resources and breaks it.
RUN uv pip install --python /app/venv/bin/python \
    --no-cache-dir . "setuptools<82" && rm -rf ./build

# Maritime adapter (GET /health, POST /chat -> QwenPaw loopback API),
# first-boot provisioning, and the process supervisor config.
COPY deploy/maritime/adapter.py /app/maritime/adapter.py
COPY deploy/maritime/bootstrap.py /app/maritime/bootstrap.py
COPY deploy/maritime/supervisord.conf /app/maritime/supervisord.conf
COPY deploy/maritime/entrypoint.sh /app/maritime/entrypoint.sh
RUN chmod 755 /app/maritime/entrypoint.sh

# Maritime injects PORT at runtime (currently 18789); the adapter binds it.
CMD ["/app/maritime/entrypoint.sh"]
