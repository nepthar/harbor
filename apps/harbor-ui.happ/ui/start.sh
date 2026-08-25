#!/bin/sh
# /venv is a temp volume. uv sync recreates it if the image Python no longer matches.
set -eu

uv sync --frozen --no-dev --project /app
exec /venv/bin/uvicorn server:app --host 0.0.0.0 --port "$PORT" --app-dir /app
