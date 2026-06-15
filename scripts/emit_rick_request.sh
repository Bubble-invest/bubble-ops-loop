#!/usr/bin/env bash
# =============================================================================
# emit_rick_request.sh — standardized dept→Rick help request channel.
#
# Every dept agent calls this when it needs Rick (debugging, new tool, framework
# fix, skill change). Produces a durable YAML file in the dept's
# queues/management/ directory — the canonical record. Also attempts to emit a
# kanban card for live dashboard visibility (degrades gracefully when the
# dashboard is down).
#
# USAGE (from dept agent):
#   /home/claude/bubble-ops-loop/scripts/emit_rick_request.sh \
#     --dept=maya \
#     --title="Diagnose why warming-routing skips every 3rd lead" \
#     --body="Evidence: ... Proposed fix: ..." \
#     --priority=high
#
# YAML output path:
#   /home/claude/agents/bubble-ops-<dept>/queues/management/rick-<slug>-<YYYYMMDD>.yaml
#
# Idempotent: running twice with the same title on the same date does NOT create
# a duplicate file.
#
# Exit 0 always — emission should never fail the caller.
# =============================================================================
set -uo pipefail

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
DEPT=""
TITLE=""
BODY=""
PRIORITY="normal"

for arg in "$@"; do
  case "$arg" in
    --dept=*)      DEPT="${arg#--dept=}"         ;;
    --title=*)     TITLE="${arg#--title=}"       ;;
    --body=*)      BODY="${arg#--body=}"         ;;
    --priority=*)  PRIORITY="${arg#--priority=}" ;;
    --help|-h)
      cat <<'HELP'
Usage: emit_rick_request.sh --dept=<slug> --title="..." --body="..." [--priority=low|normal|high|urgent]

Required:
  --dept=<slug>       Department slug (maya, ben, claudette, tony, …)
  --title="..."       One-line summary of what you need
  --body="..."        Detailed prose with diagnosis, evidence, proposed fix

Optional:
  --priority=<level>  low | normal | high | urgent (default: normal)

Output:
  Prints the YAML path created and kanban emission status.
  YAML is the durable record; kanban is best-effort live dashboard visibility.
HELP
      exit 0
      ;;
    *) ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
if [ -z "$DEPT" ] || [ -z "$TITLE" ] || [ -z "$BODY" ]; then
  echo "emit_rick_request: ERROR: --dept, --title, and --body are required" >&2
  echo "Usage: emit_rick_request.sh --dept=<slug> --title=\"...\" --body=\"...\" [--priority=low|normal|high|urgent]" >&2
  exit 0
fi

# Validate priority enum
case "$PRIORITY" in
  low|normal|high|urgent) ;;
  *)
    echo "emit_rick_request: WARN: invalid priority '$PRIORITY', defaulting to 'normal'" >&2
    PRIORITY="normal"
    ;;
esac

# ---------------------------------------------------------------------------
# Compute paths
# ---------------------------------------------------------------------------
DEPT_DIR="/home/claude/agents/bubble-ops-${DEPT}"
QUEUE_DIR="${DEPT_DIR}/queues/management"

# Slugify the title for the filename: lowercase, spaces→dashes, strip non-alnum
SLUG="$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//' | head -c 60)"
TODAY="$(date +%Y%m%d)"
YAML_PATH="${QUEUE_DIR}/rick-${SLUG}-${TODAY}.yaml"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------------------
# Create dept directory if missing (e.g. legacy dept without queue dirs)
# ---------------------------------------------------------------------------
if [ ! -d "$QUEUE_DIR" ]; then
  mkdir -p "$QUEUE_DIR" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Idempotency: if a file matching rick-*-<TODAY>.yaml has the same title,
# don't create a duplicate. We check the `title:` field of any matching file.
# ---------------------------------------------------------------------------
for existing in "$QUEUE_DIR"/rick-*-"${TODAY}".yaml; do
  [ -f "$existing" ] || continue
  existing_title="$(grep -m1 '^title:' "$existing" 2>/dev/null | sed 's/^title: *//;s/^"//;s/"$//' | tr -d "'")" || true
  if [ "$existing_title" = "$TITLE" ]; then
    echo "emit_rick_request: SKIP — identical request already exists at $existing" >&2
    echo "yaml_path=$existing"
    echo "kanban_status=skipped_duplicate"
    exit 0
  fi
done

# ---------------------------------------------------------------------------
# Generate YAML
# ---------------------------------------------------------------------------
cat > "$YAML_PATH" <<YAML_EOF
id: rick-${SLUG}-${TODAY}
kind: management_note
audience: [rick, joris]
created_at: '${TIMESTAMP}'
created_by: ${DEPT}
title: "${TITLE}"
detail: >-
  ${BODY}
status: open
priority: ${PRIORITY}
YAML_EOF

echo "emit_rick_request: YAML written to $YAML_PATH" >&2
echo "yaml_path=$YAML_PATH"

# ---------------------------------------------------------------------------
# Kanban emission — best-effort, degrades gracefully when dashboard is down
# ---------------------------------------------------------------------------
EMIT_KANBAN="/home/claude/scripts/emit_kanban_item.sh"
KANBAN_STATUS="not_attempted"

if [ -x "$EMIT_KANBAN" ]; then
  # Build a compact kanban body (cap at 500 chars — kanban cards are glanceable)
  KANBAN_BODY="${BODY:0:500}"

  # Use the Mac Tailscale IP as KANBAN_HOST (Morty→Mac dashboard tunnel)
  if [ "$(hostname)" = "morty" ] || hostname | grep -q "hetzner"; then
    export KANBAN_HOST="${KANBAN_HOST:-100.75.151.47:3847}"
  fi

  if "$EMIT_KANBAN" \
       task="rick-request" \
       title="$TITLE" \
       body="[${DEPT}] ${KANBAN_BODY}" \
       type="incident" \
       priority="$PRIORITY" \
       owner="rick" \
       context_url="file://${YAML_PATH}" 2>/dev/null; then
    KANBAN_STATUS="emitted"
  else
    KANBAN_STATUS="queued_fallback"
  fi
else
  echo "emit_rick_request: WARN: emit_kanban_item.sh not found at $EMIT_KANBAN — kanban skipped" >&2
  KANBAN_STATUS="script_missing"
fi

echo "kanban_status=$KANBAN_STATUS"

exit 0
