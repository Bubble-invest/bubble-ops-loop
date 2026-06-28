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
PROJ=""
DUE=""
HOST=""
LINKS=""

for arg in "$@"; do
  case "$arg" in
    task=*)         TASK="${arg#task=}"         ;;
    title=*)        TITLE="${arg#title=}"       ;;
    body=*)         BODY="${arg#body=}"         ;;
    type=*)         TYPE="${arg#type=}"         ;;
    priority=*)     PRIORITY="${arg#priority=}" ;;
    owner=*)        OWNER="${arg#owner=}"       ;;
    proj=*)         PROJ="${arg#proj=}"         ;;
    due=*)          DUE="${arg#due=}"           ;;
    budget=*)       BUDGET="${arg#budget=}"     ;;
    host=*)         HOST="${arg#host=}"         ;;
    links=*)        LINKS="${arg#links=}"       ;;
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
  # host default per owner (overridable by explicit host=). Tonio = the LOCAL
  # Tony (main-strategist) — @ClaudeRickyBot, runs rnd_loop on the Mac — so it
  # routes to dept:tony + host:local. VPS depts default host:vps.
  local host_default=""
  case "$owner_norm" in
    rnd|rick|rick_rnd)          dept_label="dept:rnd"        ;;
    ben)                        dept_label="dept:ben"; host_default="vps"   ;;
    maya)                       dept_label="dept:maya"; host_default="vps"  ;;
    tonio)                      dept_label="dept:tony"; host_default="local" ;;
    tony|main|main_strategist|ricky) dept_label="dept:tony"; host_default="vps" ;;
    content|miranda)            dept_label="dept:content"; host_default="local" ;;
    security|eliot)             dept_label="dept:security"   ;;
    accountant|geraldine|géraldine) dept_label="dept:accountant"; host_default="vps" ;;
    morty)                      dept_label="dept:morty"; host_default="vps" ;;
    claudette)                  dept_label="dept:claudette"; host_default="local" ;;
    *)                          dept_label=""                ;;
  esac
  # explicit host= wins; else the owner default
  local host_norm
  host_norm=$(echo "${HOST:-$host_default}" | tr '[:upper:]' '[:lower:]')
  local host_label=""
  case "$host_norm" in
    local|mac)  host_label="host:local" ;;
    vps|remote) host_label="host:vps"   ;;
    *)          host_label=""           ;;
  esac

  # proj → proj:<slug> label (free-form slug, lowercased; the cockpit map decides display)
  local proj_label=""
  if [ -n "$PROJ" ]; then
    local proj_norm
    proj_norm=$(echo "$PROJ" | tr '[:upper:]' '[:lower:]' | tr ' _' '--' | sed 's/^proj://')
    proj_label="proj:${proj_norm}"
  fi

  # due → due:YYYY-MM-DD label (validate the shape; ignore if malformed)
  local due_label=""
  if [ -n "$DUE" ]; then
    if echo "$DUE" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
      due_label="due:${DUE}"
    else
      echo "emit_kanban_item: due=${DUE} is not YYYY-MM-DD — ignoring" >&2
    fi
  fi

  # budget → budget:$N label (real-$ budget for this card, cache-excluded; a tweakable
  # constraint + an importance signal — board #358 v3). Accept a bare integer (dollars).
  local budget_label=""
  if [ -n "$BUDGET" ]; then
    # strip a leading $ if present, accept integer
    local _b="${BUDGET#\$}"
    if echo "$_b" | grep -qE '^[0-9]+$'; then
      budget_label="budget:\$${_b}"
    else
      echo "emit_kanban_item: budget=${BUDGET} is not an integer dollar amount — ignoring" >&2
    fi
  fi

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
  DIAGRAM_MERMAID="$DIAGRAM_MERMAID" VISUAL_ATTACHMENTS="$VISUAL_ATTACHMENTS" LINKS="$LINKS" \
  python3 -c "
import os

task              = os.environ['TASK']
title             = os.environ['TITLE'][:200]
body              = os.environ['BODY']
context_url       = os.environ['CONTEXT_URL']
actions           = os.environ['ACTIONS']
telegram_ref      = os.environ['TELEGRAM_REF']
links_raw         = os.environ.get('LINKS', '')
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

# ── Typed links (parent / relates / blocks) — Obsidian-style card map ──────────
# Syntax: links=parent:258;relates:312,318;blocks:340  (each value = issue #s).
# Rendered as a ## Links section of #N refs (auto-linked + clickable on GitHub,
# parsed by the cockpit for link-chips + the per-project Mermaid graph).
if links_raw and links_raw.strip():
    _order = ['parent', 'relates', 'blocks']
    _labels = {'parent': 'Parent', 'relates': 'Relates', 'blocks': 'Blocks'}
    _groups = {}
    for chunk in links_raw.split(';'):
        chunk = chunk.strip()
        if ':' not in chunk:
            continue
        kind, nums = chunk.split(':', 1)
        kind = kind.strip().lower()
        if kind not in _labels:
            continue
        refs = []
        for n in nums.split(','):
            n = n.strip().lstrip('#')
            if n.isdigit():
                refs.append('#' + n)
        if refs:
            _groups.setdefault(kind, []).extend(refs)
    if _groups:
        lines.append('## Links')
        for kind in _order:
            if kind in _groups:
                # de-dupe, preserve order
                seen = []
                for r in _groups[kind]:
                    if r not in seen:
                        seen.append(r)
                lines.append('- **' + _labels[kind] + ':** ' + ', '.join(seen))
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

  # due:<date> is a dynamic label — ensure it exists before applying (gh issue
  # create fails the whole call on an unknown label). proj:/host: are pre-created.
  if [ -n "$budget_label" ]; then
    gh label create "$budget_label" --repo "$BOARD_REPO" --color "0e8a16" \
      --description "Real-\$ budget (cache-excluded) for this card" --force >/dev/null 2>&1 || true
  fi
  if [ -n "$due_label" ]; then
    gh label create "$due_label" --repo "$BOARD_REPO" --color "fef2c0" \
      --description "Due date" --force >/dev/null 2>&1 || true
  fi
  # proj: may be a brand-new project slug — create it if missing (idempotent).
  if [ -n "$proj_label" ]; then
    gh label create "$proj_label" --repo "$BOARD_REPO" --color "5319e7" \
      --description "Super-project" --force >/dev/null 2>&1 || true
  fi

  # Assemble --label flags
  local label_args=()
  [ -n "$dept_label"    ] && label_args+=("--label" "$dept_label")
  [ -n "$host_label"    ] && label_args+=("--label" "$host_label")
  [ -n "$proj_label"    ] && label_args+=("--label" "$proj_label")
  [ -n "$due_label"     ] && label_args+=("--label" "$due_label")
  [ -n "$budget_label"  ] && label_args+=("--label" "$budget_label")
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

# _kanban_queue_alert — fire a Telegram alert when a card falls to the local
# queue. Reads TELEGRAM_BOT_TOKEN + BUBBLE_OPERATOR_CHAT_ID from env.
# Degrades silently (no abort) when either is unset — the stderr WARN is the
# primary signal; Telegram is a best-effort secondary.
_kanban_queue_alert() {
  local title="$1"
  local queue="$2"
  local tok="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${KANBAN_ALERT_CHAT_ID:-${BUBBLE_OPERATOR_CHAT_ID:-}}"

  if [ -z "$tok" ] || [ -z "$chat" ]; then
    # No token or no chat_id — stderr WARN already emitted by caller; skip Telegram.
    return 0
  fi

  local msg
  msg="[emit-kanban WARN] card NOT on board — fell to local queue.
Title: ${title}
Queue: ${queue}
Rick must run drain_kanban_queue.sh to replay."

  # Best-effort POST; never let the alert itself crash the emitter.
  curl -s -m 5 -o /dev/null \
    "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" \
    2>/dev/null || true
}

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
    # ── LOUD WARN: the card did NOT reach the board ───────────────────────────
    echo "[WARN] emit fell to local queue — card NOT on board (Rick must drain): ${TITLE}" >&2
    echo "emit_kanban_item: dashboard at ${KANBAN_HOST} returned $HTTP, item queued at $QUEUE" >&2
    _kanban_queue_alert "${TITLE}" "$QUEUE"
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
