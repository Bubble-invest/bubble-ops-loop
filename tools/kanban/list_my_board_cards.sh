#!/usr/bin/env bash
# list_my_board_cards.sh — surface the open board cards assigned to THIS agent
# (the punctual-mission funnel, board #304/#341). Prints them due-sorted
# (overdue first). Read-only; never fails the loop (exit 0 always).
#
# Usage:  list_my_board_cards.sh <dept-slug> [host]
#   <dept-slug>  e.g. ben, maya, tony, rnd  → filters label dept:<slug>
#   [host]       local|vps (default: vps)   → filters label host:<host>
#
# Auth: same pattern as emit_kanban_item.sh — use an already-authenticated gh
# (Mac dev), else mint a board-scoped token via bubble-board-token.sh and export
# it as GH_TOKEN (VPS depts). Degrades silently if neither is available.

set -uo pipefail

SLUG="${1:-}"
HOST="${2:-vps}"
[ -z "$SLUG" ] && { echo "list_my_board_cards: dept slug required as \$1" >&2; exit 0; }

BOARD_REPO="Bubble-invest/bubble-ops-board"

# Resolve gh auth (mirror emit_kanban_item.sh)
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  : # already authenticated
else
  MINTER=/usr/local/bin/bubble-board-token.sh
  if [ -x "$MINTER" ]; then
    TOK="$("$MINTER" 2>/dev/null || true)"
    [ -n "$TOK" ] && export GH_TOKEN="$TOK"
  fi
fi

command -v gh >/dev/null 2>&1 || { echo "list_my_board_cards: gh not available — skip" >&2; exit 0; }

gh issue list --repo "$BOARD_REPO" --state open \
  --label "dept:${SLUG}" --limit 100 \
  --json number,title,labels,createdAt 2>/dev/null \
| HOST="$HOST" python3 -c "
import sys, json, datetime, os
HOST = os.environ.get('HOST', 'vps')
try:
    d = json.load(sys.stdin)
except Exception:
    print('  (could not read board — skip)'); raise SystemExit
def host_of(i):
    for l in i.get('labels', []):
        if l['name'].startswith('host:'): return l['name'][5:]
    return None
# surface cards whose host matches OR is absent (un-hosted legacy cards belong to the dept)
d = [i for i in d if host_of(i) in (HOST, None)]
def due(i):
    for l in i.get('labels', []):
        if l['name'].startswith('due:'): return l['name'][4:]
    return None
today = datetime.date.today().isoformat()
def key(i):
    dd = due(i)
    return (0, dd) if dd else (1, i.get('createdAt',''))   # overdue/soonest first, undated last
items = sorted(d, key=key)
if not items:
    print('  (no board cards assigned to me)'); raise SystemExit
overdue = [i for i in items if (due(i) and due(i) < today)]
print(f'{len(items)} board card(s) assigned to me' + (f' — {len(overdue)} OVERDUE' if overdue else '') + ':')
for i in items:
    dd = due(i); flag = ' OVERDUE' if dd and dd < today else (f' (due {dd})' if dd else '')
    print(f\"  #{i['number']}{flag}  {i['title'][:72]}\")
"
exit 0
