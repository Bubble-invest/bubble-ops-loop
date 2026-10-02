#!/usr/bin/env bash
# test_1195_ask_handoff.sh — rotation rollout to agents WITHOUT an L4 session_handoff mission.
#   1. a HANDOFF.md with a fresh mtime but an old "(updated ...)" header stamp is STALE (SKIP).
#   2. --ask-handoff: the script appends ONE line to the inject file and, once the (fake) agent
#      rewrites HANDOFF.md, proceeds (dry-run never stops anything).
#   3. --ask-handoff with an agent that never answers -> SKIP (never rotates blind).
# Hermetic: Mac script run directly with --dry-run semantics for the proceed path; stub emitter.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAC="$REPO_ROOT/deploy/local/bubble-session-rotate-mac.sh"
FAILED=0; fail(){ echo "FAIL: $*" >&2; FAILED=1; }; pass(){ echo "PASS: $*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
W="$TMP/dept"; mkdir -p "$W/tools/kanban"; export HOME="$TMP/home"; mkdir -p "$HOME/Library/LaunchAgents"
touch "$HOME/Library/LaunchAgents/com.bubble.ops-loop-t.plist"
cat > "$W/tools/kanban/emit_kanban_item.sh" <<'E'
#!/usr/bin/env bash
echo "$*" >> "$STUB_EMIT_LOG"
E
chmod +x "$W/tools/kanban/emit_kanban_item.sh"; export STUB_EMIT_LOG="$TMP/emit.log"; : > "$STUB_EMIT_LOG"
INJ="$TMP/inject"; : > "$INJ"
OLD="$(date -u -v-3d +%Y-%m-%d 2>/dev/null || date -u -d '-3 days' +%Y-%m-%d)"
printf '# t - session handoff (updated %s 10:00 UTC)\n' "$OLD" > "$W/HANDOFF.md"   # fresh mtime, stale stamp
out=$(bash "$MAC" t --workdir "$W" --dry-run 2>&1); echo "$out" | grep -q "SKIP t:" && pass "stale header stamp SKIPs despite fresh mtime" || fail "stamp not honored: $out"

# stubs: nothing real may be stopped/started
mkdir -p "$TMP/bin"; printf '#!/bin/sh\necho "launchctl $*" >> "$STUB_LC_LOG"\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"
export STUB_LC_LOG="$TMP/lc.log"; : > "$STUB_LC_LOG"; export PATH="$TMP/bin:$PATH"; export TMUX_BIN="$TMP/bin/nonexistent-tmux"

# dry-run must NOT inject (no side effect on a live agent)
: > "$INJ"; ROTATE_IDLE_S=0 bash "$MAC" t --workdir "$W" --ask-handoff --inject-file "$INJ" --dry-run >/dev/null 2>&1
[[ ! -s "$INJ" ]] && pass "dry-run does not inject" || fail "dry-run injected"

# fake agent: when the inject file gets a line, rewrite HANDOFF.md with a fresh stamp
: > "$INJ"
( for i in $(seq 1 60); do if [[ -s "$INJ" ]]; then sleep 1; printf '# t - session handoff (updated %s UTC)\n' "$(date -u '+%Y-%m-%d %H:%M')" > "$W/HANDOFF.md"; exit 0; fi; sleep 1; done ) &
out=$(ROTATE_IDLE_S=0 ROTATE_ASK_WAIT_S=40 bash "$MAC" t --workdir "$W" --ask-handoff --inject-file "$INJ" 2>&1); wait
[[ "$(wc -l < "$INJ" | tr -d ' ')" == "1" ]] && pass "exactly one inject line written" || fail "inject lines != 1"
echo "$out" | grep -q "ask-handoff: HANDOFF.md rewritten" && pass "agent answered -> gate passes" || fail "no rewrite detected: $out"
grep -q "bootout" "$STUB_LC_LOG" && pass "proceeded to rotate (stubbed launchctl bootout)" || fail "did not proceed: $out"

# agent never answers -> SKIP, nothing stopped
printf '# t - session handoff (updated %s 10:00 UTC)\n' "$OLD" > "$W/HANDOFF.md"; : > "$STUB_LC_LOG"; : > "$INJ"
out=$(ROTATE_IDLE_S=0 ROTATE_ASK_WAIT_S=3 bash "$MAC" t --workdir "$W" --ask-handoff --inject-file "$INJ" 2>&1)
echo "$out" | grep -q "SKIP t:" && [[ ! -s "$STUB_LC_LOG" ]] && pass "silent agent -> SKIP, nothing stopped" || fail "silent agent case: $out"

# busy session (transcript just changed) -> SKIP, nothing injected
P="$HOME/.claude/projects/$(printf '%s' "$W" | LC_ALL=C tr -c 'A-Za-z0-9' '-')"; mkdir -p "$P"; echo '{}' > "$P/x.jsonl"
: > "$INJ"; out=$(ROTATE_IDLE_S=3600 ROTATE_ASK_WAIT_S=2 bash "$MAC" t --workdir "$W" --ask-handoff --inject-file "$INJ" 2>&1)
echo "$out" | grep -q "SKIP t:" && [[ ! -s "$INJ" ]] && pass "busy session -> SKIP without injecting" || fail "busy case: $out"
(( FAILED )) && { echo "SOME FAILED"; exit 1; } || echo "ALL PASS"
