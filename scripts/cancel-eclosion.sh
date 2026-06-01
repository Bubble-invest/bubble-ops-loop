#!/usr/bin/env bash
# =============================================================================
# cancel-eclosion.sh - Sprint Lifecycle Deliverable A.
#
# Abandons an in-flight eclosure for a dept that NEVER reached `Live`.
# Distinct from retire-dept (which decommissions a Live dept).
#
# Flow:
#   1. Pre-flight: dept exists, STATE.yaml::status != Live
#   2. SSH to Morty: systemctl disable --now ops-loop-<slug>.service
#      + remove unit file (mocked in tests)
#   3. gh repo archive vdk888/bubble-ops-<slug> (non-destructive)
#   4. Update STATE.yaml: status -> Cancelled + cancelled_at
#   5. Print BotFather operator instructions (cannot automate)
#
# Usage:
#   ./cancel-eclosion.sh --slug=miranda [--repo-dir=/path] [--dry-run]
#
# --dry-run computes the plan and prints BotFather instructions WITHOUT
# touching Morty, GitHub, or STATE.yaml. Use this for the console preview.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: cancel-eclosion.sh --slug=<slug> [--repo-dir=<path>] [--dry-run]

Synopsis:
  Cancels an in-flight eclosure (pre-Live only). For Live depts use
  retire-dept.sh instead.

Arguments:
  --slug=<slug>      Department slug. Required.
  --repo-dir=<path>  Local path to the dept repo working tree.
                     Defaults to $BUBBLE_BOOTSTRAP_CLONE_DIR/bubble-ops-<slug>
                     or /tmp/bubble-ops-<slug>.
  --dry-run          Compute the plan + print BotFather instructions
                     WITHOUT touching Morty / GitHub / STATE.yaml.
  --help             Show this message.

Exit codes:
  0    Success (cancelled, or --dry-run plan rendered)
  2    Blocked (e.g. dept is Live -> operator must use retire-dept)
  64   Bad CLI args
USAGE
}

SLUG=""
REPO_DIR=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --repo-dir=*) REPO_DIR="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$SLUG" ]] && { echo "ERROR: --slug required" >&2; usage >&2; exit 64; }

if [[ -z "$REPO_DIR" ]]; then
  if [[ -n "${BUBBLE_BOOTSTRAP_CLONE_DIR:-}" ]]; then
    REPO_DIR="${BUBBLE_BOOTSTRAP_CLONE_DIR}/bubble-ops-${SLUG}"
  else
    REPO_DIR="/tmp/bubble-ops-${SLUG}"
  fi
fi

exec python3 "$SCRIPT_DIR/lib/cancel_eclosion.py" \
  --slug="$SLUG" \
  --repo-dir="$REPO_DIR" \
  $([ "$DRY_RUN" = "1" ] && echo "--dry-run")
