#!/usr/bin/env bash
# =============================================================================
# validate-step.sh - UX-2 Component C: validate an onboarding step's artifact,
# commit it on the onboarding/<slug> branch, and update STATE.yaml.
#
# Called by the UX-1 skill OR directly by the operator. Pure local; no network
# (except `git push` which is best-effort).
#
# Usage:
#   ./validate-step.sh --slug=miranda --step=mandate --repo-dir=/tmp/bubble-ops-miranda
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: validate-step.sh --slug=<slug> --step=<step> --repo-dir=<path> [--validated-by=<who>]

Synopsis:
  Validates the artifact produced for one onboarding step, then commits the
  relevant files on branch onboarding/<slug> and updates onboarding/STATE.yaml.

Arguments:
  --slug=<slug>         Department slug (e.g. miranda).
  --step=<step>         One of: mandate | missions | layers | skills_tools |
                        gates_kpis | dry_run.
  --repo-dir=<path>     Local path to the dept repo (the git working tree).
  --validated-by=<who>  Operator slug (default: $USER or "operator").
  --help                Show this message.

Exit codes:
  0   success: artifact valid + committed + STATE.yaml updated.
  1   validation failed: artifact does not match schema (no commit, no
      STATE.yaml update).
  64  bad usage.

Example:
  ./validate-step.sh --slug=miranda --step=mandate --repo-dir=/tmp/bubble-ops-miranda

USAGE
}

SLUG=""
STEP=""
REPO_DIR=""
VALIDATED_BY="${USER:-operator}"

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --step=*) STEP="${arg#*=}" ;;
    --repo-dir=*) REPO_DIR="${arg#*=}" ;;
    --validated-by=*) VALIDATED_BY="${arg#*=}" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$SLUG" ]] && { echo "ERROR: --slug required" >&2; usage >&2; exit 64; }
[[ -z "$STEP" ]] && { echo "ERROR: --step required" >&2; usage >&2; exit 64; }
[[ -z "$REPO_DIR" ]] && { echo "ERROR: --repo-dir required" >&2; usage >&2; exit 64; }
[[ ! -d "$REPO_DIR" ]] && { echo "ERROR: repo-dir not found: $REPO_DIR" >&2; exit 64; }

# Delegate the heavy lifting to Python.
exec python3 "$SCRIPT_DIR/lib/validate_step_runner.py" \
  --slug="$SLUG" \
  --step="$STEP" \
  --repo-dir="$REPO_DIR" \
  --validated-by="$VALIDATED_BY"
