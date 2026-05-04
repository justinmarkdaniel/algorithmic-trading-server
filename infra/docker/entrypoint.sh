#!/bin/sh
set -e

# Optional: source secrets from a file the orchestrator drops at /tmp/ssm.env
# (e.g. AWS SSM Parameter Store, HashiCorp Vault, Doppler, etc.). If absent,
# we fall back to whatever env vars the container was started with — that's
# the local-dev path.
if [ -f /tmp/ssm.env ]; then
    . /tmp/ssm.env
fi

mkdir -p "${BOT_LOG_DIR:-/app/logs}"

exec uvicorn server.api.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info \
    --no-access-log
