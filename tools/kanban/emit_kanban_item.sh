#!/usr/bin/env bash
# emit_kanban_item.sh — push a single action-required item to the GitHub board kanban.
#
# Primary backend: create a GitHub issue on Bubble-invest/bubble-ops-board via `gh issue create`.
# Fallback: append to a local dead-letter queue file (drained later by
# tools/kanban/drain_kanban_queue.sh). The old localhost:3847 dashboard POST
# was formally retired (board #1251) — that receiver moved to GitHub Issues
# on 2026-06-20 (board #164) and nothing has served that port since.
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
#     budget=10 \
#     intent=system-convergence-north-star \
#     actions=accept,reject,escalate \
#     context_url=https://wiki/... \
#     telegram_ref="https://t.me/c/123/456"
#
# Required args: task, title, budget (integer USD, per-run — board #537: every card
#           must carry a budget so cost is attributable per card from creation).
#           A missing/invalid budget fails LOUD on stderr and creates NO card.
# Optional: body, type (approval|decision|incident|findings|manual|bug|feature|infra|docs|chore|research),
#           priority (normal|high|urgent), owner, actions (comma-separated), context_url, telegram_ref,
#           intent (one or more comma-separated live operator-intent slugs),
#           diagram_mermaid (Mermaid source for decision diagrams, ≤3000 chars),
#           visual_attachments (comma-separated repo-relative paths to images).
#
# GitHub labels applied automatically:
#   dept:<owner>  — if owner is a known dept (rnd/ben/maya/tony/content/security/accountant/morty/claudette)
#   type:<t>      — mapped from the type= arg
#   status:triage — default routing label (approval/decision types also add needs:human instead)
#
# Idempotency: if an open issue already contains <!-- emit-key: <task>::<title-slug> -->,
# no duplicate is created. The dedup key is task+title (not task alone) so a
# multi-finding cron can surface every distinct finding while a re-emit of the
# exact same finding still collapses to one card. The legacy <!-- emit-task: <task> -->
# marker is still emitted (drain_kanban_queue.sh + tooling grep for it).
#
# Exit code contract (board #1251 — fail loud):
#   0 — the card is actually ON THE BOARD: a GitHub issue was created, OR an
#       open issue for this task+title already existed (dedup hit).
#   0 — a REJECTED emit that intentionally creates no card at all (missing
#       task=/title=, or a missing/invalid budget= — board #537). These are
#       caller-usage errors, not board-reachability failures; they were
#       already loud on stderr (+ a Telegram alert for the budget case)
#       before this fix and stay exit-0 so a malformed call never aborts the
#       caller's larger tick. `--print-emit-key` also exits 0 (dry-run, no
#       card attempted either way).
#   1 — the card did NOT reach the board: GitHub was unreachable/failed AND
#       it fell to the local dead-letter queue (still written — queueing
#       beats losing the item — but the caller must be able to tell "filed"
#       from "not filed" and act accordingly, e.g. treat it as escalation-
#       worthy rather than assuming the finding is tracked).
# A caller that ignores the exit code already gets a queued item (never
# silent data loss); a caller that checks it now gets an honest signal.

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
BUDGET=""
INTENTS=""

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
    intent=*)       INTENTS="${arg#intent=}"    ;;
    intents=*)      INTENTS="${arg#intents=}"   ;;
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

# ── Dedup key derivation ──────────────────────────────────────────────────────
# The idempotency key is task + a deterministic slug of the title, so two
# DIFFERENT findings from the SAME task (e.g. a multi-finding audit cron) each
# get their own card, while re-emitting the EXACT same finding still dedups.
#
# Slug rules (must match the marker written into the issue body):
#   - derive from the title TRUNCATED to 200 chars (same truncation used for the
#     issue title), so the key stays consistent with what's stored
#   - lowercase
#   - every run of non-[a-z0-9] characters (spaces, /, →, the ':' in CVE-2024:…,
#     etc.) collapses to a single '-'
#   - leading/trailing '-' trimmed
# Output: "<task>::<slug>"
_emit_key() {
  TASK="$1" TITLE="$2" python3 -c "
import os, re
task = os.environ['TASK']
title = os.environ['TITLE'][:200].lower()
slug = re.sub(r'[^a-z0-9]+', '-', title).strip('-')
print(task + '::' + slug)
"
}

# Dry-run hook for tests: \`emit_kanban_item.sh --print-emit-key task=… title=…\`
# prints the computed dedup key and exits, exercising the REAL slug logic above
# without touching GitHub. Exempt from the budget gate below — it never creates
# a card, so it doesn't need one.
if [ "${1:-}" = "--print-emit-key" ]; then
  _emit_key "$TASK" "$TITLE"
  exit 0
fi

# ── Budget-reject Telegram alert ─────────────────────────────────────────────
# _budget_reject_alert — fire a Telegram alert when an emit is REJECTED for a
# missing/invalid budget. Mirrors _kanban_queue_alert exactly (same env vars,
# same guard, same best-effort curl): a fire-and-forget cron/agent tick doesn't
# have a human watching stderr, so a silently-dropped card is the wrong failure
# mode for #537 ("cards must be precise"). This makes the drop LOUD to a human.
# Degrades silently (no send, no abort) when TELEGRAM_BOT_TOKEN or the chat id
# is unset — keeps tests/dry-runs hermetic.
_budget_reject_alert() {
  local title="$1"
  local owner="$2"
  local task="$3"
  local got="$4"
  local tok="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${KANBAN_ALERT_CHAT_ID:-${BUBBLE_OPERATOR_CHAT_ID:-}}"

  if [ -z "$tok" ] || [ -z "$chat" ]; then
    # No token or no chat_id — stderr error already emitted by caller; skip Telegram.
    return 0
  fi

  local msg
  msg="⚠️ emit REJECTED — missing/invalid budget= on card '${title}' from ${owner:-?} (task=${task}). Card NOT created. Add budget=<int USD> and retry. (Got: '${got}')"

  # Best-effort POST; never let the alert itself crash the emitter.
  curl -s -m 5 -o /dev/null \
    "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" \
    2>/dev/null || true
}

# ── Budget is a MANDATORY emit input (board #537) ────────────────────────────
# Every emitted card must carry a per-run USD budget so cost is attributable
# per card from creation ("cards must be precise" — Joris, 2026-07-05). This is
# a hard validation gate, not a soft warning: a missing or non-integer budget
# must NOT create a card. We keep the existing "exit 0" convention (so a bad
# emit call fails LOUD on stderr but doesn't crash the calling agent's turn —
# emission failures must never break a cron/agent tick), but we return BEFORE
# any gh/dashboard emission happens, so no card is ever created without one.
# In addition to the stderr error, fire a best-effort Telegram alert so a
# silently-dropped card is loud to a human (a fire-and-forget tick has no human
# on stderr) — same alert pattern as the fallback-queue case (_kanban_queue_alert).
_BUDGET_STRIPPED="${BUDGET#\$}"
if [ -z "$BUDGET" ] || ! echo "$_BUDGET_STRIPPED" | grep -qE '^[1-9][0-9]*$'; then
  echo "emit_kanban_item: budget= is required (integer USD, per-run) — every card must carry a budget. Got: '${BUDGET}'" >&2
  _budget_reject_alert "$TITLE" "$OWNER" "$TASK" "$BUDGET"
  exit 0
fi

# ── Intent proposal normalization (advisory, never a creation gate) ──────────
# Accept the label slug, bare slug, or #1247 wiki-link form and store one
# canonical comma-separated slug list.  The body link is always written, even
# when the corresponding board label has not been installed yet.  An absent or
# malformed intent is LOUDLY flagged but can never block urgent/legitimate work.
_normalize_intents() {
  INTENTS_RAW="$1" python3 -c "
import os, re
raw = os.environ.get('INTENTS_RAW', '')
seen = []
for token in raw.split(','):
    value = token.strip()
    value = re.sub(r'^intent:', '', value, flags=re.I)
    m = re.fullmatch(r'\[\[shared/operator-intents/([A-Za-z0-9][A-Za-z0-9_-]*)\]\]', value)
    if m:
        value = m.group(1)
    value = re.sub(r'^shared/operator-intents/', '', value)
    # #1247 is case-exact and extensionless.  A `.md` target is unresolved,
    # not something the emitter silently repairs/blesses.
    if value.lower().endswith('.md'):
        continue
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value) and value not in seen:
        seen.append(value)
print(','.join(seen))
"
}

NORMALIZED_INTENTS=$(_normalize_intents "$INTENTS")
if [ -z "$NORMALIZED_INTENTS" ]; then
  echo "[WARN] emit-kanban: no usable intent= supplied — card will be created and FLAGGED as an intent ORPHAN for triage (never blocked)" >&2
elif [ -n "$INTENTS" ] && [ "$NORMALIZED_INTENTS" != "$INTENTS" ]; then
  echo "emit_kanban_item: normalized intent= to '${NORMALIZED_INTENTS}'" >&2
fi

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

  # Idempotency check: look for an open issue carrying this exact task+title key.
  # Dedup is on task+title (not task alone) so distinct findings from one
  # multi-finding cron each surface their own card; a re-emit of the same
  # finding still collapses to one.
  local emit_key
  emit_key=$(_emit_key "$TASK" "$TITLE")
  local marker="emit-key: ${emit_key}"
  local existing
  existing=$(gh issue list \
    --repo "$BOARD_REPO" \
    --state open \
    --search "\"${marker}\" in:body" \
    --limit 1 \
    --json number \
    --jq '.[0].number' 2>/dev/null || true)

  if [ -n "$existing" ]; then
    echo "emit_kanban_item: open issue #${existing} already exists for key=${emit_key} — skipping duplicate" >&2
    return 0
  fi

  # Build issue body and title via python3 (robust escaping, same approach as original)
  local tmpfile
  tmpfile=$(mktemp "${TMPDIR:-/tmp}/emit_kanban_body.XXXXXX")

  TASK="$TASK" TITLE="$TITLE" BODY="$BODY" TYPE="$TYPE" PRIORITY="$PRIORITY" \
  OWNER="$OWNER" ACTIONS="$ACTIONS" CONTEXT_URL="$CONTEXT_URL" TELEGRAM_REF="$TELEGRAM_REF" \
  DIAGRAM_MERMAID="$DIAGRAM_MERMAID" VISUAL_ATTACHMENTS="$VISUAL_ATTACHMENTS" LINKS="$LINKS" \
  NORMALIZED_INTENTS="$NORMALIZED_INTENTS" \
  EMIT_KEY="$emit_key" \
  python3 -c "
import os

task              = os.environ['TASK']
emit_key          = os.environ['EMIT_KEY']
title             = os.environ['TITLE'][:200]
body              = os.environ['BODY']
context_url       = os.environ['CONTEXT_URL']
actions           = os.environ['ACTIONS']
telegram_ref      = os.environ['TELEGRAM_REF']
links_raw         = os.environ.get('LINKS', '')
diagram_mermaid   = os.environ.get('DIAGRAM_MERMAID', '')
visual_attach_raw = os.environ.get('VISUAL_ATTACHMENTS', '')
intent_slugs      = [s for s in os.environ.get('NORMALIZED_INTENTS', '').split(',') if s]

lines = []
lines.append('## Job')
lines.append(title)
lines.append('')
if intent_slugs:
    links = ['[[shared/operator-intents/' + slug + ']]' for slug in intent_slugs]
    lines.append('Serves-intent(s): ' + ', '.join(links))
else:
    lines.append('Serves-intent(s): UNRESOLVED')
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
# Legacy task marker — kept for drain_kanban_queue.sh + tooling that greps it.
lines.append('<!-- emit-task: ' + task + ' -->')
# Dedup key (task + title slug) — what the idempotency search now matches on.
lines.append('<!-- emit-key: ' + emit_key + ' -->')

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

  # Intent labels are collection-derived and installed separately by the
  # read-only-by-default label taxonomy tool.  Never make issue creation fail
  # because a label has not reached the board yet: apply only exact existing
  # labels; the canonical body link above remains durable either way.
  local existing_intent_labels=""
  existing_intent_labels=$(gh label list --repo "$BOARD_REPO" --limit 1000 --json name \
    --jq '.[].name' 2>/dev/null || true)

  # Assemble --label flags
  local label_args=()
  [ -n "$dept_label"    ] && label_args+=("--label" "$dept_label")
  [ -n "$host_label"    ] && label_args+=("--label" "$host_label")
  [ -n "$proj_label"    ] && label_args+=("--label" "$proj_label")
  [ -n "$due_label"     ] && label_args+=("--label" "$due_label")
  [ -n "$budget_label"  ] && label_args+=("--label" "$budget_label")
  [ -n "$type_label"    ] && label_args+=("--label" "$type_label")
  [ -n "$routing_label" ] && label_args+=("--label" "$routing_label")
  if [ -n "$NORMALIZED_INTENTS" ]; then
    local _intent_slug _intent_label _existing_match
    while IFS= read -r _intent_slug; do
      [ -n "$_intent_slug" ] || continue
      _intent_label="intent:${_intent_slug}"
      _existing_match=$(printf '%s\n' "$existing_intent_labels" | \
        awk -v wanted="$_intent_label" 'tolower($0) == tolower(wanted) { print; exit }')
      if [ -n "$_existing_match" ]; then
        label_args+=("--label" "$_existing_match")
        if [ "$_existing_match" != "$_intent_label" ]; then
          echo "[WARN] emit-kanban: label casing drift (${_existing_match}; canonical ${_intent_label}) — reusing existing label, not creating a duplicate" >&2
        fi
      else
        echo "[WARN] emit-kanban: label ${_intent_label} is not installed — preserving body link and creating card without that label" >&2
      fi
    done < <(printf '%s\n' "$NORMALIZED_INTENTS" | tr ',' '\n')
  fi

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

# _resolve_queue_path — the single source of truth for where the dead-letter
# queue file lives when GitHub is unreachable. KEEP THIS IN SYNC WITH
# drain_kanban_queue.sh's own default (same candidate order) — board #1251's
# root cause was these two independently-guessed paths silently disagreeing,
# so a queued card could land somewhere nothing ever drains.
#
# Priority (first usable wins):
#   1. $KANBAN_QUEUE — explicit override, always wins (a per-dept systemd
#      drop-in, or a test harness).
#   2. $BUBBLE_AGENT_WORKDIR/memory/kanban_queue.jsonl — the dept's OWN
#      persistent project checkout (e.g. /srv/agents/morty), set by the
#      agent-launch unit for every uid-isolated dept. This is the fix for
#      board #1251's actual failure: a sandboxed Bash tool call's writable
#      filesystem is scoped to the project checkout + a session-scoped tmp
#      dir, NOT arbitrary paths under $HOME — so $HOME-relative guesses (the
#      old candidates below) silently failed inside the sandbox even though
#      the dept uid owns $HOME at the Unix-permission level, and the write
#      fell through to $TMPDIR (a `/tmp/claude-<uid>` dir that vanishes with
#      the session — see #1250's incident). A path under the project
#      checkout is inside the sandbox's writable root, so it actually lands.
#   3. $HOME/claude-workspaces/Rick_RnD/monitoring/kanban_queue.jsonl — Rick's
#      own dev-Mac default (no BUBBLE_AGENT_WORKDIR there; this IS the
#      project tree on that box).
#   4. ${TMPDIR:-/tmp}/kanban_queue.jsonl — last resort only. Session-scoped;
#       the caller is told explicitly that this copy may not survive.
_resolve_queue_path() {
  if [ -n "${KANBAN_QUEUE:-}" ]; then
    printf '%s' "$KANBAN_QUEUE"
    return 0
  fi
  if [ -n "${BUBBLE_AGENT_WORKDIR:-}" ]; then
    local _wd="${BUBBLE_AGENT_WORKDIR%/}/memory"
    if mkdir -p "$_wd" 2>/dev/null && [ -w "$_wd" ]; then
      printf '%s' "$_wd/kanban_queue.jsonl"
      return 0
    fi
  fi
  local _rick="$HOME/claude-workspaces/Rick_RnD/monitoring"
  if mkdir -p "$_rick" 2>/dev/null && [ -w "$_rick" ]; then
    printf '%s' "$_rick/kanban_queue.jsonl"
    return 0
  fi
  printf '%s' "${TMPDIR:-/tmp}/kanban_queue.jsonl"
  return 1  # signals "this is the session-scoped last resort", not an error
}

# _queue_emit — the fallback used when GitHub is unreachable/failed.
#
# BOARD #1251: this used to POST to a local dashboard at localhost:3847
# first. That receiver was retired 2026-06-20 (board #164 — the kanban moved
# to GitHub Issues) and nothing has listened on that port anywhere in the
# fleet since; every emit that reached this function paid a real network
# timeout (`curl -m 5`) to hit a dead hop before queueing. Formally retired
# here (board #1251 ask 4) — go straight to the durable queue.
_queue_emit() {
  local PAYLOAD
  PAYLOAD=$(TASK="$TASK" TITLE="$TITLE" BODY="$BODY" TYPE="$TYPE" PRIORITY="$PRIORITY" \
            OWNER="$OWNER" ACTIONS="$ACTIONS" CONTEXT_URL="$CONTEXT_URL" TELEGRAM_REF="$TELEGRAM_REF" \
            BUDGET="$BUDGET" NORMALIZED_INTENTS="$NORMALIZED_INTENTS" \
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
    'budget': int(os.environ['BUDGET'].lstrip('$')),
    'intents': [s for s in os.environ.get('NORMALIZED_INTENTS', '').split(',') if s],
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
    echo "emit_kanban_item: failed to build queue payload" >&2
    return 1
  fi

  local QUEUE
  QUEUE=$(_resolve_queue_path)
  local queue_is_durable=$?
  mkdir -p "$(dirname "$QUEUE")" 2>/dev/null || true
  echo "$PAYLOAD" >> "$QUEUE"
  # ── LOUD WARN: the card did NOT reach the board ───────────────────────────
  echo "[WARN] emit fell to local queue — card NOT on board (Rick must drain): ${TITLE}" >&2
  if [ "$queue_is_durable" -eq 0 ]; then
    echo "emit_kanban_item: GitHub unavailable, item queued at durable path $QUEUE" >&2
  else
    echo "emit_kanban_item: GitHub unavailable AND no durable queue dir was writable — item queued at SESSION-SCOPED $QUEUE (may not survive; fix BUBBLE_AGENT_WORKDIR/KANBAN_QUEUE for this host)" >&2
  fi
  _kanban_queue_alert "${TITLE}" "$QUEUE"
  return 1
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
  # 0. POISONED-ENV GUARD. A caller that prefixes the invocation with
  #    `GH_TOKEN="${GITHUB_TOKEN:-}" ...` (a common but self-sabotaging habit —
  #    Morty/VPS did exactly this) exports an EMPTY GH_TOKEN. An empty GH_TOKEN
  #    makes `gh auth status` exit 0 while `gh` is actually UNauthenticated, so
  #    the check below would return 0 early and every `gh` call then fails with
  #    "Could not resolve to a Repository" — the emit silently falls to the
  #    dead local queue. Unset any empty/whitespace-only token so a poisoned env
  #    can never defeat the real token resolution (step 2a) below.
  local _gt="${GH_TOKEN-}"; if [ -z "${_gt//[[:space:]]/}" ]; then unset GH_TOKEN; fi
  local _ght="${GITHUB_TOKEN-}"; if [ -z "${_ght//[[:space:]]/}" ]; then unset GITHUB_TOKEN; fi
  # 1. `gh auth status` alone is NOT sufficient (#536 signature, #673): a
  #    SCOPED GITHUB_TOKEN (e.g. repo-only, no access to the board org/repo)
  #    passes `gh auth status` (it's a real, non-empty token) while every
  #    board API call still 404s — the poisoned-env guard above only catches
  #    an EMPTY token, not a valid-but-wrong-scope one. This bit Morty for
  #    ~3 weeks. Ambient auth is trusted ONLY once we've proven it can
  #    actually reach the board repo via a real API call.
  if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1 \
     && gh api "repos/${BOARD_REPO}" --jq .name &>/dev/null 2>&1; then
    return 0  # gh authed AND board-reachable — _gh_emit uses ambient auth
  fi
  # Ambient auth either isn't present or can't reach the board — make sure a
  # poisoned/wrong-scope token doesn't leak into the fallback paths below
  # (step 2a/2b mint their OWN token via GH_TOKEN=; a stale wrong-scope
  # GH_TOKEN still in the env would keep failing the same way).
  unset GH_TOKEN
  unset GITHUB_TOKEN
  # 2a. Preferred VPS path: a root-owned systemd timer (bubble-board-token-refresh)
  #     keeps a fresh short-lived board token at /run/bubble-board/token
  #     (root:claude 0640, ~45min refresh). Reading it needs NO sudo, so it works
  #     even under NoNewPrivileges (where the sudo-minter path below is blocked).
  #     BOARD #1251: that shared file is group `claude`-only, so it is invisible
  #     to a per-dept isolated agent uid (agent-morty, agent-ben, ...) even
  #     though a NoNewPrivileges Bash sandbox is exactly the case that needs
  #     the no-sudo path most. The refresher (deploy/bin/bubble-board-token-
  #     refresh.sh) also drops a PER-DEPT copy at /run/bubble-board/token.<dept>
  #     owned root:agent-<dept> 0640 — no credential is broadened, each dept
  #     only gets read access to a copy already permitted to that same dept's
  #     sudoers-scoped minter grant (/etc/sudoers.d/bubble-board-token-agent-
  #     <dept>). Try the per-dept copy first (when BUBBLE_DEPT is set), then
  #     the shared file, so a sandboxed dept session finds a readable token
  #     without needing sudo at all.
  local tokfile="${BOARD_TOKEN_FILE:-/run/bubble-board/token}"
  local dept_tokfile=""
  [ -n "${BUBBLE_DEPT:-}" ] && dept_tokfile="${tokfile}.${BUBBLE_DEPT}"
  local _tf
  for _tf in "$dept_tokfile" "$tokfile"; do
    [ -n "$_tf" ] || continue
    if command -v gh &>/dev/null && [ -r "$_tf" ]; then
      local ftok
      ftok=$(cat "$_tf" 2>/dev/null || true)
      case "$ftok" in
        ghs_*) export GH_TOKEN="$ftok"; return 0 ;;
      esac
    fi
  done
  # 2b. Fallback: mint on demand via the root-owned minter through a sudoers
  #     NOPASSWD rule (works only where NoNewPrivileges is NOT set).
  local minter=/usr/local/bin/bubble-board-token.sh
  if command -v gh &>/dev/null && [ -x "$minter" ]; then
    local tok
    tok=$(sudo -n "$minter" 2>/dev/null || true)
    if [ -n "$tok" ]; then
      export GH_TOKEN="$tok"
      return 0
    fi
  fi
  # 2c. host:local (Mac) fallback — fetch the short-lived board token minted on
  #     the VPS (bubble-board-token-refresh.timer -> /run/bubble-board/token,
  #     ~45min, issues:write only) over Tailscale. Only attempted when all local
  #     paths above failed AND we're actually on a Mac (Darwin) — on the VPS
  #     itself /run/bubble-board/token is already local (step 2a), so this never
  #     fires there and we never add a needless SSH hop to ourselves. Requires
  #     this Mac's key in claude@joris-cx33 authorized_keys. Verified live on
  #     Geraldine's M5 during the 2026-07-02 VPS→M5 migration (board #463).
  if command -v gh &>/dev/null && [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    local vtok
    vtok=$(ssh -o BatchMode=yes -o ConnectTimeout=6 claude@joris-cx33 'cat /run/bubble-board/token' 2>/dev/null || true)
    case "$vtok" in
      ghs_*) export GH_TOKEN="$vtok"; return 0 ;;
    esac
  fi
  return 1  # no usable GitHub auth
}

# ── Main: try GitHub first, fall back to the durable local queue ─────────────

GH_OK=0
if _resolve_gh_token; then
  if _gh_emit; then
    GH_OK=1
  fi
fi

if [ "$GH_OK" -eq 0 ]; then
  echo "emit_kanban_item: gh unavailable or failed — falling back to local queue" >&2
  _queue_emit
  # ── FAIL LOUD (board #1251) ────────────────────────────────────────────────
  # Neither transport landed the card on the board. The queue write above
  # (whatever path it used) means the finding is not LOST, but it is not
  # TRACKED either — a caller that doesn't check this exit code must not be
  # able to walk away believing the finding is on the board.
  echo "emit_kanban_item: CARD NOT ON BOARD — '${TITLE}' (task=${TASK}) — see the [WARN] above for the queue path; run tools/kanban/drain_kanban_queue.sh to replay once GitHub/auth is reachable" >&2
  exit 1
fi

exit 0
