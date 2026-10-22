#!/usr/bin/env bash
# Start Debate Studio on http://127.0.0.1:8000 (serves frontend/dist when built).
# DEMO=1 enables the offline FakeProvider (no API key, no billing).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r backend/requirements.txt
fi
export DEBATE_STUDIO_ENABLE_FAKE="${DEMO:-0}"
exec .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "${PORT:-8000}"
