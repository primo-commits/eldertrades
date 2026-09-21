#!/usr/bin/env bash
# Mac / Linux equivalent of the .bat files.
#   ./run.sh install | check | practice | live | test
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-check}" in
  install)  python3 -m pip install -r requirements.txt ;;
  check)    python3 -m elder.preflight --scan ;;
  practice) python3 -m elder.runner ;;
  live)     python3 -m elder.runner --live ;;
  test)     python3 -m tests.test_pipeline && python3 -m tests.test_reconciler ;;
  *)        echo "usage: ./run.sh [install|check|practice|live|test]" ;;
esac
