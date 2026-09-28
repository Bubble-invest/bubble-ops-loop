#!/usr/bin/env bash
# list_my_board_cards.sh — surface the open board cards assigned to THIS agent
# (the punctual-mission funnel, board #304/#341). Prints them due-sorted
# (overdue first). Read-only; never fails the loop (exit 0 always).
#
# Usage:  list_my_board_cards.sh <dept-slug> [host]
#   <dept-slug>  e.g. ben, maya, tony, rnd  → filters label dept:<slug>
#   [host]       local|vps (default: vps)   → filters label host:<host>
#
# Auth: mirror emit_kanban_item.sh (ambient, dept/shared token files, minter,
# Mac fallback). Degrades gracefully when the board is unavailable.

set -uo pipefail

SLUG="${1:-}"
HOST="${2:-vps}"
[ -z "$SLUG" ] && { echo "list_my_board_cards: dept slug required as \$1" >&2; exit 0; }

BOARD_REPO="Bubble-invest/bubble-ops-board"

# Mirror emit_kanban_item.sh: ambient board access, dept/shared files,
# sudo minter, then the Mac Tailscale fallback. Never log credentials.
_resolve_gh_token() {
  local _gt="${GH_TOKEN-}" _ght="${GITHUB_TOKEN-}"
  [ -n "${_gt//[[:space:]]/}" ] || unset GH_TOKEN
  [ -n "${_ght//[[:space:]]/}" ] || unset GITHUB_TOKEN
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 \
     && gh api "repos/${BOARD_REPO}" --jq .name >/dev/null 2>&1; then
    return 0
  fi
  unset GH_TOKEN GITHUB_TOKEN
  local tokfile="${BOARD_TOKEN_FILE:-/run/bubble-board/token}"
  local dept_tokfile="" _tf ftok
  [ -n "${BUBBLE_DEPT:-}" ] && dept_tokfile="${tokfile}.${BUBBLE_DEPT}"
  for _tf in "$dept_tokfile" "$tokfile"; do
    [ -n "$_tf" ] || continue
    if command -v gh >/dev/null 2>&1 && [ -r "$_tf" ]; then
      ftok=$(cat "$_tf" 2>/dev/null || true)
      case "$ftok" in
        ghs_*) export GH_TOKEN="$ftok"; return 0 ;;
      esac
    fi
  done
  local minter=/usr/local/bin/bubble-board-token.sh tok
  if command -v gh >/dev/null 2>&1 && [ -x "$minter" ]; then
    tok=$(sudo -n "$minter" 2>/dev/null || true)
    if [ -n "$tok" ]; then
      export GH_TOKEN="$tok"
      return 0
    fi
  fi
  if command -v gh >/dev/null 2>&1 && [ "$(uname -s 2>/dev/null)" = Darwin ]; then
    tok=$(ssh -o BatchMode=yes -o ConnectTimeout=6 claude@joris-cx33 'cat /run/bubble-board/token' 2>/dev/null || true)
    case "$tok" in
      ghs_*) export GH_TOKEN="$tok"; return 0 ;;
    esac
  fi
  return 1
}

if ! _resolve_gh_token; then
  echo "list_my_board_cards: board auth unavailable — skip" >&2
  exit 0
fi

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
