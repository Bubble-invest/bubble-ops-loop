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

# _resolve_gh_token() now has a host:local (Mac) fallback that shells out to
# `ssh claude@joris-cx33` (board #463). On a dev Mac with real Tailscale access
# to the VPS, GH_TOKEN=bad_token_force_fail alone is no longer enough to force
# the dashboard-fallback path — the SSH step would succeed for real and this
# test would silently file a live card on the board. Shadow `ssh` (and `sudo`,
# used by the 2b minter fallback) with a no-op stub ahead of PATH so every
# host-specific auth path fails deterministically and this test stays hermetic.
STUBBIN="$TMPDIR_T/stubbin"
mkdir -p "$STUBBIN"
cat > "$STUBBIN/ssh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$STUBBIN/sudo" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUBBIN/ssh" "$STUBBIN/sudo"

stderr_output=$(
  PATH="$STUBBIN:$PATH" \
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
  PATH="$STUBBIN:$PATH" \
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

# ── Test 4: empty/whitespace GH_TOKEN in the env must NOT poison resolution ──
# Covers #536: Morty/VPS invoked emit as `GH_TOKEN="${GITHUB_TOKEN:-}" bash …`,
# exporting an EMPTY GH_TOKEN. An empty GH_TOKEN makes `gh auth status` exit 0
# while gh is actually UNauthenticated, so _resolve_gh_token() short-circuited
# with `return 0` and left the empty token in place → every gh call failed
# "Could not resolve to a Repository" → cards fell to the dead local queue.
# The guard at the top of _resolve_gh_token() must unset an empty/whitespace
# GH_TOKEN/GITHUB_TOKEN so step-2a (the real /run/bubble-board/token) is reached.
#
# We exercise the guard hermetically: stub `gh` so that `gh auth status` reports
# authed ONLY when a non-empty GH_TOKEN is present (mirrors real gh behaviour —
# an empty token is NOT authed). Extract just _resolve_gh_token from the emitter,
# seed a fake token file, and assert the empty-token case still resolves the
# real token from the file rather than early-returning on the poison.

# End-to-end via the REAL emitter (no fragile function extraction). We drive
# _gh_emit down its real code path by stubbing `gh`:
#   - `gh auth status` succeeds iff GH_TOKEN is non-empty (real gh semantics —
#     an empty token is NOT authed).
#   - `gh issue list` returns empty (no dup), `gh label list`/`gh issue create`
#     succeed and `create` prints a URL AND touches a marker file, proving the
#     GitHub path (not the dead queue) was taken.
# BOARD_TOKEN_FILE points step-2a at a seeded fake token. With an EMPTY GH_TOKEN
# in the env, the #536 guard must unset it so step-2a resolves the file token
# and the emit lands on the (stubbed) board — the marker file must appear.
STUB2="$TMPDIR_T/stub2"
mkdir -p "$STUB2"
MARKER="$TMPDIR_T/gh-create-was-called"
# Stub mirrors REAL gh's poison behaviour proven live (#536): with GH_TOKEN
# EXPORTED (even as ""), `gh auth status` exits 0 while gh is NOT usable, so any
# subsequent API call fails "Could not resolve to a Repository". Only a genuinely
# UNSET GH_TOKEN (or a real ghs_ value) makes gh actually work.
cat > "$STUB2/gh" <<EOF
#!/usr/bin/env bash
_tok_present() { [ -n "\${GH_TOKEN+x}" ]; }        # is GH_TOKEN SET (even if "")?
_tok_usable()  { case "\${GH_TOKEN:-}" in ghs_*|gho_*|ghp_*) return 0;; *) return 1;; esac; }
case "\${1:-} \${2:-}" in
  "auth status") _tok_present && exit 0 || exit 1 ;;   # empty token still "authed" → the trap
  "issue list")  _tok_usable && echo "" || { echo "GraphQL: Could not resolve to a Repository" >&2; exit 1; } ;;
  "label list")  _tok_usable && echo "" || exit 1 ;;
  "label create") _tok_usable && exit 0 || exit 1 ;;
  "issue create")
     _tok_usable || { echo "GraphQL: Could not resolve to a Repository" >&2; exit 1; }
     touch "$MARKER"
     echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99999" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB2/gh"

TOKFILE="$TMPDIR_T/board-token"
echo "ghs_faketokenfromfile" > "$TOKFILE"
QUEUE4="$TMPDIR_T/kanban_queue4.jsonl"

# Invoke EXACTLY the way Morty did: prefix an empty GH_TOKEN into the env.
PATH="$STUB2:$PATH" \
GH_TOKEN="${GITHUB_TOKEN:-}" \
BOARD_TOKEN_FILE="$TOKFILE" \
KANBAN_QUEUE="$QUEUE4" \
TELEGRAM_BOT_TOKEN="" \
BUBBLE_OPERATOR_CHAT_ID="" \
bash "$EMITTER" \
  task=test-536-empty-ghtoken \
  title="536 empty-GH_TOKEN guard card" \
  type=incident \
  owner=rnd >/dev/null 2>&1

[ -f "$MARKER" ] \
  || fail "empty GH_TOKEN poisoned resolution: gh issue create was NOT reached (the #536 bug — emit fell to the queue instead of the board)"
pass "empty GH_TOKEN is unset by the guard → emit reaches the board via step-2a token (#536)"

# The queue must NOT have caught this card (it went to the board, not the fallback).
if [ -f "$QUEUE4" ] && grep -q "536 empty-GH_TOKEN guard card" "$QUEUE4"; then
  fail "card fell to the local queue despite a valid file token — guard did not take effect"
fi
pass "card did NOT fall to the dead local queue"

echo ""
echo "All tests passed."
