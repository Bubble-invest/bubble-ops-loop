#!/usr/bin/env bash
# test_1434_emit_normalize_intents_backtick.sh — regression test for board #1434.
#
# Bug: `.md: command not found` on stderr (deployed script's line 202) for
# EVERY emit_kanban_item.sh call that reaches _normalize_intents() — including
# a plain cron-failure-alert.sh emit with no intent= at all. Root cause: the
# _normalize_intents() body is a python3 -c "..." string that bash parses as a
# DOUBLE-QUOTED string (not a heredoc), and one of its python comments had
# backticks around `.md`. Bash evaluates backtick pairs inside double quotes
# as command substitution BEFORE handing the string to python3, so it tried
# to run a command literally named `.md` -> ".md: command not found" on
# stderr. This fires unconditionally (independent of $INTENTS content) since
# it's a static parse-time substitution, not something the input can avoid.
#
# This test drives the REAL emitter with the EXACT argument shape
# cron-failure-alert.sh uses (task=cron-failure-alert title=... body=...
# type=incident owner=rnd host=vps budget=5 context_url=...) and asserts
# stderr never contains "command not found" or the literal ".md" bareword
# error — hermetically, via the same ssh/sudo/gh stubbing style as
# test_emit_kanban_fallback_loud.sh / test_emit_budget_required.sh.
#
# Run: bash tests/test_1434_emit_normalize_intents_backtick.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

# Hermetic stubs — never touch the real board or a real Mac->VPS SSH hop.
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

# ── Test 1: cron-failure-alert.sh's exact argument shape, no intent= arg ─────
# (This is the shape observed in the board #1434 log line: task=cron-failure-alert,
# title/body free text, type=incident, owner=rnd, host=vps, budget=5,
# context_url=..., and NO intent= — proving the bug fires even with intent unset.)

QUEUE="$TMPDIR_T/kanban_queue.jsonl"
stderr_output=$(
  PATH="$STUBBIN:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_QUEUE="$QUEUE" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=cron-failure-alert \
    title="morty-agentic-audit.service failed (exit-code)" \
    body="Unit morty-agentic-audit.service entered failed state." \
    type=incident \
    owner=rnd \
    host=vps \
    budget=5 \
    context_url="https://github.com/Bubble-invest/bubble-ops-board" \
    2>&1 >/dev/null
)

echo "$stderr_output" | grep -qi "command not found" \
  && fail "stderr contains a 'command not found' error (#1434 regression). Got: $stderr_output"
pass "no 'command not found' on stderr for cron-failure-alert's exact argument shape"

echo "$stderr_output" | grep -qE '(^|[^A-Za-z0-9_/.-])\.md(:| )' \
  && fail "stderr contains a stray bare '.md' token (#1434 regression). Got: $stderr_output"
pass "no stray bare '.md' token on stderr"

# ── Test 2: same call, but WITH an intent= that legitimately ends in .md ────
# Exercises the actual code path the backticked comment was documenting (an
# intent value ending in .md must be silently dropped, not blow up the shell).

stderr_output2=$(
  PATH="$STUBBIN:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_QUEUE="$TMPDIR_T/kanban_queue2.jsonl" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=cron-failure-alert \
    title="second failure card" \
    type=incident \
    owner=rnd \
    host=vps \
    budget=5 \
    intent=some-page.md \
    2>&1 >/dev/null
)

echo "$stderr_output2" | grep -qi "command not found" \
  && fail "stderr contains a 'command not found' error with intent=some-page.md. Got: $stderr_output2"
pass "intent= ending in .md does not trigger a shell error"

echo "$stderr_output2" | grep -q "no usable intent= supplied" \
  || fail "a .md-suffixed intent should normalize to empty and warn as an orphan. Got: $stderr_output2"
pass "'.md'-suffixed intent is dropped (normalizes to the intent-orphan warning), not carried through"

echo ""
echo "All tests passed."
