#!/usr/bin/env bash
# test_1251_queue_path_agreement.sh — board #1251: emit_kanban_item.sh and
# drain_kanban_queue.sh must independently resolve to the SAME default queue
# path, without relying on an explicit $KANBAN_QUEUE override (every other
# test in this suite sets KANBAN_QUEUE explicitly, which bypasses the exact
# default-resolution logic that caused the original incident — a card queued
# under /tmp/claude-<uid>, a path nothing ever drains).
#
# Root cause (board #1251, observed on the morty box 2026-09-13): the old
# default candidate ladder guessed at $HOME-relative paths ($HOME/claude-
# workspaces/Rick_RnD/monitoring, $HOME/.bubble) that a sandboxed Bash-tool
# session cannot write to even though the dept uid owns $HOME at the Unix-
# permission level — the sandbox's writable filesystem is scoped to the
# project checkout (a dept's $BUBBLE_AGENT_WORKDIR, e.g. /srv/agents/morty)
# plus a session-scoped tmp dir. The fix: prefer $BUBBLE_AGENT_WORKDIR/memory
# — inside the sandbox-writable project tree, durable (not tmp), and NOT
# dependent on $HOME. Both scripts must agree on this by construction.
#
# Run: bash tests/test_1251_queue_path_agreement.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"
DRAIN="$REPO_ROOT/tools/kanban/drain_kanban_queue.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

# Hermetic gh/ssh/sudo stubs — force _resolve_gh_token down to the queue path
# deterministically, same rationale as the other emit tests in this suite.
STUBBIN="$TMPDIR_T/stubbin"
mkdir -p "$STUBBIN"
for bin in gh ssh sudo; do
  printf '#!/usr/bin/env bash\nexit 1\n' > "$STUBBIN/$bin"
  chmod +x "$STUBBIN/$bin"
done

# Simulate a dept's own persistent project checkout (what BUBBLE_AGENT_WORKDIR
# points at on the VPS, e.g. /srv/agents/morty) — writable, NOT a $HOME path,
# NOT /tmp.
WORKDIR="$TMPDIR_T/srv-agents-faketest"
mkdir -p "$WORKDIR"

# ── Test 1: emit, with NO $KANBAN_QUEUE set, lands under
#            $BUBBLE_AGENT_WORKDIR/memory/kanban_queue.jsonl ────────────────
env -i PATH="$STUBBIN:/usr/bin:/bin" \
  HOME="$TMPDIR_T/home-should-not-be-used" \
  BUBBLE_AGENT_WORKDIR="$WORKDIR" \
  TMPDIR="$TMPDIR_T/session-scoped-tmp-should-not-be-used" \
  bash "$EMITTER" \
    task=test-1251-workdir-queue \
    title="Queue lands under BUBBLE_AGENT_WORKDIR" \
    type=incident \
    owner=morty \
    budget=10 >/dev/null 2>&1
emit_exit=$?

[ "$emit_exit" -ne 0 ] || fail "expected non-zero exit (gh/dashboard both fail), got 0"
pass "emit exits non-zero when gh is unreachable (fail loud, board #1251)"

EXPECTED_QUEUE="$WORKDIR/memory/kanban_queue.jsonl"
[ -f "$EXPECTED_QUEUE" ] \
  || fail "queue file NOT found at \$BUBBLE_AGENT_WORKDIR/memory/kanban_queue.jsonl ($EXPECTED_QUEUE) — did it fall back to \$HOME or \$TMPDIR instead?"
pass "queue landed at \$BUBBLE_AGENT_WORKDIR/memory/kanban_queue.jsonl (durable, not \$HOME- or \$TMPDIR-relative)"

grep -q "Queue lands under BUBBLE_AGENT_WORKDIR" "$EXPECTED_QUEUE" \
  || fail "card title not found in the resolved queue file"
pass "card content present in the resolved queue file"

# It must NOT have landed in the decoy $HOME or $TMPDIR paths.
[ -f "$TMPDIR_T/home-should-not-be-used/claude-workspaces/Rick_RnD/monitoring/kanban_queue.jsonl" ] \
  && fail "card ALSO leaked into the old \$HOME-relative default — candidates should be mutually exclusive, not both written"
[ -f "$TMPDIR_T/session-scoped-tmp-should-not-be-used/kanban_queue.jsonl" ] \
  && fail "card fell through to the session-scoped \$TMPDIR path despite a usable \$BUBBLE_AGENT_WORKDIR"
pass "queue did not also/instead land in \$HOME or \$TMPDIR"

# ── Test 2: drain_kanban_queue.sh, under the SAME env, resolves to the
#            IDENTICAL path (the actual "both ends agree" assertion) ────────
DRAIN_OUT=$(
  env -i PATH="$STUBBIN:/usr/bin:/bin" \
    HOME="$TMPDIR_T/home-should-not-be-used" \
    BUBBLE_AGENT_WORKDIR="$WORKDIR" \
    DRAIN_DRY_RUN=1 \
    bash "$DRAIN" 2>&1
)
echo "$DRAIN_OUT" | grep -qF "$EXPECTED_QUEUE" \
  || fail "drain_kanban_queue.sh (same env, no \$KANBAN_QUEUE) did not resolve the SAME path emit used. Got: $DRAIN_OUT"
pass "drain_kanban_queue.sh resolves the IDENTICAL default path as emit_kanban_item.sh under the same env — both ends agree"

echo "$DRAIN_OUT" | grep -q "Queue lands under BUBBLE_AGENT_WORKDIR" \
  || fail "drain dry-run did not see the card emit actually queued. Got: $DRAIN_OUT"
pass "drain can see the exact card emit queued (no orphaned queue file)"

# ── Test 3: both ends STILL agree when $BUBBLE_AGENT_WORKDIR/memory exists
#            but is NOT writable (r1 adversarial review finding) ────────────
# emit's _resolve_queue_path falls through to candidate 3 ($HOME/claude-
# workspaces/Rick_RnD/monitoring) when candidate 2 isn't writable for the
# emitting session. drain's default resolution must make the SAME decision
# under the same env, or a card queued at candidate 3 would be reported as
# "queue not found" by a drain that blindly trusted candidate 2 — a narrower
# recurrence of #1251's actual failure mode.
UNWRITABLE_WORKDIR="$TMPDIR_T/unwritable-workdir"
mkdir -p "$UNWRITABLE_WORKDIR/memory"
chmod 555 "$UNWRITABLE_WORKDIR/memory"
RICK_HOME="$TMPDIR_T/home-for-fallback-test"
mkdir -p "$RICK_HOME"

env -i PATH="$STUBBIN:/usr/bin:/bin" \
  HOME="$RICK_HOME" \
  BUBBLE_AGENT_WORKDIR="$UNWRITABLE_WORKDIR" \
  TMPDIR="$TMPDIR_T/decoy-tmp" \
  bash "$EMITTER" \
    task=test-1251-unwritable-workdir \
    title="Falls through to Rick-monitoring when workdir unwritable" \
    type=incident \
    owner=morty \
    budget=10 >/dev/null 2>&1
emit_exit2=$?
# Deliberately do NOT restore write perms here — the drain assertion below
# needs the SAME unwritable state emit just saw. Removing a directory only
# needs write+execute on its PARENT (not on the 555 dir itself), so the
# trap's `rm -rf "$TMPDIR_T"` at script exit can still clean this up fine.

[ "$emit_exit2" -ne 0 ] || fail "expected non-zero exit, got 0"
EXPECTED_FALLBACK="$RICK_HOME/claude-workspaces/Rick_RnD/monitoring/kanban_queue.jsonl"
[ -f "$EXPECTED_FALLBACK" ] \
  || fail "emit did not fall through to the Rick-monitoring path when \$BUBBLE_AGENT_WORKDIR/memory was unwritable"
pass "emit falls through to candidate 3 when \$BUBBLE_AGENT_WORKDIR/memory is unwritable"

DRAIN_OUT2=$(
  env -i PATH="$STUBBIN:/usr/bin:/bin" \
    HOME="$RICK_HOME" \
    BUBBLE_AGENT_WORKDIR="$UNWRITABLE_WORKDIR" \
    DRAIN_DRY_RUN=1 \
    bash "$DRAIN" 2>&1
)
echo "$DRAIN_OUT2" | grep -qF "$EXPECTED_FALLBACK" \
  || fail "drain did NOT fall through to the same candidate-3 path under an unwritable workdir — it disagreed with emit. Got: $DRAIN_OUT2"
pass "drain also falls through to candidate 3 under the same unwritable-workdir env — still agrees with emit"

echo "$DRAIN_OUT2" | grep -q "Falls through to Rick-monitoring" \
  || fail "drain could not see the card that fell through to candidate 3. Got: $DRAIN_OUT2"
pass "drain can see the card that fell through to candidate 3 (no stranded queue)"

echo ""
echo "All tests passed."
