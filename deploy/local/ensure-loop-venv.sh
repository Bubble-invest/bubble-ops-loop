#!/usr/bin/env bash
# ensure-loop-venv.sh — idempotently (re)build the dedicated python venv the
# Mac loop tooling pins its interpreter to (board #1330).
#
# WHY: due_missions.py (and several scripts/lib/*.py dept.yaml / crons-manifest
# / dispatch-directive readers) import PyYAML at module scope. Bare `python3`
# on a Mac can resolve NON-DETERMINISTICALLY between an interpreter that has
# pyyaml and one that doesn't (e.g. homebrew python@3.14 vs. CommandLineTools
# python 3.9) — the import then crashes mid-tick, and a completion command
# that ERRORS looks identical to one that never ran (same class as #1235/
# #1316). Fix: give the loop ONE pinned, known-good interpreter that never
# depends on PATH lookup order — this script builds it.
#
# Run this once per Mac checkout (idempotent — safe to re-run; it skips the
# rebuild if the venv is already present and importing yaml). It is also
# called automatically by install-local-loop-backup.sh, so a normal
# (re)install keeps it current.
#
# Usage: deploy/local/ensure-loop-venv.sh [--force]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV="${LOCAL_LOOP_VENV:-$REPO_ROOT/.venv}"
REQ="$REPO_ROOT/scripts/requirements.txt"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

log() { echo "[ensure-loop-venv] $*"; }
die() { echo "[ensure-loop-venv] ERROR: $*" >&2; exit 1; }

[[ -f "$REQ" ]] || die "missing $REQ"

if [[ "$FORCE" != 1 && -x "$VENV/bin/python3" ]] \
    && "$VENV/bin/python3" -c 'import yaml' >/dev/null 2>&1; then
    log "$VENV already present and yaml-capable — skipping rebuild"
    exit 0
fi

BOOTSTRAP_PY="$(command -v python3 || command -v python)"
[[ -n "$BOOTSTRAP_PY" ]] || die "no python3/python on PATH to bootstrap the venv"

log "building $VENV from $REQ (bootstrap interpreter: $BOOTSTRAP_PY)"
"$BOOTSTRAP_PY" -m venv "$VENV" || die "python -m venv failed"
"$VENV/bin/python3" -m pip install --upgrade pip -q || die "pip upgrade failed"
"$VENV/bin/python3" -m pip install -q -r "$REQ" || die "pip install -r $REQ failed"

# Fail loud, not silent: an install that somehow doesn't leave yaml importable
# must not be treated as success — that would just move #1330's silent-failure
# class one step earlier.
"$VENV/bin/python3" -c 'import yaml' >/dev/null 2>&1 \
    || die "$VENV/bin/python3 still cannot import yaml after install — aborting"

log "$VENV ready ($("$VENV/bin/python3" --version 2>&1), pyyaml $("$VENV/bin/python3" -c 'import yaml; print(yaml.__version__)'))"
