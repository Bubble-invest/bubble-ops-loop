#!/usr/bin/env bash
# =============================================================================
# test_861_tick_lock.sh — RED/GREEN harness for board #861:
#
#   "loop-backup races a live in-flight tick: any subagent over 4 min
#    double-runs" — the floor's inject->`claude -p` fallback fires with NO
#    live-side lock, so a live tick whose late steps take >240s gets a SECOND,
#    headless tick on the SAME due-set (duplicate Layer-3 order / Layer-4
#    letter).
#
# THE FIX: the live tick now holds the SAME flock loop-backup.sh's
# run_backup_tick() already takes (`${LOCK_DIR}/ops-loop-<slug>.tick.lock`,
# see scripts/loop-backup.sh — UNCHANGED by this fix) for the duration of its
# tick, via the new scripts/tick-lock.sh. This file does NOT touch
# loop-backup.sh's own locking (already correct) — it proves the NEW
# tick-lock.sh correctly integrates with it:
#
#   1 (RED)   — reproduces the bug's precondition: with NOTHING holding the
#               live-tick lock (today's actual behavior — a live tick never
#               locks), a stale-heartbeat dept's floor fallback FIRES a
#               competing headless tick. This is exactly the race: a live
#               tick busy past the 240s inject-wait window looks "stale" to
#               the floor and gets doubled.
#   2 (GREEN) — with scripts/tick-lock.sh acquire held (simulating STEP 1 of
#               a live tick that is still mid-flight), the SAME floor run now
#               SKIPS — run_backup_tick's existing flock -n sees the lock
#               held and returns 99 (no tick, no notify).
#   3 (FAIL-SAFE) — SIGKILL the lock holder (simulating the live session
#               CRASHING mid-tick, with `release` never called). The lock is
#               free again immediately (kernel-level flock release on process
#               exit) and the VERY NEXT floor run fires normally — a
#               crashed/stale lock must NEVER permanently block a legitimate
#               tick.
#   4         — explicit acquire -> release -> floor fires again immediately
#               (the clean-shutdown path, not just the crash path).
#   5         — acquire() itself never blocks even when the lock is already
#               held by someone else (bounded wait, always returns 0).
#
# Hermetic: fake dept workdir under a tmpdir; LOCK_DIR + AGENTS_ROOT
# redirected into the tmpdir; `claude` and `systemctl` are stubbed (no real
# tick, no real systemd). Requires bash >= 4 (loop-backup.sh itself uses
# `${VAR,,}`) — on macOS with only the stock bash 3.2, run with a newer bash
# on PATH, e.g. `/opt/homebrew/bin/bash tests/test_861_tick_lock.sh`.
#
# Run:  bash tests/test_861_tick_lock.sh
#       bash tests/test_861_tick_lock.sh -v     # verbose (show script stderr/out)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$REPO_ROOT/scripts/loop-backup.sh}"
TICK_LOCK="${TICK_LOCK:-$REPO_ROOT/scripts/tick-lock.sh}"
PY="${BUBBLE_OPS_LOOP_PY:-$REPO_ROOT/venv/bin/python}"

[[ -f "$SCRIPT"    ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }
[[ -f "$TICK_LOCK" ]] || { echo "FATAL: script not found: $TICK_LOCK"; exit 2; }
[[ -x "$PY"        ]] || { echo "FATAL: venv python not found: $PY (run: python3 -m venv venv && venv/bin/pip install pyyaml)"; exit 2; }
if [[ -z "${BASH_VERSINFO:-}" || "${BASH_VERSINFO[0]}" -lt 4 ]]; then
    echo "FATAL: this harness (and loop-backup.sh's \${VAR,,} usage) needs bash >= 4. Re-run with a newer bash, e.g.:"
    echo "  /opt/homebrew/bin/bash $0 $*"
    exit 2
fi

# ── GNU/BSD date+touch portability shim (mirrors tests/test_loop_backup.sh) ──
if date -d "@0" >/dev/null 2>&1; then
    touch_at_epoch() { touch -d "@$1" "$2"; }
else
    touch_at_epoch() { touch -t "$(date -r "$1" "+%Y%m%d%H%M.%S")" "$2"; }
fi

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── stubs (claude + systemctl) ────────────────────────────────────────────────
CLAUDE_STUB="$WORK/claude-stub.sh"
CLAUDE_LOG="$WORK/claude-invocations.log"
: > "$CLAUDE_LOG"
cat > "$CLAUDE_STUB" <<EOF
#!/usr/bin/env bash
echo "CLAUDE_STUB_RAN" >> "$CLAUDE_LOG"
echo '{"type":"result","result":"stub ok"}'
exit 0
EOF
chmod +x "$CLAUDE_STUB"

SYSTEMCTL_STUB="$WORK/systemctl-stub.sh"
ENABLED_FILE="$WORK/enabled-depts.txt"
: > "$ENABLED_FILE"
cat > "$SYSTEMCTL_STUB" <<EOF
#!/usr/bin/env bash
# "show ... MainPID" -> 0 (no live poller found) so inject_live_loop() bails
# out immediately (return 1) instead of doing a real pgrep/cgroup scan or
# waiting up to 240s — the floor falls straight through to run_backup_tick(),
# which is the exact function whose flock we are testing.
if [[ "\$1" == "show" ]]; then
    echo 0
    exit 0
fi
if [[ "\$1" == "is-enabled" ]]; then
    unit="\$2"
    slug="\${unit#ops-loop-}"; slug="\${slug%.service}"
    enabled="\$(cat "$ENABLED_FILE" 2>/dev/null || true)"
    for e in \$enabled; do
        if [[ "\$e" == "\$slug" ]]; then echo "enabled"; exit 0; fi
    done
    echo "disabled"; exit 1
fi
exit 0
EOF
chmod +x "$SYSTEMCTL_STUB"

set_enabled() { printf '%s\n' "$*" > "$ENABLED_FILE"; }

# ── fixtures ─────────────────────────────────────────────────────────────────
AGENTS_ROOT="$WORK/agents"
mkdir -p "$AGENTS_ROOT"

make_dept() {
    # make_dept <slug> <age_seconds>  — stale-looking heartbeat (mimics a
    # live tick that has been running long enough that the floor considers
    # it stale, i.e. exactly the incident's precondition: the tick IS alive,
    # it just hasn't written a fresh heartbeat yet).
    local slug="$1" age="$2"
    local wd="$AGENTS_ROOT/bubble-ops-$slug"
    local today; today="$(date -u +%Y-%m-%d)"
    local hbdir="$wd/outputs/$today"
    mkdir -p "$hbdir"
    local hb="$hbdir/heartbeat.log"
    echo "heartbeat" > "$hb"
    touch_at_epoch "$(( $(date -u +%s) - age ))" "$hb"
}

LOCK_DIR="$WORK/lock"
mkdir -p "$LOCK_DIR"

common_env() {
    export BUBBLE_OPS_LOOP_ROOT="$REPO_ROOT"
    export BUBBLE_BACKUP_LOG="$WORK/loop-backup.jsonl"
    export BUBBLE_BACKUP_STALE_SEC=5400          # 90 min (default)
    export BUBBLE_BACKUP_CLAUDE_BIN="$CLAUDE_STUB"
    export BUBBLE_BACKUP_NOTIFY_CMD="$WORK/no-notify.sh"
    export BUBBLE_BACKUP_TELEGRAM_CHAT_ID="9999"
    export BUBBLE_BACKUP_AGENTS_ROOT="$AGENTS_ROOT"
    export BUBBLE_BACKUP_LOCK_DIR="$LOCK_DIR"     # ← the lock BOTH scripts share
    export BUBBLE_BACKUP_SYSTEMCTL="$SYSTEMCTL_STUB"
    export BUBBLE_TICK_LOCK_PY="python3"
    unset BUBBLE_BACKUP_DEPTS
}
cat > "$WORK/no-notify.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$WORK/no-notify.sh"

OUT=""; ERR=""; ALL=""; RC=0
run_script() {
    local script="$1"; shift
    if [[ $VERBOSE == 1 ]]; then
        bash "$script" "$@" > "$WORK/out" 2> >(tee "$WORK/err" >&2); RC=$?
    else
        bash "$script" "$@" > "$WORK/out" 2> "$WORK/err"; RC=$?
    fi
    OUT="$(cat "$WORK/out")"; ERR="$(cat "$WORK/err")"
    ALL="$OUT
$ERR"
}

run_tick_lock() {
    bash "$TICK_LOCK" "$@"
}

reset_claude_log() { : > "$CLAUDE_LOG"; }
fired() { [[ "$(cat "$CLAUDE_LOG")" == *"CLAUDE_STUB_RAN"* ]]; }

echo "== board #861 tick-lock RED/GREEN =="

# =============================================================================
# Fixture: one stale (3h > 90min threshold), enabled dept: "livewire" — the
# floor considers it eligible for a backup fallback on every run below.
# =============================================================================
SLUG="livewire"
make_dept "$SLUG" 10800
set_enabled "$SLUG"
common_env
export BUBBLE_BACKUP_DEPTS="$SLUG"

# -----------------------------------------------------------------------------
# 1. RED — nothing holds the tick-lock (today's real live-tick behavior:
#    inject_live_loop's systemctl stub reports no live poller, so the floor
#    falls straight to run_backup_tick, and NOTHING stops it since the live
#    tick never locks). This reproduces the bug's precondition.
# -----------------------------------------------------------------------------
reset_claude_log
run_tick_lock status "$SLUG" >/dev/null 2>&1
if [[ $? -ne 1 ]]; then
    bad "PRECOND lock should start FREE for $SLUG"
else
    ok "PRECOND lock starts FREE for $SLUG"
fi

run_script "$SCRIPT"
if fired; then
    ok "1-RED floor FIRES a backup tick when nothing holds the live-tick lock (reproduces the double-fire precondition)"
else
    bad "1-RED expected the floor to fire (baseline reproduction failed); out=$ALL"
fi

# -----------------------------------------------------------------------------
# 2. GREEN — simulate STEP 1 of a live tick that is still mid-flight: it has
#    acquired the SAME lock and not yet released it. The floor must now SKIP.
# -----------------------------------------------------------------------------
reset_claude_log
# Re-stale the heartbeat: run_backup_tick's own write_external_heartbeat step
# in test 1 above just made it look FRESH, which would skip this run for the
# WRONG reason (freshness) and mask what we're actually testing (the lock).
# Isolate the lock as the ONLY variable.
make_dept "$SLUG" 10800
run_tick_lock acquire "$SLUG" >/dev/null 2>&1
if run_tick_lock status "$SLUG" >/dev/null 2>&1; then
    ok "2-setup tick-lock acquired for $SLUG (simulating an in-flight live tick)"
else
    bad "2-setup tick-lock acquire did not take effect"
fi

run_script "$SCRIPT"
if fired; then
    bad "2-GREEN expected the floor to SKIP while the live-tick lock is held; it fired instead (fix did not take effect); out=$ALL"
else
    ok "2-GREEN floor SKIPS the competing backup tick while the live-tick lock is held (fix confirmed)"
fi
if grep -q "lock held" "$WORK/loop-backup.jsonl" 2>/dev/null; then
    ok "2-GREEN skip event log records 'lock held (concurrent tick)'"
else
    bad "2-GREEN expected a 'lock held' skip event in $WORK/loop-backup.jsonl; got: $(cat "$WORK/loop-backup.jsonl" 2>/dev/null)"
fi

# -----------------------------------------------------------------------------
# 3. FAIL-SAFE — the live session CRASHES mid-tick (SIGKILL the holder;
#    `release` is NEVER called). The lock must free itself IMMEDIATELY
#    (kernel-level flock release on process exit, not the pidfile bookkeeping)
#    so the very next floor run is NOT permanently blocked.
# -----------------------------------------------------------------------------
HOLDER_PID="$(run_tick_lock status "$SLUG" | sed -n 's/held pid=\([0-9]*\).*/\1/p')"
if [[ -n "$HOLDER_PID" ]]; then
    ok "3-setup holder pid captured ($HOLDER_PID)"
else
    bad "3-setup could not read holder pid from tick-lock status"
fi
kill -9 "$HOLDER_PID" 2>/dev/null
# No sleep, no wait for the belt-and-suspenders self-expiry timer — the
# kernel drops flock() the instant the process dies. If this needed a delay
# to pass, that would itself be evidence the crash-safety property is fake.
if run_tick_lock status "$SLUG" >/dev/null 2>&1; then
    bad "3-FAIL-SAFE lock still reports held after the holder was SIGKILLed"
else
    ok "3-FAIL-SAFE lock is FREE immediately after the holder is SIGKILLed (crash-safe)"
fi

reset_claude_log
make_dept "$SLUG" 10800   # re-stale: isolate the lock as the only variable
run_script "$SCRIPT"
if fired; then
    ok "3-FAIL-SAFE floor fires normally again right after a crashed live tick — a stale/crashed lock never permanently blocks a legitimate tick"
else
    bad "3-FAIL-SAFE expected the floor to fire after the crash freed the lock; it stayed blocked (dept would silently stop ticking forever); out=$ALL"
fi

# -----------------------------------------------------------------------------
# 4. Clean path — explicit acquire -> release (not a crash) also frees the
#    lock immediately, and the floor is not left waiting on anything.
# -----------------------------------------------------------------------------
run_tick_lock acquire "$SLUG" >/dev/null 2>&1
run_tick_lock release "$SLUG" >/dev/null 2>&1
if run_tick_lock status "$SLUG" >/dev/null 2>&1; then
    bad "4-clean-release lock still held after an explicit release"
else
    ok "4-clean-release lock is free immediately after an explicit release"
fi

reset_claude_log
make_dept "$SLUG" 10800   # re-stale: isolate the lock as the only variable
run_script "$SCRIPT"
if fired; then
    ok "4-clean-release floor fires normally after a clean acquire+release cycle"
else
    bad "4-clean-release expected the floor to fire after a clean release; out=$ALL"
fi

# -----------------------------------------------------------------------------
# 5. acquire() must NEVER block the caller, even when the lock is genuinely
#    contended by someone else (e.g. a backup -p tick itself mid-run). Bound
#    the wall-clock time to prove this is a bounded poll, not an accidental
#    blocking wait.
# -----------------------------------------------------------------------------
run_tick_lock acquire "$SLUG" >/dev/null 2>&1   # first holder takes it
START_EPOCH=$(date -u +%s)
run_tick_lock acquire "$SLUG" >/dev/null 2>&1   # second acquire: same slug -> idempotent no-op
RC5A=$?
END_EPOCH=$(date -u +%s)
ELAPSED=$(( END_EPOCH - START_EPOCH ))
if [[ $RC5A -eq 0 && $ELAPSED -le 5 ]]; then
    ok "5-never-blocks a same-slug re-acquire returns 0 promptly (${ELAPSED}s, idempotent no-op)"
else
    bad "5-never-blocks re-acquire took too long or failed (rc=$RC5A elapsed=${ELAPSED}s)"
fi
run_tick_lock release "$SLUG" >/dev/null 2>&1

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
