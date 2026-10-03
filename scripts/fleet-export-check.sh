#!/usr/bin/env bash
# Root coordinator runs only framework code, with Python environment isolated.
set -euo pipefail
FRAMEWORK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON_BIN:-python3}" -I "$FRAMEWORK_ROOT/scripts/lib/fleet_export_check.py" "$@"
