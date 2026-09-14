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

echo ""
echo "All tests passed."
