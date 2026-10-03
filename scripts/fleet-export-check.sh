#!/usr/bin/env bash
# Root coordinator runs only framework code, with Python environment isolated.
set -euo pipefail
# Imports must never leave bytecode owned by this maintenance UID in dept trees.
export PYTHONDONTWRITEBYTECODE=1
FRAMEWORK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON_BIN:-python3}" -I -B "$FRAMEWORK_ROOT/scripts/lib/fleet_export_check.py" "$@"
