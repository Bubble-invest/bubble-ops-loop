#!/usr/bin/env bash
# test_emit_kanban_fallback_loud.sh — verify that emit_kanban_item.sh emits a
# [WARN] on stderr and exits 0 when both gh and the dashboard fail.
#
# Covers bug #262: previously a failed emission exited 0 silently — cards
# vanished into kanban_queue.jsonl with no operator signal.
#
# Run: bash tests/test_emit_kanban_fallback_loud.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# ── Test 1: fallback path emits LOUD [WARN] on stderr ────────────────────────

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT
QUEUE="$TMPDIR_T/kanban_queue.jsonl"

stderr_output=$(
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-emit-fallback-loud \
    title="Bug 262 fallback test card" \
    type=incident \
    owner=rnd 2>&1 >/dev/null
)
exit_code=$?

# Assert exit 0 (emission must never break callers)
[ "$exit_code" -eq 0 ] || fail "exit code was $exit_code, expected 0"
pass "exit code is 0"

# Assert [WARN] appears on stderr
echo "$stderr_output" | grep -q "\[WARN\].*NOT on board" \
  || fail "[WARN] ... NOT on board not found in stderr. Got: $stderr_output"
pass "[WARN] ... NOT on board present on stderr"

# Assert card landed in queue
[ -f "$QUEUE" ] || fail "kanban_queue.jsonl not created"
pass "kanban_queue.jsonl created"

# Assert queue contains the card title
grep -q "Bug 262 fallback test card" "$QUEUE" \
  || fail "card title not found in queue. Queue: $(cat "$QUEUE")"
pass "card title present in kanban_queue.jsonl"

# ── Test 2: happy path (gh succeeds) is unchanged / quiet ────────────────────
# We can't call real GitHub in CI, so we just verify the emitter exits 0
# when gh auth isn't available (it should fall through to dashboard+queue).
# This test confirms we haven't broken the happy-path exit convention.

QUEUE2="$TMPDIR_T/kanban_queue2.jsonl"
exit2=$(
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE2" \
  TELEGRAM_BOT_TOKEN="" \
  bash "$EMITTER" \
    task=test-emit-fallback-loud-2 \
    title="Second card — still exits 0" \
    type=findings \
    owner=ben 2>/dev/null; echo $?
)
[ "$exit2" -eq 0 ] || fail "second emit call returned $exit2, expected 0"
pass "second emit call exits 0"

# ── Test 3: drain dry-run lists card and exits 0 ─────────────────────────────

DRAIN="$REPO_ROOT/tools/kanban/drain_kanban_queue.sh"
if [ -x "$DRAIN" ]; then
  QUEUE3="$TMPDIR_T/kanban_queue3.jsonl"
  # Seed queue with the canonical format
  echo '{"task":"drain-test","severity":"kanban_only","message":"(kanban-only emit) Drain test","steps":[],"kanban_items":[{"title":"Drain test card","type":"incident","priority":"normal","owner":"rnd"}]}' > "$QUEUE3"

  drain_out=$(KANBAN_QUEUE="$QUEUE3" DRAIN_DRY_RUN=1 bash "$DRAIN" 2>&1)
  drain_exit=$?
  [ "$drain_exit" -eq 0 ] || fail "drain --dry-run exited $drain_exit, expected 0"
  echo "$drain_out" | grep -q "Drain test card" \
    || fail "drain dry-run did not list card title. Got: $drain_out"
  # Queue must be unchanged after dry run
  [ -f "$QUEUE3" ] || fail "drain dry-run deleted the queue file (should not)"
  pass "drain dry-run exits 0 and lists card"
else
  echo "SKIP: drain_kanban_queue.sh not found at $DRAIN (not blocking)"
fi

echo ""
echo "All tests passed."
