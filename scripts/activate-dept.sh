#!/usr/bin/env bash
# =============================================================================
# activate-dept.sh - UX-5 activation flow entry point.
#
# Promotes a dept from `Ready to activate` to `Live` by:
#   1. Running activation.can_activate() to verify all preconditions
#   2. Building the activation PR body per Notion v5 lines 977-995
#   3. Opening the PR via the bubble-token-broker + gh CLI
#   4. Printing the operator-facing post-merge checklist
#
# Usage:
#   ./activate-dept.sh --slug=miranda [--repo-dir=/path] [--dry-run]
#
# --dry-run mode prints the PR body to stdout and exits 0 without opening
# a real PR. Use this for the console preview pane.
#
# Notion v5 references:
#   - lines 950-1003 — "Activation : du Ready vers Live"
#   - lines 977-995  — PR body structure
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: activate-dept.sh --slug=<slug> [--repo-dir=<path>] [--dry-run]
                        [--base-branch=<branch>] [--repo-url=<url>]

Synopsis:
  Activation flow for a fully-onboarded department. Refuses to run if
  can_activate() returns False (missing step / wrong status / missing
  dept.yaml / dry-run failure).

Arguments:
  --slug=<slug>             Department slug (e.g. miranda). Required.
  --repo-dir=<path>         Local path to the dept repo working tree.
                            Defaults to /tmp/bubble-ops-<slug>.
  --dry-run                 Build and print the PR body to stdout; do NOT
                            mint a token, do NOT open a PR. Exits 0 if
                            preconditions are met, 2 otherwise.
  --base-branch=<branch>    PR base branch (default: main).
  --repo-url=<url>          Full git URL of the dept repo (default:
                            https://github.com/vdk888/bubble-ops-<slug>).
  --broker=<path>           Path to bubble-token-broker (default:
                            /opt/bubble-token-broker/bin/bubble-token-broker).
  --guard=<path>            Path to bubble-git-guard (default:
                            /opt/bubble-git-guard/bin/bubble-git-guard).
  --help                    Show this message.

Exit codes:
  0    Success (PR opened, or --dry-run preview rendered)
  2    can_activate() returned False; blockers printed to stderr
  64   Bad CLI args
  1    Broker or gh PR creation failed

Example (preview):
  ./activate-dept.sh --slug=miranda --dry-run
USAGE
}

SLUG=""
REPO_DIR=""
BASE_BRANCH="main"
DRY_RUN=0
REPO_URL=""
BROKER_PATH="/opt/bubble-token-broker/bin/bubble-token-broker"
GUARD_PATH="/opt/bubble-git-guard/bin/bubble-git-guard"

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --repo-dir=*) REPO_DIR="${arg#*=}" ;;
    --base-branch=*) BASE_BRANCH="${arg#*=}" ;;
    --repo-url=*) REPO_URL="${arg#*=}" ;;
    --broker=*) BROKER_PATH="${arg#*=}" ;;
    --guard=*) GUARD_PATH="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$SLUG" ]] && { echo "ERROR: --slug required" >&2; usage >&2; exit 64; }

# Default --repo-dir to /tmp/bubble-ops-<slug> if not provided.
if [[ -z "$REPO_DIR" ]]; then
  REPO_DIR="/tmp/bubble-ops-$SLUG"
fi

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: repo-dir not found: $REPO_DIR" >&2
  echo "  Bootstrap the dept first (scripts/bootstrap-dept.sh --slug=$SLUG)" >&2
  exit 64
fi

# Default --repo-url.
if [[ -z "$REPO_URL" ]]; then
  REPO_URL="https://github.com/vdk888/bubble-ops-$SLUG"
fi

# Delegate to Python worker. Use the calling environment's interpreter
# (honors $PYTHON, e.g. set by a Python caller to sys.executable) so
# activate_runner.py's in-process dry-run import sees the same deps as the
# caller. On the VPS this stays "python3" (system interpreter already has
# the deps) — only dev/CI callers that export PYTHON change behavior.
exec "${PYTHON:-python3}" "$SCRIPT_DIR/lib/activate_runner.py" \
  --slug="$SLUG" \
  --repo-dir="$REPO_DIR" \
  --base-branch="$BASE_BRANCH" \
  --repo-url="$REPO_URL" \
  --broker-path="$BROKER_PATH" \
  --guard-path="$GUARD_PATH" \
  $([ "$DRY_RUN" = "1" ] && echo "--dry-run")
