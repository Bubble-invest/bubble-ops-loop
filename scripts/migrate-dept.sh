#!/usr/bin/env bash
# =============================================================================
# migrate-dept.sh — Sprint H+I Fix 7.
#
# Brownfield-dept migration: ingest an existing workspace (with config.yaml
# + CLAUDE.md) into a fresh bubble-ops-<slug>/ tree, with mandate pre-seeded
# and STATE.yaml advanced to status=Configuring + validated_steps=[mandate].
#
# Parallel to bootstrap-dept.sh (greenfield). Use this for Maya, Ben, and
# any other dept that already exists outside the bubble-ops-loop convention.
#
# Usage:
#   ./migrate-dept.sh --source=~/claude-workspaces/Maya_Sales \
#                      --slug=maya --display-name=Maya --owner=operator
#
# Test-hook env vars (same as bootstrap-dept.sh):
#   BUBBLE_BOOTSTRAP_CLONE_DIR — override the default /tmp clone parent dir
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: migrate-dept.sh --source=<path> --slug=<slug> --display-name=<name> --owner=<owner>

Synopsis:
  Migrate a brownfield dept workspace (existing config.yaml + CLAUDE.md)
  into a bubble-ops-<slug>/ tree compatible with the onboarding skill.

Arguments:
  --source=<path>         Path to the existing workspace (e.g.
                          ~/claude-workspaces/Maya_Sales). Must contain
                          config.yaml; CLAUDE.md optional but strongly recommended.
  --slug=<slug>           Kebab-case dept slug.
  --display-name=<name>   Display name (e.g. Maya).
  --owner=<owner>         Owner slug (e.g. operator).
  --help                  Show this message.

Test hooks (env vars):
  BUBBLE_BOOTSTRAP_CLONE_DIR  Parent dir for the new bubble-ops-<slug>/
                              tree (default: /tmp).

Behavior:
  - Refuses to clobber an existing target.
  - Fails clearly if --source doesn't exist or has no config.yaml.
  - Emits a mapping report: fields mapped + fields needing operator review.
USAGE
}

SOURCE=""
SLUG=""
DISPLAY_NAME=""
OWNER=""

for arg in "$@"; do
  case "$arg" in
    --source=*) SOURCE="${arg#*=}" ;;
    --slug=*) SLUG="${arg#*=}" ;;
    --display-name=*) DISPLAY_NAME="${arg#*=}" ;;
    --owner=*) OWNER="${arg#*=}" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "$SOURCE" ]]; then
  echo "ERROR: --source is required" >&2; usage >&2; exit 64
fi
if [[ -z "$SLUG" ]]; then
  echo "ERROR: --slug is required" >&2; usage >&2; exit 64
fi
if [[ ! "$SLUG" =~ ^[a-z][a-z0-9-]+$ ]]; then
  echo "ERROR: --slug '$SLUG' is not kebab-case (^[a-z][a-z0-9-]+$)" >&2
  exit 64
fi
if [[ -z "$DISPLAY_NAME" ]]; then
  echo "ERROR: --display-name is required" >&2; usage >&2; exit 64
fi
if [[ -z "$OWNER" ]]; then
  echo "ERROR: --owner is required" >&2; usage >&2; exit 64
fi

# Expand ~ in --source.
SOURCE="${SOURCE/#\~/$HOME}"

CLONE_PARENT="${BUBBLE_BOOTSTRAP_CLONE_DIR:-/tmp}"
TARGET="${CLONE_PARENT}/bubble-ops-${SLUG}"

# Pre-flight: bot handle length (same logic as bootstrap-dept.sh Fix 4).
SLUG_COMPACT="${SLUG//-/}"
BOT_HANDLE="bubbleops${SLUG_COMPACT}_bot"
HANDLE_LEN=${#BOT_HANDLE}
if (( HANDLE_LEN > 32 )); then
  cat >&2 <<EOF
ERROR: dept slug '$SLUG' produces a Telegram bot handle longer than 32 chars.

  Generated handle: @${BOT_HANDLE}
  Length:           ${HANDLE_LEN} (Telegram BotFather max = 32)

Pick a shorter slug (<= 19 chars sans dashes) and re-run.
EOF
  exit 64
fi

# Delegate to Python helper.
mkdir -p "$CLONE_PARENT"
python3 "$SCRIPT_DIR/lib/migrate.py" \
  --source="$SOURCE" \
  --target="$TARGET" \
  --slug="$SLUG" \
  --display-name="$DISPLAY_NAME" \
  --owner="$OWNER"
