#!/usr/bin/env bash
# test_emit_kanban_dismiss_ledger.sh — end-to-end proof that emit_kanban_item.sh
# never reaches `gh issue create` for a key on the persistent dismiss-ledger
# (board #1395), while a genuinely new key still creates a card normally.
#
# This is the durable-across-restarts regression the ledger exists to prevent:
# wiki-compile's intent-audit extractor kept re-flagging the SAME ~5-7 pages
# every nightly compile because closing the board CARD never recorded the
# dismissal anywhere the next compile could see — the open-issue idempotency
# check only helps while the card stays open. The dismiss-ledger fixes that by
# checking a stable task::title-slug key against a git-tracked JSON file
# BEFORE any GitHub call is made.
#
# Run: bash tests/test_emit_kanban_dismiss_ledger.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

# ── Stub `gh` so the emitter's happy path is reachable hermetically ─────────
# `gh auth status` + `gh api repos/<board>` succeed (mirrors the "already
# authenticated gh" case _resolve_gh_token checks first) so _gh_emit runs.
# `gh issue list` reports no existing duplicate. `gh label list/create` are
# no-ops. `gh issue create` touches a marker file and prints a fake issue URL
# — its presence/absence is what proves whether GitHub was ever reached.
STUBBIN="$TMPDIR_T/stubbin"
mkdir -p "$STUBBIN"
CREATE_MARKER="$TMPDIR_T/gh-issue-create-was-called"
cat > "$STUBBIN/gh" <<EOF
#!/usr/bin/env bash
case "\${1:-} \${2:-}" in
  "auth status")            exit 0 ;;
  "api repos/Bubble-invest/bubble-ops-board") exit 0 ;;
  "issue list")             echo "" ; exit 0 ;;
  "label list")             echo "" ; exit 0 ;;
  "label create")           exit 0 ;;
  "issue create")
     touch "$CREATE_MARKER"
     echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99999"
     exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUBBIN/gh"

# ── Test ledger: dismisses ONE specific key ──────────────────────────────────
LEDGER="$TMPDIR_T/dismissed_emit_keys.json"
DISMISSED_TASK="wiki-intent-candidate-leak"
DISMISSED_TITLE="candidate intent leak: cgp/hot.md"
DISMISSED_KEY=$(bash "$EMITTER" --print-emit-key "task=$DISMISSED_TASK" "title=$DISMISSED_TITLE")
cat > "$LEDGER" <<EOF
{"schema_version": 1, "dismissed": [{"key": "$DISMISSED_KEY", "reason": "test fixture", "dismissed_at": "2026-09-22", "dismissed_by": "test"}]}
EOF

# ── Test 1: a DISMISSED key never reaches gh issue create ───────────────────
rm -f "$CREATE_MARKER"
stderr1=$(
  PATH="$STUBBIN:$PATH" \
  KANBAN_DISMISS_LEDGER="$LEDGER" \
  KANBAN_QUEUE="$TMPDIR_T/queue1.jsonl" \
  TELEGRAM_BOT_TOKEN="" \
  bash "$EMITTER" \
    task="$DISMISSED_TASK" \
    title="$DISMISSED_TITLE" \
    type=findings owner=rnd budget=1 2>&1 >/dev/null
)
exit1=$?

[ "$exit1" -eq 0 ] || fail "dismissed-key emit exited $exit1, expected 0 (intentionally-no-card is a success)"
pass "dismissed-key emit exits 0"

[ ! -f "$CREATE_MARKER" ] || fail "gh issue create WAS called for a dismissed key — ledger did not block it"
pass "gh issue create was never called for the dismissed key"

echo "$stderr1" | grep -qi "dismiss-ledger" \
  || fail "stderr does not mention the dismiss-ledger skip. Got: $stderr1"
pass "stderr names the dismiss-ledger skip"

[ ! -f "$TMPDIR_T/queue1.jsonl" ] || fail "dismissed key fell to the local dead-letter queue (should just be skipped, not queued)"
pass "dismissed key did not fall to the local queue either"

# ── Test 2: a genuinely NEW key (not on the ledger) still creates a card ────
rm -f "$CREATE_MARKER"
NEW_TITLE="candidate intent leak: rick_rnd/genuinely-new-page.md"
exit2=$(
  PATH="$STUBBIN:$PATH" \
  KANBAN_DISMISS_LEDGER="$LEDGER" \
  KANBAN_QUEUE="$TMPDIR_T/queue2.jsonl" \
  TELEGRAM_BOT_TOKEN="" \
  bash "$EMITTER" \
    task="$DISMISSED_TASK" \
    title="$NEW_TITLE" \
    type=findings owner=rnd budget=1 >/dev/null 2>&1
  echo $?
)
[ "$exit2" -eq 0 ] || fail "new-key emit exited $exit2, expected 0"
pass "new-key emit exits 0"

[ -f "$CREATE_MARKER" ] || fail "gh issue create was NOT called for a genuinely new (non-dismissed) key — the ledger over-suppressed"
pass "gh issue create WAS called for the genuinely new key — real findings still surface"

# ── Test 3: a missing ledger file degrades to current (emit) behavior ───────
rm -f "$CREATE_MARKER"
exit3=$(
  PATH="$STUBBIN:$PATH" \
  KANBAN_DISMISS_LEDGER="$TMPDIR_T/does-not-exist.json" \
  KANBAN_QUEUE="$TMPDIR_T/queue3.jsonl" \
  TELEGRAM_BOT_TOKEN="" \
  bash "$EMITTER" \
    task="$DISMISSED_TASK" \
    title="$DISMISSED_TITLE" \
    type=findings owner=rnd budget=1 >/dev/null 2>&1
  echo $?
)
[ "$exit3" -eq 0 ] || fail "missing-ledger emit exited $exit3, expected 0"
[ -f "$CREATE_MARKER" ] || fail "a MISSING ledger file blocked emission — must degrade to emit, never crash/over-suppress"
pass "a missing ledger file degrades to current (emit) behavior, never blocks or crashes"

echo ""
echo "All tests passed."
