#!/usr/bin/env bash
# Elder Trades launcher (Linux/macOS).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] && source .venv/bin/activate
mkdir -p logs
exec python -m elder.runner "$@"
