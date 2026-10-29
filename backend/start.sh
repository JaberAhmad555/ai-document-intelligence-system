#!/usr/bin/env bash
set -euo pipefail

python -m alembic upgrade head

python -m app.workers \
  --processor app.services.invoice_pipeline:process_document \
  --poll-seconds 2 &
worker_pid=$!

python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" &
api_pid=$!

cleanup() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

wait -n "$api_pid" "$worker_pid"
