#!/usr/bin/env bash
# Read a board card and ALL comments without depending on gh's TTY rendering.
# Usage: view_board_card.sh <issue-number> [--json]
# JSON: REST issue object with comments replaced by the complete comment array.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^[1-9][0-9]*$ || ( $# -eq 2 && "$2" != --json ) ]]; then
  echo "Usage: view_board_card.sh <issue-number> [--json]" >&2
  exit 2
fi
ISSUE="$1"
FORMAT="${2:-text}"
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
  echo "view_board_card: board authentication unavailable (check the per-dept token file)" >&2
  exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo "view_board_card: python3 is required" >&2; exit 1; }
TMP_CARD=$(mktemp -d)
trap 'rm -rf "$TMP_CARD"' EXIT
if ! gh api "repos/${BOARD_REPO}/issues/${ISSUE}" >"$TMP_CARD/issue.json" 2>/dev/null; then
  echo "view_board_card: cannot read card #${ISSUE} (authentication/access failure, not found, or API unavailable)" >&2
  exit 1
fi
if ! gh api --paginate --slurp "repos/${BOARD_REPO}/issues/${ISSUE}/comments?per_page=100" >"$TMP_CARD/comments.json" 2>/dev/null; then
  echo "view_board_card: cannot read all comments for #${ISSUE} (authentication/access failure or API unavailable)" >&2
  exit 1
fi
python3 - "$TMP_CARD" "$FORMAT" <<'PYTHON'
import json
from pathlib import Path
import sys

try:
    directory = Path(sys.argv[1])
    card = json.loads((directory / "issue.json").read_text())
    pages = json.loads((directory / "comments.json").read_text())
    comments = sorted(
        (comment for page in pages for comment in page),
        key=lambda comment: (comment["created_at"], comment["id"]),
    )
    if sys.argv[2] == "--json":
        card["comments"] = comments
        output = json.dumps(card, ensure_ascii=False, indent=2)
    else:
        lines = [f"#{card['number']} {card['title']}", f"State: {card['state']}",
                 "Labels: " + ", ".join(label["name"] for label in card["labels"]),
                 "", card.get("body") or "", "", f"Comments ({len(comments)}):"]
        for comment in comments:
            author = (comment.get("user") or {}).get("login") or "[deleted]"
            lines.extend(["", f"{author} — {comment['created_at']}", comment.get("body") or ""])
        output = "\n".join(lines)
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    print("view_board_card: invalid or incomplete board API response", file=sys.stderr)
    sys.exit(1)
print(output)
PYTHON
