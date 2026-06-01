#!/usr/bin/env bash
# =============================================================================
# retire-dept.sh - Sprint Lifecycle Deliverable B.
#
# Decommissions a Live department with dignity.
#
# Flow:
#   1. Pre-flight: STATE.yaml::status == Live (for non-Live use cancel-eclosion)
#   2. Send a final FR Bureau-de-Cadre Telegram message via the dept's bot
#   3. SSH to Morty: systemctl disable (WITHOUT --now — graceful)
#   4. Flip dept.yaml::department.status = retired + commit + push
#   5. Update STATE.yaml: status -> Retired + retired_at + retired_reason
#
# Usage:
#   ./retire-dept.sh --slug=miranda [--repo-dir=/path] [--reason=text] [--dry-run]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: retire-dept.sh --slug=<slug> [--repo-dir=<path>] [--reason=<text>] [--dry-run]

Synopsis:
  Decommissions a Live department. For pre-Live depts use cancel-eclosion.

Arguments:
  --slug=<slug>      Department slug. Required.
  --repo-dir=<path>  Local path to the dept repo working tree.
                     Defaults to $BUBBLE_BOOTSTRAP_CLONE_DIR/bubble-ops-<slug>
                     or /tmp/bubble-ops-<slug>.
  --reason=<text>    Free-form retirement reason. Recorded in STATE.yaml.
                     Default: "Decommissioned".
  --dry-run          Compute the plan + farewell message WITHOUT touching
                     Telegram / Morty / git / STATE.yaml.
  --help             Show this message.

Exit codes:
  0    Success (retired, or --dry-run plan rendered)
  2    Blocked (e.g. dept not Live -> operator must use cancel-eclosion)
  64   Bad CLI args
USAGE
}

SLUG=""
REPO_DIR=""
REASON="Decommissioned"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --repo-dir=*) REPO_DIR="${arg#*=}" ;;
    --reason=*) REASON="${arg#*=}" ;;
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

exec python3 "$SCRIPT_DIR/lib/retire_dept.py" \
  --slug="$SLUG" \
  --repo-dir="$REPO_DIR" \
  --reason="$REASON" \
  $([ "$DRY_RUN" = "1" ] && echo "--dry-run")
