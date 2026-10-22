#!/usr/bin/env bash
# Linux / macOS terminal launcher. DEMO=1 enables the offline FakeProvider.
set -euo pipefail
cd "$(dirname "$0")/.."
args=("$@")
if [ "${DEMO:-0}" = "1" ]; then args+=("--demo"); fi
exec python3 run.py "${args[@]}"
