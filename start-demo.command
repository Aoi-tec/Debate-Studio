#!/bin/bash
# Debate Studio - demo mode (no API key, no billing)
exec "$(dirname "$0")/start.command" --demo "$@"
