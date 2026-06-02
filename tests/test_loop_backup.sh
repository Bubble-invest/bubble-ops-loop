#!/usr/bin/env bash
# =============================================================================
# test_loop_backup.sh — WS4 bash harness for loop-backup.sh (TDD).
#
# Covers the two WS4 changes to scripts/loop-backup.sh:
#
#   A. DRY_RUN footgun fix — the script must honor BOTH `BUBBLE_BACKUP_DRY_RUN`
#      AND the bare `DRY_RUN` people actually type, resolve a sane precedence,
#      and LOG IT LOUDLY on startup ("DRY_RUN resolved to <0|1> from <var>").
#      Red->green proof: the PRE-WS4 script (scripts/loop-backup.sh.orig, if
#      present) IGNORES `DRY_RUN` → still spends a tick → test RED; the fixed
#      script honors it → test GREEN.
#
#   B. Notify-on-fire — when a backup tick ACTUALLY RUNS for a dept (stale /
#      missing heartbeat), the script pings Joris ONCE on Telegram; on a fresh
#      (healthy) loop it must NOT ping. The Telegram send + the claude tick are
#      stubbed (no live HTTP, no real `claude -p`): BUBBLE_BACKUP_NOTIFY_CMD
#      captures the would-be ping, BUBBLE_BACKUP_CLAUDE_BIN is a no-op stub.
#
# Hermetic: fake dept workdirs with controlled heartbeat fixtures under a
# tmpdir; LOCK_DIR + BACKUP_LOG redirected into the tmpdir. Uses the REAL
# repo venv python + the REAL scripts/lib/loop_backup.py decision logic (so the
# freshness gate is exercised for real). No live Telegram, no live claude.
#
# Run:  bash tests/test_loop_backup.sh
#       bash tests/test_loop_backup.sh -v     # verbose (show script stderr/out)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$REPO_ROOT/scripts/loop-backup.sh}"
ORIG_SCRIPT="${ORIG_SCRIPT:-$REPO_ROOT/scripts/loop-backup.sh.orig}"  # pre-WS4 (optional)
PY="${BUBBLE_OPS_LOOP_PY:-$REPO_ROOT/venv/bin/python}"

[[ -f "$SCRIPT" ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }
[[ -x "$PY"     ]] || { echo "FATAL: venv python not found: $PY"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── stubs ────────────────────────────────────────────────────────────────────
# claude stub: a no-op that succeeds, so the run-branch completes WITHOUT a real
# tick. Records each invocation so we can assert a tick "ran".
CLAUDE_STUB="$WORK/claude-stub.sh"
CLAUDE_LOG="$WORK/claude-invocations.log"
: > "$CLAUDE_LOG"
cat > "$CLAUDE_STUB" <<EOF
#!/usr/bin/env bash
echo "CLAUDE_STUB_RAN" >> "$CLAUDE_LOG"
echo '{"result":"stub ok"}'
exit 0
EOF
chmod +x "$CLAUDE_STUB"

# notify stub: capture (slug, chat_id, msg) of every backup-fired ping. NO HTTP.
NOTIFY_STUB="$WORK/notify-stub.sh"
NOTIFY_LOG="$WORK/notify.log"
: > "$NOTIFY_LOG"
cat > "$NOTIFY_STUB" <<EOF
#!/usr/bin/env bash
# args: slug chat_id msg
printf '%s\t%s\t%s\n' "\$1" "\$2" "\$3" >> "$NOTIFY_LOG"
exit 0
EOF
chmod +x "$NOTIFY_STUB"

# ── fixtures ─────────────────────────────────────────────────────────────────
# Build a fake dept workdir at $AGENTS_ROOT/bubble-ops-<slug> with a heartbeat
# whose mtime/content age is controlled. We mirror the real liveness signal:
# outputs/<UTC-date>/heartbeat.log (latest_heartbeat_epoch reads file mtime).
AGENTS_ROOT="$WORK/agents"
mkdir -p "$AGENTS_ROOT"

make_dept() {
    # make_dept <slug> <age_seconds|none>
    local slug="$1" age="$2"
    local wd="$AGENTS_ROOT/bubble-ops-$slug"
    local today; today="$(date -u +%Y-%m-%d)"
    local hbdir="$wd/outputs/$today"
    mkdir -p "$hbdir"
    if [[ "$age" == "none" ]]; then
        return 0   # no heartbeat file at all → decision = run (no evidence)
    fi
    local hb="$hbdir/heartbeat.log"
    echo "heartbeat" > "$hb"
    # backdate the file mtime by <age> seconds so latest_heartbeat_epoch sees it stale/fresh
    touch -d "@$(( $(date -u +%s) - age ))" "$hb"
}

# Common env to point the script entirely inside the tmpdir.
common_env() {
    export BUBBLE_OPS_LOOP_ROOT="$REPO_ROOT"           # real venv + lib
    export BUBBLE_BACKUP_LOG="$WORK/loop-backup.jsonl"  # event log → tmp
    export BUBBLE_BACKUP_STALE_SEC=5400                # 90 min (default)
    export BUBBLE_BACKUP_CLAUDE_BIN="$CLAUDE_STUB"     # no real tick
    export BUBBLE_BACKUP_NOTIFY_CMD="$NOTIFY_STUB"     # no real Telegram
    export BUBBLE_BACKUP_TELEGRAM_CHAT_ID="9999"       # deterministic recipient
    export BUBBLE_BACKUP_AGENTS_ROOT="$AGENTS_ROOT"    # fake dept workdirs (tmp)
    export BUBBLE_BACKUP_LOCK_DIR="$WORK/lock"         # flock dir → tmp
    mkdir -p "$WORK/lock"
}

OUT=""; ERR=""; ALL=""; RC=0
run_script() {
    # run_script <script_path>. Runs in the CURRENT shell (not a subshell) so
    # captured OUT/ERR/ALL/RC are visible to the assertions. Callers set/unset
    # the DRY_RUN env vars around the call themselves.
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

# Set the two dry-run env vars for ONE run, then restore. Usage:
#   with_dryrun "<BUBBLE_BACKUP_DRY_RUN or -unset->" "<DRY_RUN or -unset->" SCRIPT
with_dryrun() {
    local canon="$1" bare="$2" script="$3"
    if [[ "$canon" == "-unset-" ]]; then unset BUBBLE_BACKUP_DRY_RUN; else export BUBBLE_BACKUP_DRY_RUN="$canon"; fi
    if [[ "$bare"  == "-unset-" ]]; then unset DRY_RUN;               else export DRY_RUN="$bare"; fi
    run_script "$script"
    unset BUBBLE_BACKUP_DRY_RUN DRY_RUN
}

echo "== WS4 loop-backup.sh tests =="

# =============================================================================
# A. DRY_RUN footgun — resolution + loud log + precedence
# =============================================================================
# We run with a non-existent dept so the script never reaches a real tick
# (workdir missing → SKIP), isolating the startup DRY_RUN-resolution banner.
common_env
export BUBBLE_BACKUP_DEPTS="nonexistent-dept-xyz"

# A1: bare DRY_RUN=1 is HONORED (the footgun fix) + loud log line present.
with_dryrun -unset- 1 "$SCRIPT"
if [[ "$ALL" == *"DRY_RUN resolved to 1 from DRY_RUN"* ]]; then
    ok "A1 bare DRY_RUN=1 honored + loud log ('resolved to 1 from DRY_RUN')"
else
    bad "A1 expected 'DRY_RUN resolved to 1 from DRY_RUN'; got: $ALL"
fi

# A2: canonical BUBBLE_BACKUP_DRY_RUN=1 honored + sourced-from canonical.
with_dryrun 1 -unset- "$SCRIPT"
if [[ "$ALL" == *"DRY_RUN resolved to 1 from BUBBLE_BACKUP_DRY_RUN"* ]]; then
    ok "A2 canonical BUBBLE_BACKUP_DRY_RUN=1 honored + loud log"
else
    bad "A2 expected 'resolved to 1 from BUBBLE_BACKUP_DRY_RUN'; got: $ALL"
fi

# A3: precedence — canonical wins even when it's 0 and bare DRY_RUN=1.
with_dryrun 0 1 "$SCRIPT"
if [[ "$ALL" == *"DRY_RUN resolved to 0 from BUBBLE_BACKUP_DRY_RUN"* ]]; then
    ok "A3 precedence: canonical=0 wins over bare DRY_RUN=1"
else
    bad "A3 expected 'resolved to 0 from BUBBLE_BACKUP_DRY_RUN'; got: $ALL"
fi

# A4: default — neither var set → resolved to 0 from default.
with_dryrun -unset- -unset- "$SCRIPT"
if [[ "$ALL" == *"DRY_RUN resolved to 0 from default"* ]]; then
    ok "A4 neither var set → 'resolved to 0 from default'"
else
    bad "A4 expected 'resolved to 0 from default'; got: $ALL"
fi

# A5: truthy alias — bare DRY_RUN=true normalizes to 1.
with_dryrun -unset- true "$SCRIPT"
if [[ "$ALL" == *"DRY_RUN resolved to 1 from DRY_RUN"* ]]; then
    ok "A5 DRY_RUN=true normalizes to 1"
else
    bad "A5 expected truthy 'true' → 1; got: $ALL"
fi

# A6 (RED->GREEN proof): the PRE-WS4 script IGNORES bare DRY_RUN. Demonstrate
# that the old behavior is different — old script has NO 'DRY_RUN resolved'
# banner at all (the fix introduced it). Only runs if the .orig is present.
if [[ -f "$ORIG_SCRIPT" ]]; then
    with_dryrun -unset- 1 "$ORIG_SCRIPT"
    if [[ "$ALL" != *"DRY_RUN resolved to"* ]]; then
        ok "A6 RED->GREEN: pre-WS4 script has NO 'DRY_RUN resolved' banner (footgun present)"
    else
        bad "A6 pre-WS4 script unexpectedly logged a DRY_RUN banner: $ALL"
    fi
else
    echo "  SKIP: A6 (no $ORIG_SCRIPT to prove red->green against)"
fi

# =============================================================================
# B. Notify-on-fire — one ping per STALE dept, none for FRESH depts.
# =============================================================================
# Hermetic: BUBBLE_BACKUP_AGENTS_ROOT shadows the dept workdir base into the
# tmpdir; claude + Telegram are stubbed. We seed three depts:
#   fresh  — heartbeat 10 min old (< 90m stale)  → SKIP  → no ping
#   stale  — heartbeat 3h old      (> 90m stale)  → RUN   → one ping
#   never  — no heartbeat at all                  → RUN   → one ping
common_env
make_dept fresh 600
make_dept stale 10800
make_dept never none
export BUBBLE_BACKUP_DEPTS="fresh stale never"

: > "$NOTIFY_LOG"
with_dryrun -unset- -unset- "$SCRIPT"

# B1: exactly the two stale-ish depts (stale + never) pinged, fresh did NOT.
pinged="$(cut -f1 "$NOTIFY_LOG" | sort | tr '\n' ' ')"
nlines="$(grep -c . "$NOTIFY_LOG" || true)"
if [[ "$nlines" == "2" && "$pinged" == "never stale " ]]; then
    ok "B1 one ping per stale dept (stale+never), NONE for fresh"
else
    bad "B1 expected pings={never,stale} (2); got n=$nlines pinged='$pinged'; notify.log=$(cat "$NOTIFY_LOG"); script err=$ERR"
fi

# B2: ping message shape + recipient.
if grep -q $'\t9999\t🛟 backup tick fired for stale ' "$NOTIFY_LOG" \
   && grep -q 'exit=0' "$NOTIFY_LOG"; then
    ok "B2 ping shape '🛟 backup tick fired for <slug> … exit=<code>' + chat_id 9999"
else
    bad "B2 wrong ping shape; notify.log=$(cat "$NOTIFY_LOG")"
fi

# B3: NO ping for the fresh dept anywhere.
if ! grep -qP '^fresh\t' "$NOTIFY_LOG"; then
    ok "B3 fresh (healthy) dept never pinged"
else
    bad "B3 fresh dept was pinged; notify.log=$(cat "$NOTIFY_LOG")"
fi

# B4: a real claude tick stub actually ran for stale+never (proves the run
#     branch executed, not just an accounting artifact).
if [[ "$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)" == "2" ]]; then
    ok "B4 claude tick stub ran exactly twice (stale + never)"
else
    bad "B4 expected 2 claude runs; got $(grep -c . "$CLAUDE_LOG" || true); log=$(cat "$CLAUDE_LOG")"
fi

# =============================================================================
# C. DRY_RUN footgun — BEHAVIORAL proof (the heart of the fix).
# The historical incident: DRY_RUN=1 set, ignored, 3 REAL ticks fired. So the
# decisive test is behavioral: with a STALE dept and bare DRY_RUN=1, the FIXED
# script must run NO claude tick and send NO ping (it now honors the bare name).
# =============================================================================
common_env
make_dept stale 10800
export BUBBLE_BACKUP_DEPTS="stale"

# C1 GREEN (fixed): bare DRY_RUN=1 SUPPRESSES the tick + the ping.
: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"
with_dryrun -unset- 1 "$SCRIPT"
if [[ "$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)" == "0" \
   && "$(grep -c . "$NOTIFY_LOG" || true)" == "0" ]]; then
    ok "C1 GREEN: fixed script + bare DRY_RUN=1 → NO tick, NO ping (footgun closed)"
else
    bad "C1 fixed script ran a tick/ping under bare DRY_RUN=1; claude=$(cat "$CLAUDE_LOG") notify=$(cat "$NOTIFY_LOG")"
fi

# C2 RED (pre-WS4): the OLD script IGNORES bare DRY_RUN. It cannot use our
# claude/agents stubs (it hardcodes those paths), so we prove the ignore via
# the event log: the fixed script records a 'DRY_RUN — would run' SKIP event;
# the old script, given ONLY bare DRY_RUN=1 (canonical unset), treats dry-run
# as 0 and takes the RUN path (it would emit a 'run' decision, never the
# 'DRY_RUN — would run' skip). We assert that divergence on the event log.
if [[ -f "$ORIG_SCRIPT" ]]; then
    # Fixed script: confirm it emits the dry-run skip event for the stale dept.
    : > "$WORK/loop-backup.jsonl"
    with_dryrun -unset- 1 "$SCRIPT"
    fixed_has_dryrun_skip=0
    grep -q 'DRY_RUN — would run' "$WORK/loop-backup.jsonl" 2>/dev/null && fixed_has_dryrun_skip=1

    # Old script: point its event log into tmp (it honors BUBBLE_BACKUP_LOG),
    # give it ONLY bare DRY_RUN=1. It will try the RUN path → reach the
    # hardcoded /home/claude/agents workdir (absent here under our slug) → SKIP
    # 'workdir not found', and crucially NEVER emit a 'DRY_RUN — would run'
    # event, proving the bare DRY_RUN was IGNORED.
    : > "$WORK/loop-backup.jsonl"
    with_dryrun -unset- 1 "$ORIG_SCRIPT"
    old_has_dryrun_skip=0
    grep -q 'DRY_RUN — would run' "$WORK/loop-backup.jsonl" 2>/dev/null && old_has_dryrun_skip=1

    if [[ "$fixed_has_dryrun_skip" == "1" && "$old_has_dryrun_skip" == "0" ]]; then
        ok "C2 RED->GREEN: bare DRY_RUN honored by fixed (dry-run skip event) but IGNORED by pre-WS4"
    else
        bad "C2 fixed_dryrun_skip=$fixed_has_dryrun_skip old_dryrun_skip=$old_has_dryrun_skip (expected 1/0)"
    fi
else
    echo "  SKIP: C2 (no $ORIG_SCRIPT)"
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
