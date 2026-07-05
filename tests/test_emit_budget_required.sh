#!/usr/bin/env bash
# test_emit_budget_required.sh — verify budget= is a MANDATORY emit input
# (board #537): every kanban card must carry a per-run USD budget so cost is
# attributable per card from creation.
#
# Covers:
#   (a) an emit call WITHOUT budget= fails LOUD (clear stderr error naming
#       budget=), exits 0 (the existing "never break the caller" convention),
#       and creates NO card at all — not on the board, not in the fallback
#       queue either (it's an invalid call, not a degraded emission).
#   (b) an emit call with a non-integer budget= (garbage, negative, zero) is
#       treated the same as missing — hard skip, no card.
#   (c) an emit call WITH a valid integer budget= still reaches the real emit
#       path (proven end-to-end via the fallback-to-queue path, same hermetic
#       stubbing style as test_emit_kanban_fallback_loud.sh).
#
# Run: bash tests/test_emit_budget_required.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

# Hermetic stubs — same rationale as test_emit_kanban_fallback_loud.sh: shadow
# ssh/sudo so every host-specific gh-auth path fails deterministically and this
# test never touches the real board or a real Mac->VPS SSH hop.
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

# ── Test (a): missing budget= fails loud, creates NO card ────────────────────

QUEUE_A="$TMPDIR_T/queue_a.jsonl"
out_a=$(
  PATH="$STUBBIN:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE_A" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-budget-required-a \
    title="Card with no budget must not be created" \
    type=incident \
    owner=rnd 2>&1 >/dev/null
)
exit_a=$?

[ "$exit_a" -eq 0 ] || fail "(a) exit code was $exit_a, expected 0 (fail loud, don't crash caller)"
pass "(a) exit code is 0 even though budget is missing"

echo "$out_a" | grep -q "budget= is required" \
  || fail "(a) expected a clear 'budget= is required' error on stderr. Got: $out_a"
pass "(a) stderr clearly names budget= as the missing/required field"

# No card must exist anywhere — not the board (can't verify network here, but
# the run never reached _gh_emit/_dashboard_emit) and NOT the fallback queue
# either, since this is an invalid call, not a degraded-but-valid emission.
[ ! -f "$QUEUE_A" ] || fail "(a) a card leaked into the fallback queue despite missing budget: $(cat "$QUEUE_A" 2>/dev/null)"
pass "(a) no card fell to the local fallback queue (invalid call, not a degraded emit)"

# ── Test (b): non-integer / zero / negative budget is a hard skip too ───────

for bad_budget in "abc" "0" "-5" "  " "12.5"; do
  QUEUE_B="$TMPDIR_T/queue_b_$(echo "$bad_budget" | tr -cd 'a-zA-Z0-9').jsonl"
  out_b=$(
    PATH="$STUBBIN:$PATH" \
    GH_TOKEN=bad_token_force_fail \
    KANBAN_HOST=localhost:19999 \
    KANBAN_QUEUE="$QUEUE_B" \
    TELEGRAM_BOT_TOKEN="" \
    BUBBLE_OPERATOR_CHAT_ID="" \
    bash "$EMITTER" \
      task=test-budget-required-b \
      title="Card with bad budget=$bad_budget" \
      type=incident \
      owner=rnd \
      "budget=$bad_budget" 2>&1 >/dev/null
  )
  exit_b=$?
  [ "$exit_b" -eq 0 ] || fail "(b) budget='$bad_budget': exit code was $exit_b, expected 0"
  echo "$out_b" | grep -q "budget=.*is required" \
    || fail "(b) budget='$bad_budget': expected 'budget= ... is required' error. Got: $out_b"
  [ ! -f "$QUEUE_B" ] || fail "(b) budget='$bad_budget': card leaked into fallback queue"
done
pass "(b) non-integer/zero/negative/blank budgets are all hard-rejected, no card created"

# ── Test (c): valid integer budget= reaches the real emit path end-to-end ───
# Same hermetic style as test_emit_kanban_fallback_loud.sh Test 1: gh auth
# fails (stubbed), dashboard POST fails (bad host), so the card lands in the
# local fallback queue — proving the call got PAST the budget gate and into
# the real emission logic.

QUEUE_C="$TMPDIR_T/queue_c.jsonl"
out_c=$(
  PATH="$STUBBIN:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE_C" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-budget-required-c \
    title="Card with valid budget reaches emit path" \
    type=incident \
    owner=rnd \
    budget=15 2>&1 >/dev/null
)
exit_c=$?

[ "$exit_c" -eq 0 ] || fail "(c) exit code was $exit_c, expected 0"
pass "(c) exit code is 0 with a valid budget"

echo "$out_c" | grep -qv "budget= is required" \
  || true  # sanity only; the real assertion is the queue file below
if echo "$out_c" | grep -q "budget= is required"; then
  fail "(c) valid budget=15 was incorrectly rejected. Got: $out_c"
fi
pass "(c) valid integer budget is NOT rejected by the budget gate"

[ -f "$QUEUE_C" ] || fail "(c) valid-budget emit did not reach the fallback queue (never got past the gate?)"
grep -q "Card with valid budget reaches emit path" "$QUEUE_C" \
  || fail "(c) card title not found in queue. Queue: $(cat "$QUEUE_C")"
pass "(c) valid-budget emit reaches the real emit path end-to-end (card lands in fallback queue)"

# ── Test (d): a bare integer budget (no leading \$) is accepted, matching the
# existing render logic's \$-stripping convention ────────────────────────────

QUEUE_D="$TMPDIR_T/queue_d.jsonl"
bash_d_out=$(
  PATH="$STUBBIN:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE_D" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-budget-required-d \
    title="Card with dollar-prefixed budget" \
    type=incident \
    owner=rnd \
    'budget=$20' 2>&1 >/dev/null
)
[ -f "$QUEUE_D" ] || fail "(d) budget=\$20 (dollar-prefixed) was rejected. Output: $bash_d_out"
pass "(d) a \$-prefixed integer budget (e.g. budget=\$20) is accepted"

# ── Test (e): budget-reject fires a LOUD Telegram alert (loud-drop, not silent) ─
# A fire-and-forget cron/agent tick has no human watching stderr, so a
# silently-dropped card is the wrong failure mode for #537. The reject path must
# ALSO fire a best-effort Telegram alert (same _kanban_queue_alert pattern).
# We stub `curl` (shadowing PATH, same style as the ssh/sudo stubs) so the alert
# never hits the network: the stub touches a marker file when it sees a Telegram
# sendMessage call. We assert:
#   (e1) with a STUB TELEGRAM_BOT_TOKEN + chat id set, the reject path attempts
#        the alert (marker touched), AND still creates no card.
#   (e2) with TELEGRAM_BOT_TOKEN="" the reject path does NOT attempt a send
#        (stays hermetic — the guard no-ops), so the marker is NOT touched.
STUBBIN_E="$TMPDIR_T/stubbin_e"
mkdir -p "$STUBBIN_E"
# Re-provide the ssh/sudo no-op stubs alongside the curl stub so gh-auth still
# fails deterministically on this PATH too.
cp "$STUBBIN/ssh" "$STUBBIN/sudo" "$STUBBIN_E/"
TG_MARKER="$TMPDIR_T/telegram-alert-was-attempted"
cat > "$STUBBIN_E/curl" <<EOF
#!/usr/bin/env bash
# No-op curl stub: if this is a Telegram sendMessage call, record that the alert
# was attempted, then succeed quietly. Never touches the network.
for a in "\$@"; do
  case "\$a" in
    *api.telegram.org*sendMessage*) touch "$TG_MARKER" ;;
  esac
done
exit 0
EOF
chmod +x "$STUBBIN_E/curl"

# (e1) STUB token + chat id set → alert attempted, still no card.
QUEUE_E1="$TMPDIR_T/queue_e1.jsonl"
rm -f "$TG_MARKER"
out_e1=$(
  PATH="$STUBBIN_E:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE_E1" \
  TELEGRAM_BOT_TOKEN="stub-bot-token" \
  BUBBLE_OPERATOR_CHAT_ID="stub-chat-id" \
  bash "$EMITTER" \
    task=test-budget-required-e1 \
    title="No-budget card must alert loudly" \
    type=incident \
    owner=rnd 2>&1 >/dev/null
)
exit_e1=$?
[ "$exit_e1" -eq 0 ] || fail "(e1) exit code was $exit_e1, expected 0"
[ -f "$TG_MARKER" ] || fail "(e1) budget-reject did NOT attempt a Telegram alert (marker not touched). Got: $out_e1"
[ ! -f "$QUEUE_E1" ] || fail "(e1) a card leaked into the fallback queue despite missing budget"
pass "(e1) budget-reject fires a Telegram alert (loud drop) when a token+chat are set, and still creates no card"

# (e2) TELEGRAM_BOT_TOKEN="" → guard no-ops, no send attempted (stays hermetic).
QUEUE_E2="$TMPDIR_T/queue_e2.jsonl"
rm -f "$TG_MARKER"
out_e2=$(
  PATH="$STUBBIN_E:$PATH" \
  GH_TOKEN=bad_token_force_fail \
  KANBAN_HOST=localhost:19999 \
  KANBAN_QUEUE="$QUEUE_E2" \
  TELEGRAM_BOT_TOKEN="" \
  BUBBLE_OPERATOR_CHAT_ID="" \
  bash "$EMITTER" \
    task=test-budget-required-e2 \
    title="No-budget card with empty token must stay silent" \
    type=incident \
    owner=rnd 2>&1 >/dev/null
)
exit_e2=$?
[ "$exit_e2" -eq 0 ] || fail "(e2) exit code was $exit_e2, expected 0"
[ ! -f "$TG_MARKER" ] || fail "(e2) alert was attempted despite empty TELEGRAM_BOT_TOKEN — guard failed, test is not hermetic"
echo "$out_e2" | grep -q "budget= is required" \
  || fail "(e2) expected the stderr 'budget= is required' error even with empty token. Got: $out_e2"
pass "(e2) empty TELEGRAM_BOT_TOKEN → no Telegram send attempted (guard respected, stays hermetic)"

echo ""
echo "All budget-required tests passed."
