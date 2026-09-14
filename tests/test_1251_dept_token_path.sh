#!/usr/bin/env bash
# test_1251_dept_token_path.sh — board #1251: the sandbox-safe per-dept board
# token path. deploy/bin/bubble-board-token-refresh.sh drops a per-dept copy
# at /run/bubble-board/token.<dept> (root:agent-<dept> 0640) alongside the
# shared /run/bubble-board/token (root:claude 0640). A uid-isolated dept
# session (agent-morty etc.) cannot read the shared file post-#1120 — this
# test proves _resolve_gh_token's step 2a checks the per-dept file (named via
# BOARD_TOKEN_FILE + BUBBLE_DEPT) and uses it even when the shared file is
# absent/unreadable, without ever needing sudo (the only path that actually
# works under a NoNewPrivileges Bash-tool sandbox).
#
# Run: bash tests/test_1251_dept_token_path.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

STUBBIN="$TMPDIR_T/stubbin"
mkdir -p "$STUBBIN"
MARKER="$TMPDIR_T/gh-create-was-called"
cat > "$STUBBIN/gh" <<EOF
#!/usr/bin/env bash
_tok_usable() { case "\${GH_TOKEN:-}" in ghs_*) return 0;; *) return 1;; esac; }
case "\${1:-} \${2:-}" in
  "auth status") exit 1 ;;                          # ambient gh never authed here
  "issue list")  _tok_usable && echo "" || exit 1 ;;
  "label list")  _tok_usable && echo "" || exit 1 ;;
  "label create") _tok_usable && exit 0 || exit 1 ;;
  "issue create")
     _tok_usable || exit 1
     touch "$MARKER"
     echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99998" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUBBIN/gh"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STUBBIN/sudo"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STUBBIN/ssh"
chmod +x "$STUBBIN/sudo" "$STUBBIN/ssh"

BOARDTOK="$TMPDIR_T/run-bubble-board-token"
DEPTTOK="${BOARDTOK}.morty"
echo "ghs_deptonlytoken" > "$DEPTTOK"
# Deliberately do NOT create $BOARDTOK (the shared claude-group file) — this
# simulates the real post-#1120 state: a dept uid can read its OWN per-dept
# copy but not the shared one.

QUEUE="$TMPDIR_T/queue.jsonl"
out=$(
  PATH="$STUBBIN:$PATH" \
  BOARD_TOKEN_FILE="$BOARDTOK" \
  BUBBLE_DEPT="morty" \
  KANBAN_QUEUE="$QUEUE" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-1251-dept-token \
    title="Dept-only token reaches the board" \
    type=incident \
    owner=morty \
    budget=10 2>&1
)
exit_code=$?

[ -f "$MARKER" ] \
  || fail "gh issue create was never reached with a per-dept-only token file. Got: $out"
pass "per-dept token file (BOARD_TOKEN_FILE.\$BUBBLE_DEPT) is used when the shared file is absent"

[ "$exit_code" -eq 0 ] || fail "expected exit 0 (card reached the board), got $exit_code"
pass "exit code is 0 — card genuinely reached the board via the dept-only token"

[ ! -f "$QUEUE" ] || fail "card ALSO fell to the local queue despite reaching the board via the dept token"
pass "card did not fall to the local queue"

# Sanity: WITHOUT BUBBLE_DEPT set at all, the same dept-only file must NOT be
# found (there is nothing to derive the per-dept filename from), proving the
# success above genuinely came from the dept-token path, not some other leak.
rm -f "$MARKER"
out2=$(
  PATH="$STUBBIN:$PATH" \
  BOARD_TOKEN_FILE="$BOARDTOK" \
  KANBAN_QUEUE="$TMPDIR_T/queue2.jsonl" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-1251-dept-token-2 \
    title="No BUBBLE_DEPT means no dept-token lookup" \
    type=incident \
    owner=morty \
    budget=10 2>&1
)
exit2=$?
[ ! -f "$MARKER" ] \
  || fail "gh issue create was reached even without BUBBLE_DEPT set — the shared file must not exist, so this proves nothing was actually gating on the dept file"
[ "$exit2" -ne 0 ] || fail "expected non-zero exit without BUBBLE_DEPT set (no readable token at all), got 0"
pass "without BUBBLE_DEPT, the dept-only token file is correctly not found (control case)"

echo ""
echo "All tests passed."
