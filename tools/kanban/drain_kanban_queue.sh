#!/usr/bin/env bash
# drain_kanban_queue.sh — replay stranded kanban cards to the GitHub board.
#
# WHY: emit_kanban_item.sh falls back to a local JSONL queue when both the
# GitHub issue path and the dashboard POST fail. Cards that land there never
# reach the board unless something replays them. This script is that replay.
#
# USAGE:
#   ~/claude-workspaces/Rick_RnD/tools/kanban/drain_kanban_queue.sh
#   KANBAN_QUEUE=/custom/path.jsonl drain_kanban_queue.sh   # override path
#   DRAIN_DRY_RUN=1 drain_kanban_queue.sh                   # dry-run, no gh calls
#
# Idempotence: processed lines are appended to <queue>.drained and removed
# from the live queue atomically (temp-file swap). A line is only archived
# after a SUCCESSFUL gh issue create — failed lines stay in the queue for the
# next run. Running the script multiple times is safe.
#
# Auth: requires the same GitHub auth as emit_kanban_item.sh (ambient `gh`
# auth or the bubble-board-token.sh minter). If no auth is available the
# script exits 1 loudly — do not silently swallow the failure.
#
# Exit codes:
#   0 — queue was empty, OR all lines drained successfully
#   1 — auth unavailable (no lines attempted)
#   2 — partial: some lines failed (remain in queue for next run)

set -uo pipefail

QUEUE="${KANBAN_QUEUE:-$HOME/claude-workspaces/Rick_RnD/monitoring/kanban_queue.jsonl}"
DRAINED="${QUEUE%.jsonl}.drained"
BOARD_REPO="Bubble-invest/bubble-ops-board"
DRY_RUN="${DRAIN_DRY_RUN:-0}"

# ── Auth resolution (same logic as emit_kanban_item.sh) ──────────────────────

_resolve_gh_token() {
  if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
    return 0
  fi
  local minter=/usr/local/bin/bubble-board-token.sh
  if command -v gh &>/dev/null && [ -x "$minter" ]; then
    local tok
    tok=$(sudo -n "$minter" 2>/dev/null || true)
    if [ -n "$tok" ]; then
      export GH_TOKEN="$tok"
      return 0
    fi
  fi
  return 1
}

# ── Main ─────────────────────────────────────────────────────────────────────

if [ ! -f "$QUEUE" ]; then
  echo "drain_kanban_queue: queue not found at $QUEUE — nothing to drain" >&2
  exit 0
fi

LINE_COUNT=$(wc -l < "$QUEUE" 2>/dev/null || echo 0)
if [ "$LINE_COUNT" -eq 0 ]; then
  echo "drain_kanban_queue: queue is empty — nothing to drain" >&2
  exit 0
fi

echo "drain_kanban_queue: found ${LINE_COUNT} line(s) in $QUEUE" >&2

if [ "$DRY_RUN" = "1" ]; then
  echo "drain_kanban_queue: DRY RUN — would attempt to replay ${LINE_COUNT} card(s):" >&2
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    title=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('kanban_items',[{}])[0].get('title','(unknown)'))" "$line" 2>/dev/null || echo "(parse error)")
    echo "  - ${title}" >&2
  done < "$QUEUE"
  echo "drain_kanban_queue: set DRAIN_DRY_RUN=0 (or unset) to drain for real" >&2
  exit 0
fi

if ! _resolve_gh_token; then
  echo "drain_kanban_queue: no GitHub auth available — cannot drain (set up gh auth or bubble-board-token.sh)" >&2
  exit 1
fi

FAILED=0
DRAINED_COUNT=0

# Process line by line; write a temp queue of lines that failed.
TMPQ=$(mktemp "${QUEUE}.tmp.XXXXXX")
trap 'rm -f "$TMPQ"' EXIT

while IFS= read -r line || [ -n "$line" ]; do
  [ -z "$line" ] && continue

  # Parse the payload JSON written by emit_kanban_item.sh's _dashboard_emit.
  # Shape: { "task": "...", "kanban_items": [{ "title": ..., "type": ..., ... }] }
  PARSED=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    item = d.get('kanban_items', [{}])[0]
    print(d.get('task', ''))
    print(item.get('title', '')[:200])
    print(item.get('type', 'incident'))
    print(item.get('priority', 'normal'))
    print(item.get('owner', ''))
    print(item.get('body', ''))
    print(item.get('context_url', '') or '')
    print(item.get('telegram_ref', '') or '')
except Exception as e:
    print('', '', 'incident', 'normal', '', '', '', '', sep='\n')
    sys.stderr.write('drain: parse error: ' + str(e) + '\n')
" "$line" 2>/dev/null) || true

  TASK=$(echo "$PARSED"     | sed -n '1p')
  TITLE=$(echo "$PARSED"    | sed -n '2p')
  TYPE=$(echo "$PARSED"     | sed -n '3p')
  PRIORITY=$(echo "$PARSED" | sed -n '4p')  # captured for future label mapping
  : "${PRIORITY}"  # suppress SC2034 — not used in gh call but kept for parity
  OWNER=$(echo "$PARSED"    | sed -n '5p')
  BODY=$(echo "$PARSED"     | sed -n '6p')
  CONTEXT_URL=$(echo "$PARSED" | sed -n '7p')

  if [ -z "$TITLE" ]; then
    echo "drain_kanban_queue: skipping unparseable line (archiving as failed): ${line:0:80}..." >&2
    echo "$line" >> "$TMPQ"
    FAILED=$((FAILED + 1))
    continue
  fi

  echo "drain_kanban_queue: replaying task=${TASK:-?} title=${TITLE:0:60}..." >&2

  # Idempotency: skip if an open issue already carries this task marker.
  if [ -n "$TASK" ]; then
    existing=$(gh issue list \
      --repo "$BOARD_REPO" \
      --state open \
      --search "\"emit-task: ${TASK}\" in:body" \
      --limit 1 \
      --json number \
      --jq '.[0].number' 2>/dev/null || true)
    if [ -n "$existing" ]; then
      echo "drain_kanban_queue: issue #${existing} already exists for task=${TASK} — archiving as drained" >&2
      echo "$line" >> "$DRAINED"
      DRAINED_COUNT=$((DRAINED_COUNT + 1))
      continue
    fi
  fi

  # Build a minimal issue body (task marker for idempotency; body if available).
  TMPBODY=$(mktemp /tmp/drain_kanban_body.XXXXXX)
  {
    echo "## Job"
    echo "$TITLE"
    echo ""
    if [ -n "$BODY" ]; then
      echo "$BODY"
      echo ""
    fi
    if [ -n "$CONTEXT_URL" ]; then
      echo "Context: $CONTEXT_URL"
    fi
    echo ""
    echo "*(replayed from local queue by drain_kanban_queue.sh)*"
    if [ -n "$TASK" ]; then
      echo ""
      echo "<!-- emit-task: ${TASK} -->"
    fi
  } > "$TMPBODY"

  # Map owner → dept label (mirrors emit_kanban_item.sh _gh_emit logic).
  dept_label=""
  owner_norm=$(echo "$OWNER" | tr '[:upper:]' '[:lower:]' | tr '-' '_')
  case "$owner_norm" in
    rnd|rick|rick_rnd)              dept_label="dept:rnd"        ;;
    ben)                            dept_label="dept:ben"        ;;
    maya)                           dept_label="dept:maya"       ;;
    tony|main|main_strategist|ricky) dept_label="dept:tony"     ;;
    content|miranda)                dept_label="dept:content"    ;;
    security|eliot)                 dept_label="dept:security"   ;;
    accountant|geraldine)           dept_label="dept:accountant" ;;
    morty)                          dept_label="dept:morty"      ;;
    claudette)                      dept_label="dept:claudette"  ;;
  esac

  type_label="type:chore"
  case "$TYPE" in
    incident)                   type_label="type:incident"   ;;
    findings|research)          type_label="type:research"   ;;
    approval|decision|feature)  type_label="type:feature"    ;;
    manual|chore)               type_label="type:chore"      ;;
    bug)                        type_label="type:bug"        ;;
    infra)                      type_label="type:infra"      ;;
    docs|documentation)         type_label="type:docs"       ;;
  esac

  label_args=()
  [ -n "$dept_label" ] && label_args+=("--label" "$dept_label")
  label_args+=("--label" "$type_label")
  label_args+=("--label" "status:triage")

  issue_url=$(gh issue create \
    --repo "$BOARD_REPO" \
    --title "${TITLE:0:200}" \
    --body-file "$TMPBODY" \
    "${label_args[@]}" 2>&1)
  gh_exit=$?
  rm -f "$TMPBODY"

  if [ $gh_exit -ne 0 ]; then
    echo "drain_kanban_queue: gh issue create FAILED for '${TITLE:0:60}': $issue_url" >&2
    echo "$line" >> "$TMPQ"
    FAILED=$((FAILED + 1))
  else
    echo "drain_kanban_queue: created $issue_url" >&2
    echo "$line" >> "$DRAINED"
    DRAINED_COUNT=$((DRAINED_COUNT + 1))
  fi

done < "$QUEUE"

# Atomically replace the queue with only the lines that failed.
if [ -s "$TMPQ" ]; then
  mv "$TMPQ" "$QUEUE"
else
  rm -f "$TMPQ" "$QUEUE"
fi
trap - EXIT

echo "drain_kanban_queue: done — drained=${DRAINED_COUNT}, failed=${FAILED}" >&2

if [ "$FAILED" -gt 0 ]; then
  exit 2
fi
exit 0
