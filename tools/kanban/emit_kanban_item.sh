#!/usr/bin/env bash
# emit_kanban_item.sh — push a single action-required item to the GitHub board kanban.
#
# Primary backend: create a GitHub issue on Bubble-invest/bubble-ops-board via `gh issue create`.
# Fallback: POST to the local dashboard (localhost:3847) + local queue file (migration safety net).
#
# USAGE (from a SKILL.md):
#
#   ~/claude-workspaces/Rick_RnD/tools/kanban/emit_kanban_item.sh \
#     task=rnd-ceo-inbox \
#     title="Stripe key 27 days old — rotate or accept risk" \
#     body="Optional longer context, max 2000 chars" \
#     type=decision \
#     priority=high \
#     owner=rnd \
#     actions=accept,reject,escalate \
#     context_url=https://wiki/... \
#     telegram_ref="https://t.me/c/123/456"
#
# Required args: task, title.
# Optional: body, type (approval|decision|incident|findings|manual|bug|feature|infra|docs|chore|research),
#           priority (normal|high|urgent), owner, actions (comma-separated), context_url, telegram_ref,
#           diagram_mermaid (Mermaid source for decision diagrams, ≤3000 chars),
#           visual_attachments (comma-separated repo-relative paths to images).
#
# GitHub labels applied automatically:
#   dept:<owner>  — if owner is a known dept (rnd/ben/maya/tony/content/security/accountant/morty/claudette)
#   type:<t>      — mapped from the type= arg
#   status:triage — default routing label (approval/decision types also add needs:human instead)
#
# Idempotency: if an open issue already contains <!-- emit-task: <task> -->, no duplicate is created.
#
# Exit 0 always — emission must never fail the cron.

set -uo pipefail

TASK=""
TITLE=""
BODY=""
TYPE="incident"
PRIORITY="normal"
OWNER=""
ACTIONS=""
CONTEXT_URL=""
TELEGRAM_REF=""
DIAGRAM_MERMAID=""
VISUAL_ATTACHMENTS=""

for arg in "$@"; do
  case "$arg" in
    task=*)         TASK="${arg#task=}"         ;;
    title=*)        TITLE="${arg#title=}"       ;;
    body=*)         BODY="${arg#body=}"         ;;
    type=*)         TYPE="${arg#type=}"         ;;
    priority=*)     PRIORITY="${arg#priority=}" ;;
    owner=*)        OWNER="${arg#owner=}"       ;;
    actions=*)      ACTIONS="${arg#actions=}"   ;;
    context_url=*)        CONTEXT_URL="${arg#context_url=}" ;;
    telegram_ref=*)       TELEGRAM_REF="${arg#telegram_ref=}" ;;
    diagram_mermaid=*)    DIAGRAM_MERMAID="${arg#diagram_mermaid=}" ;;
    visual_attachments=*) VISUAL_ATTACHMENTS="${arg#visual_attachments=}" ;;
    *) ;;
  esac
done

if [ -z "$TASK" ] || [ -z "$TITLE" ]; then
  echo "emit_kanban_item: task= and title= are required" >&2
  exit 0
fi

BOARD_REPO="Bubble-invest/bubble-ops-board"

# ── GitHub issue path ─────────────────────────────────────────────────────────

_gh_emit() {
  # Normalize owner → dept label
  local dept_label=""
  local owner_norm
  owner_norm=$(echo "$OWNER" | tr '[:upper:]' '[:lower:]' | tr '-' '_')
  case "$owner_norm" in
    rnd|rick|rick_rnd)          dept_label="dept:rnd"        ;;
    ben)                        dept_label="dept:ben"        ;;
    maya)                       dept_label="dept:maya"       ;;
    tony|main|main_strategist|ricky) dept_label="dept:tony"  ;;
    content|miranda)            dept_label="dept:content"    ;;
    security|eliot)             dept_label="dept:security"   ;;
    accountant|geraldine|géraldine) dept_label="dept:accountant" ;;
    morty)                      dept_label="dept:morty"      ;;
    claudette)                  dept_label="dept:claudette"  ;;
    *)                          dept_label=""                ;;
  esac

  # Map type → type label
  local type_label=""
  case "$TYPE" in
    incident)                   type_label="type:incident"   ;;
    findings|research)          type_label="type:research"   ;;
    approval|decision|feature)  type_label="type:feature"    ;;
    manual|chore)               type_label="type:chore"      ;;
    bug)                        type_label="type:bug"        ;;
    infra)                      type_label="type:infra"      ;;
    docs|documentation)         type_label="type:docs"       ;;
    *)                          type_label="type:chore"      ;;
  esac

  # Routing label
  local routing_label="status:triage"
  case "$TYPE" in
    approval|decision)          routing_label="needs:human"  ;;
  esac

  # Idempotency check: look for an open issue with the task marker
  local marker="emit-task: ${TASK}"
  local existing
  existing=$(gh issue list \
    --repo "$BOARD_REPO" \
    --state open \
    --search "\"${marker}\" in:body" \
    --limit 1 \
    --json number \
    --jq '.[0].number' 2>/dev/null || true)

  if [ -n "$existing" ]; then
    echo "emit_kanban_item: open issue #${existing} already exists for task=${TASK} — skipping duplicate" >&2
    return 0
  fi

  # Build issue body and title via python3 (robust escaping, same approach as original)
  local tmpfile
  tmpfile=$(mktemp /tmp/emit_kanban_body.XXXXXX)

  TASK="$TASK" TITLE="$TITLE" BODY="$BODY" TYPE="$TYPE" PRIORITY="$PRIORITY" \
  OWNER="$OWNER" ACTIONS="$ACTIONS" CONTEXT_URL="$CONTEXT_URL" TELEGRAM_REF="$TELEGRAM_REF" \
  DIAGRAM_MERMAID="$DIAGRAM_MERMAID" VISUAL_ATTACHMENTS="$VISUAL_ATTACHMENTS" \
  python3 -c "
import os

task              = os.environ['TASK']
title             = os.environ['TITLE'][:200]
body              = os.environ['BODY']
context_url       = os.environ['CONTEXT_URL']
actions           = os.environ['ACTIONS']
telegram_ref      = os.environ['TELEGRAM_REF']
diagram_mermaid   = os.environ.get('DIAGRAM_MERMAID', '')
visual_attach_raw = os.environ.get('VISUAL_ATTACHMENTS', '')

lines = []
lines.append('## Job')
lines.append(title)
lines.append('')
lines.append('## Inputs')
lines.append(context_url if context_url else '(n/a)')
lines.append('')
lines.append('## Allowed')
lines.append('(to be scoped by the Manager on triage)')
lines.append('')
lines.append('## Forbidden')
lines.append('(to be scoped by the Manager on triage)')
lines.append('')
lines.append('## Output')
lines.append('(to be scoped by the Manager on triage)')
lines.append('')
lines.append('## Evaluation')
lines.append('(to be scoped by the Manager on triage)')
lines.append('')
lines.append('---')

if body:
    lines.append(body[:2000])
    lines.append('')

# ── Visual planning fields (B2 — ROUND2) ──────────────────────────
if diagram_mermaid and diagram_mermaid.strip():
    lines.append('')
    lines.append('## Diagram')
    lines.append('\`\`\`mermaid')
    lines.append(diagram_mermaid.strip()[:3000])
    lines.append('\`\`\`')

if visual_attach_raw and visual_attach_raw.strip():
    paths = [p.strip() for p in visual_attach_raw.split(',') if p.strip()]
    if paths:
        lines.append('')
        lines.append('## Visual Attachments')
        for p in paths:
            lines.append('- ' + p)

if actions:
    action_list = ', '.join(a.strip() for a in actions.split(',') if a.strip())
    if action_list:
        lines.append('Suggested actions: ' + action_list)

if telegram_ref:
    lines.append('Telegram ref: ' + telegram_ref)

lines.append('')
lines.append('<!-- emit-task: ' + task + ' -->')

print('\n'.join(lines))
" > "$tmpfile"

  if [ ! -s "$tmpfile" ]; then
    echo "emit_kanban_item: failed to build issue body" >&2
    rm -f "$tmpfile"
    return 1
  fi

  # Assemble --label flags
  local label_args=()
  [ -n "$dept_label"    ] && label_args+=("--label" "$dept_label")
  [ -n "$type_label"    ] && label_args+=("--label" "$type_label")
  [ -n "$routing_label" ] && label_args+=("--label" "$routing_label")

  local issue_url
  issue_url=$(gh issue create \
    --repo "$BOARD_REPO" \
    --title "${TITLE:0:200}" \
    --body-file "$tmpfile" \
    "${label_args[@]}" 2>&1)

  local gh_exit=$?
  rm -f "$tmpfile"

  if [ $gh_exit -ne 0 ]; then
    echo "emit_kanban_item: gh issue create failed: $issue_url" >&2
    return 1
  fi

  echo "emit_kanban_item: created GitHub issue $issue_url" >&2
  return 0
}

# ── Fallback: dashboard POST + queue ─────────────────────────────────────────

_dashboard_emit() {
  local PAYLOAD
  PAYLOAD=$(TASK="$TASK" TITLE="$TITLE" BODY="$BODY" TYPE="$TYPE" PRIORITY="$PRIORITY" \
            OWNER="$OWNER" ACTIONS="$ACTIONS" CONTEXT_URL="$CONTEXT_URL" TELEGRAM_REF="$TELEGRAM_REF" \
            python3 -c "
import json, os
actions = [a.strip() for a in os.environ['ACTIONS'].split(',') if a.strip()]
item = {
    'title': os.environ['TITLE'][:200],
    'body': os.environ['BODY'][:2000] if os.environ['BODY'] else '',
    'type': os.environ['TYPE'],
    'priority': os.environ['PRIORITY'],
    'owner': os.environ['OWNER'] or None,
    'actions': actions,
    'context_url': os.environ['CONTEXT_URL'] or None,
    'telegram_ref': os.environ['TELEGRAM_REF'] or None,
}
item = {k: v for k, v in item.items() if v not in (None, '', [])}
payload = {
    'task': os.environ['TASK'],
    'severity': 'kanban_only',
    'message': '(kanban-only emit) ' + os.environ['TITLE'][:140],
    'steps': [],
    'kanban_items': [item],
}
print(json.dumps(payload))
" 2>/dev/null)

  if [ -z "$PAYLOAD" ]; then
    echo "emit_kanban_item: failed to build dashboard payload" >&2
    return 1
  fi

  KANBAN_HOST="${KANBAN_HOST:-localhost:3847}"
  HTTP=$(curl -s -m 5 -o /dev/null -w "%{http_code}" -X POST "http://${KANBAN_HOST}/api/monitor-event" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" 2>/dev/null)

  if [ "$HTTP" != "200" ]; then
    QUEUE="${KANBAN_QUEUE:-$HOME/claude-workspaces/Rick_RnD/monitoring/kanban_queue.jsonl}"
    mkdir -p "$(dirname "$QUEUE")" 2>/dev/null || true
    echo "$PAYLOAD" >> "$QUEUE"
    echo "emit_kanban_item: dashboard at ${KANBAN_HOST} returned $HTTP, item queued at $QUEUE" >&2
  fi
}

# ── Resolve a GitHub token for the board ──────────────────────────────────────
# Two auth paths, in order:
#   1. An already-authenticated `gh` (the Mac dev case: `gh auth status` passes).
#   2. The VPS path: the `claude` user can't run `gh auth login` and must NOT hold
#      the App private key, so it mints a SHORT-LIVED, issues:write-only board token
#      via the root-owned minter exposed through a tight sudoers NOPASSWD rule
#      (/usr/local/bin/bubble-board-token.sh). We export it as GH_TOKEN so the
#      `gh issue` calls in _gh_emit authenticate. Min-scope: create board issues only.
# If neither yields auth, we fall through to the dashboard (graceful, never breaks).
_resolve_gh_token() {
  if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
    return 0  # gh already authed — _gh_emit uses ambient auth
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
  return 1  # no usable GitHub auth
}

# ── Main: try GitHub first, fall back to dashboard ────────────────────────────

GH_OK=0
if _resolve_gh_token; then
  if _gh_emit; then
    GH_OK=1
  fi
fi

if [ "$GH_OK" -eq 0 ]; then
  echo "emit_kanban_item: gh unavailable or failed — falling back to dashboard" >&2
  _dashboard_emit
fi

exit 0
