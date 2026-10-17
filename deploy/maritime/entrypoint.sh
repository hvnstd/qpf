#!/bin/sh
# Maritime entrypoint: provision on first boot, then start all services.
set -e

echo "[entrypoint] QwenPaw Maritime image starting (PORT=${PORT:-8080})"
echo "[entrypoint] data dir: ${QWENPAW_WORKING_DIR:-/data/working}"

# First-boot provisioning: qwenpaw init + model configuration from env.
# Never aborts boot; failures surface via /health staying unready.
/app/venv/bin/python /app/maritime/bootstrap.py || \
  echo "[entrypoint] WARNING: bootstrap failed (see above); continuing"

exec /usr/bin/supervisord -c /app/maritime/supervisord.conf
