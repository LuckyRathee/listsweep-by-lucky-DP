#!/bin/sh
# Start the verifier locally with the settings in .env (not committed).
cd "$(dirname "$0")" || exit 1
set -a
[ -f .env ] && . ./.env
set +a
exec ../lead-email-verifier-venv/bin/uvicorn verifier.api:app --host 127.0.0.1 --port "${PORT:-8080}"
