#!/usr/bin/env bash
# =============================================================================
# test_loop_backup.sh — bash harness for loop-backup.sh (TDD).
#
# Covers, in order:
#
#   A. DRY_RUN footgun fix — the script must honor BOTH `BUBBLE_BACKUP_DRY_RUN`
#      AND the bare `DRY_RUN` people actually type, resolve a sane precedence,
#      and LOG IT LOUDLY on startup ("DRY_RUN resolved to <0|1> from <var>").
#
#   B. Notify-on-fire — when a backup tick ACTUALLY RUNS for a dept (stale /
#      missing heartbeat), the script pings {{OPERATOR}} ONCE on Telegram; on a fresh
#      (healthy) loop it must NOT ping. Telegram send + claude tick are stubbed.
#
#   C. DRY_RUN behavioral proof — with a STALE dept and bare DRY_RUN=1 the
#      FIXED script runs NO claude tick and sends NO ping.
#
#   D. --layer N (4-layer floor) — `--layer N` forces Layer N into the tick
#      prompt (bypassing decide_dispatch) and tags the fired-ping as an
#      "L<N> floor tick".
#
#   E. Auto-discovery — when BUBBLE_BACKUP_DEPTS is UNSET, depts are discovered
#      by globbing $AGENTS_ROOT/bubble-ops-*; the discovered set drives the run.
#
#   F. Eligibility — a dept whose ops-loop-<slug>.service is NOT enabled
#      (disabled or absent) is SKIPPED (structural skip, no tick, no ping).
#
#   G. Per-layer eligibility — in --layer N mode, a dept WITHOUT
#      layers/N/PROMPT.md is SKIPPED (no missing-mission tick).
#
#   H. Result-relay (B5) under --layer — the work-summary parsed from the
#      claude --output-format json envelope is still appended to the ping when
#      a forced-layer tick runs.
#
# Hermetic: fake dept workdirs with controlled heartbeat fixtures under a
# tmpdir; LOCK_DIR + BACKUP_LOG redirected into the tmpdir; `claude`,
# `systemctl` and the Telegram send are ALL stubbed (no live HTTP, no real
# `claude -p`, no real systemd). Uses the REAL repo venv python + the REAL
# scripts/lib/loop_backup.py decision logic (so the freshness gate is exercised
# for real).
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
ORIG_SCRIPT="${ORIG_SCRIPT:-$REPO_ROOT/scripts/loop-backup.sh.orig}"  # pre-fix (optional)
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
# tick. Records each invocation (and dumps its argv) so we can assert a tick
# "ran" AND inspect the TICK_PROMPT the script handed it (used by the --layer
# tests to prove "run Layer N" is in the prompt).
CLAUDE_STUB="$WORK/claude-stub.sh"
CLAUDE_LOG="$WORK/claude-invocations.log"
CLAUDE_ARGS="$WORK/claude-last-args.txt"
: > "$CLAUDE_LOG"
cat > "$CLAUDE_STUB" <<EOF
#!/usr/bin/env bash
echo "CLAUDE_STUB_RAN" >> "$CLAUDE_LOG"
# Persist the full argv (last invocation wins) so a test can grep the prompt.
printf '%s\n' "\$@" > "$CLAUDE_ARGS"
# Emit a realistic --output-format json envelope so the result-relay parser has
# a 'result' to extract (used by the H result-relay test). Overridable via
# CLAUDE_STUB_RESULT so a test can inject a specific work summary.
echo "{\"type\":\"result\",\"result\":\"\${CLAUDE_STUB_RESULT:-stub ok}\"}"
exit 0
EOF
chmod +x "$CLAUDE_STUB"

# notify stub: capture (slug, chat_id, msg) of every backup-fired ping. NO HTTP.
NOTIFY_STUB="$WORK/notify-stub.sh"
NOTIFY_LOG="$WORK/notify.log"
: > "$NOTIFY_LOG"
cat > "$NOTIFY_STUB" <<EOF
#!/usr/bin/env bash
# args: slug chat_id msg.  The msg can be multi-line (fired-line + summary);
# collapse newlines to \\n so each ping stays one log line (assertions grep it).
slug="\$1"; chat="\$2"; shift 2; msg="\$*"
printf '%s\t%s\t%s\n' "\$slug" "\$chat" "\${msg//\$'\n'/\\\\n}" >> "$NOTIFY_LOG"
exit 0
EOF
chmod +x "$NOTIFY_STUB"

# systemctl stub: emulate `systemctl is-enabled ops-loop-<slug>.service`.
# A dept is "enabled" iff its slug is listed in $ENABLED_DEPTS (space-sep,
# read from the file $ENABLED_FILE so each test can rewrite it). Anything else
# → exit 1 (disabled) / the real `is-enabled` would exit 4 for not-found; both
# are non-zero, which is all the script's eligibility gate checks.
SYSTEMCTL_STUB="$WORK/systemctl-stub.sh"
ENABLED_FILE="$WORK/enabled-depts.txt"
: > "$ENABLED_FILE"
cat > "$SYSTEMCTL_STUB" <<EOF
#!/usr/bin/env bash
# Expected call: systemctl is-enabled ops-loop-<slug>.service
if [[ "\$1" == "is-enabled" ]]; then
    unit="\$2"                       # ops-loop-<slug>.service
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

set_enabled() { printf '%s\n' "$*" > "$ENABLED_FILE"; }   # mark these slugs enabled

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

make_layer() {
    # make_layer <slug> <N>  → create layers/<N>/PROMPT.md for the dept.
    local slug="$1" n="$2"
    local dir="$AGENTS_ROOT/bubble-ops-$slug/layers/$n"
    mkdir -p "$dir"
    echo "Layer $n mission for $slug." > "$dir/PROMPT.md"
}

make_host() {
    # make_host <slug> <vps|local|MALFORMED>  → write onboarding/STATE.yaml host.
    # "MALFORMED" writes an unparseable STATE.yaml to prove fail-safe→vps.
    local slug="$1" host="$2"
    local dir="$AGENTS_ROOT/bubble-ops-$slug/onboarding"
    mkdir -p "$dir"
    if [[ "$host" == "MALFORMED" ]]; then
        printf 'host: [unterminated\n  : : :\n' > "$dir/STATE.yaml"
    else
        printf 'slug: %s\nstatus: Live\nhost: %s\n' "$slug" "$host" > "$dir/STATE.yaml"
    fi
}

reset_fixtures() {
    rm -rf "$AGENTS_ROOT"; mkdir -p "$AGENTS_ROOT"
    : > "$ENABLED_FILE"
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
    export BUBBLE_BACKUP_SYSTEMCTL="$SYSTEMCTL_STUB"   # stub is-enabled
    unset CLAUDE_STUB_RESULT
    mkdir -p "$WORK/lock"
}

OUT=""; ERR=""; ALL=""; RC=0
run_script() {
    # run_script <script_path> [args...]. Runs in the CURRENT shell (not a
    # subshell) so captured OUT/ERR/ALL/RC are visible to the assertions.
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
#   with_dryrun "<BUBBLE_BACKUP_DRY_RUN or -unset->" "<DRY_RUN or -unset->" SCRIPT [args...]
with_dryrun() {
    local canon="$1" bare="$2" script="$3"; shift 3
    if [[ "$canon" == "-unset-" ]]; then unset BUBBLE_BACKUP_DRY_RUN; else export BUBBLE_BACKUP_DRY_RUN="$canon"; fi
    if [[ "$bare"  == "-unset-" ]]; then unset DRY_RUN;               else export DRY_RUN="$bare"; fi
    run_script "$script" "$@"
    unset BUBBLE_BACKUP_DRY_RUN DRY_RUN
}

echo "== loop-backup.sh tests =="

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

# A6 (RED->GREEN proof): the PRE-fix script IGNORES bare DRY_RUN — no banner.
if [[ -f "$ORIG_SCRIPT" ]]; then
    with_dryrun -unset- 1 "$ORIG_SCRIPT"
    if [[ "$ALL" != *"DRY_RUN resolved to"* ]]; then
        ok "A6 RED->GREEN: pre-fix script has NO 'DRY_RUN resolved' banner (footgun present)"
    else
        bad "A6 pre-fix script unexpectedly logged a DRY_RUN banner: $ALL"
    fi
else
    echo "  SKIP: A6 (no $ORIG_SCRIPT to prove red->green against)"
fi

# =============================================================================
# B. Notify-on-fire — one ping per STALE dept, none for FRESH depts.
# =============================================================================
# Hermetic: fake dept workdirs in the tmpdir; claude + Telegram + systemctl
# stubbed. The three depts must be ENABLED (set_enabled) so the eligibility
# gate lets them through to the freshness gate.
#   fresh  — heartbeat 10 min old (< 90m stale)  → SKIP  → no ping
#   stale  — heartbeat 3h old      (> 90m stale)  → RUN   → one ping
#   never  — no heartbeat at all                  → RUN   → one ping
reset_fixtures
common_env
make_dept fresh 600
make_dept stale 10800
make_dept never none
set_enabled fresh stale never
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
# With a STALE dept and bare DRY_RUN=1, the FIXED script must run NO claude
# tick and send NO ping (it now honors the bare name).
# =============================================================================
reset_fixtures
common_env
make_dept stale 10800
set_enabled stale
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

# C2 RED->GREEN: the OLD script ignores bare DRY_RUN. Only runs if .orig present.
if [[ -f "$ORIG_SCRIPT" ]]; then
    : > "$WORK/loop-backup.jsonl"
    with_dryrun -unset- 1 "$SCRIPT"
    fixed_has_dryrun_skip=0
    grep -q 'DRY_RUN — would run' "$WORK/loop-backup.jsonl" 2>/dev/null && fixed_has_dryrun_skip=1
    : > "$WORK/loop-backup.jsonl"
    with_dryrun -unset- 1 "$ORIG_SCRIPT"
    old_has_dryrun_skip=0
    grep -q 'DRY_RUN — would run' "$WORK/loop-backup.jsonl" 2>/dev/null && old_has_dryrun_skip=1
    if [[ "$fixed_has_dryrun_skip" == "1" && "$old_has_dryrun_skip" == "0" ]]; then
        ok "C2 RED->GREEN: bare DRY_RUN honored by fixed (dry-run skip event) but IGNORED by pre-fix"
    else
        bad "C2 fixed_dryrun_skip=$fixed_has_dryrun_skip old_dryrun_skip=$old_has_dryrun_skip (expected 1/0)"
    fi
else
    echo "  SKIP: C2 (no $ORIG_SCRIPT)"
fi

# =============================================================================
# D. --layer N (4-layer floor): forces Layer N into the tick prompt and tags
#    the fired ping as an "L<N> floor tick".
# =============================================================================
# A single stale dept WITH layer 2 present. Run --layer 2. Assert:
#   (a) the claude stub was handed a TICK_PROMPT that says "Run Layer 2 NOW".
#   (b) the fired ping is tagged "🛟 L2 floor tick fired for <slug>".
#   (c) the startup banner announces layer-floor mode.
reset_fixtures
common_env
make_dept d2 10800
make_layer d2 2
set_enabled d2
export BUBBLE_BACKUP_DEPTS="d2"

: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"; : > "$CLAUDE_ARGS"
with_dryrun -unset- -unset- "$SCRIPT" --layer 2

# D1: the forced-layer prompt was passed to claude (the prompt is the last argv
#     element of the stub; assert it forces Layer 2 and bans decide_dispatch).
prompt="$(cat "$CLAUDE_ARGS" 2>/dev/null || true)"
if grep -q 'Run Layer 2 NOW' "$CLAUDE_ARGS" 2>/dev/null \
   && grep -q 'layers/2/PROMPT.md' "$CLAUDE_ARGS" 2>/dev/null \
   && grep -q 'Do NOT run' "$CLAUDE_ARGS" 2>/dev/null; then
    ok "D1 --layer 2 forces 'Run Layer 2 NOW' + reads layers/2/PROMPT.md + bans decide_dispatch in the prompt"
else
    bad "D1 forced-layer prompt missing/incorrect; claude args=$prompt"
fi

# D2: the fired ping is tagged as an L2 floor tick.
if grep -q '🛟 L2 floor tick fired for d2 ' "$NOTIFY_LOG"; then
    ok "D2 fired ping tagged '🛟 L2 floor tick fired for <slug>'"
else
    bad "D2 ping not tagged L2 floor; notify.log=$(cat "$NOTIFY_LOG")"
fi

# D3: startup banner announces layer-floor mode for the right layer.
if [[ "$ALL" == *"MODE: layer-floor — forcing Layer 2"* ]]; then
    ok "D3 startup banner announces 'MODE: layer-floor — forcing Layer 2'"
else
    bad "D3 missing layer-floor banner; got: $ALL"
fi

# D4: NO --layer → generic mode (decide_dispatch prompt, generic ping shape).
reset_fixtures
common_env
make_dept d0 10800
make_layer d0 1
set_enabled d0
export BUBBLE_BACKUP_DEPTS="d0"
: > "$NOTIFY_LOG"; : > "$CLAUDE_ARGS"
with_dryrun -unset- -unset- "$SCRIPT"
if grep -q 'decide_dispatch' "$CLAUDE_ARGS" 2>/dev/null \
   && grep -q '🛟 backup tick fired for d0 ' "$NOTIFY_LOG"; then
    ok "D4 no --layer → generic decide_dispatch prompt + 'backup tick' ping (back-compat)"
else
    bad "D4 generic mode broken; args=$(cat "$CLAUDE_ARGS"); notify=$(cat "$NOTIFY_LOG")"
fi

# D5: invalid --layer value is rejected (exit 2, no tick).
reset_fixtures; common_env; : > "$CLAUDE_LOG"
run_script "$SCRIPT" --layer 9
if [[ "$RC" == "2" && "$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)" == "0" ]]; then
    ok "D5 invalid '--layer 9' rejected (exit 2, no tick)"
else
    bad "D5 expected exit 2 + no tick; rc=$RC claude=$(cat "$CLAUDE_LOG")"
fi

# =============================================================================
# E. Auto-discovery: BUBBLE_BACKUP_DEPTS UNSET → glob $AGENTS_ROOT/bubble-ops-*.
# =============================================================================
# Seed three dept dirs but DO NOT set BUBBLE_BACKUP_DEPTS. All enabled + have
# layer 1. Two stale, one fresh. Run --layer 1. The discovered set must drive
# the run: 2 ticks (the stale ones), and the discovery log line must name them.
reset_fixtures
common_env
make_dept alpha 10800; make_layer alpha 1
make_dept bravo 10800; make_layer bravo 1
make_dept charlie 600; make_layer charlie 1   # fresh → skip
set_enabled alpha bravo charlie
unset BUBBLE_BACKUP_DEPTS                      # ← discovery path

: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"
with_dryrun -unset- -unset- "$SCRIPT" --layer 1

# E1: discovery banner lists all three discovered slugs.
if [[ "$ALL" == *"auto-discovered"* && "$ALL" == *"alpha"* && "$ALL" == *"bravo"* && "$ALL" == *"charlie"* ]]; then
    ok "E1 auto-discovery names alpha/bravo/charlie from the glob (BUBBLE_BACKUP_DEPTS unset)"
else
    bad "E1 discovery banner missing depts; got: $ALL"
fi

# E2: exactly the two STALE discovered depts ran a tick (fresh charlie skipped).
ran="$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)"
pinged="$(cut -f1 "$NOTIFY_LOG" | sort | tr '\n' ' ')"
if [[ "$ran" == "2" && "$pinged" == "alpha bravo " ]]; then
    ok "E2 discovered set drives the run: alpha+bravo ticked, fresh charlie skipped"
else
    bad "E2 expected 2 ticks {alpha,bravo}; ran=$ran pinged='$pinged'"
fi

# =============================================================================
# F. Eligibility — disabled / absent service is SKIPPED (no tick, no ping).
# =============================================================================
# Two stale depts WITH layer 1; only 'live' is enabled. 'paused' is disabled,
# 'ghost' has no service entry at all (stub returns disabled for both). Discover.
reset_fixtures
common_env
make_dept live 10800;   make_layer live 1
make_dept paused 10800; make_layer paused 1
make_dept ghost 10800;  make_layer ghost 1
set_enabled live                              # only 'live' enabled
unset BUBBLE_BACKUP_DEPTS

: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"; : > "$WORK/loop-backup.jsonl"
with_dryrun -unset- -unset- "$SCRIPT" --layer 1

# F1: only 'live' ticked; paused + ghost skipped (no tick, no ping).
ran="$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)"
pinged="$(cut -f1 "$NOTIFY_LOG" | sort | tr '\n' ' ')"
if [[ "$ran" == "1" && "$pinged" == "live " ]]; then
    ok "F1 disabled/absent service skipped: only 'live' ticked (paused+ghost skipped)"
else
    bad "F1 expected only live ticked; ran=$ran pinged='$pinged'"
fi

# F2: the skip is RECORDED as a structural skip event for paused + ghost.
if grep -q '"slug": "paused"' "$WORK/loop-backup.jsonl" \
   && grep -q 'not enabled' "$WORK/loop-backup.jsonl"; then
    ok "F2 ineligible depts recorded as structural skip ('not enabled') in the event log"
else
    bad "F2 structural skip not logged; jsonl=$(cat "$WORK/loop-backup.jsonl")"
fi

# =============================================================================
# G. Per-layer eligibility — in --layer N mode, a dept WITHOUT layers/N is
#    SKIPPED (no missing-mission tick). Mirrors the live 'fixture' dept that has
#    layers 2/3/4 but NOT 1.
# =============================================================================
reset_fixtures
common_env
make_dept hasL1 10800; make_layer hasL1 1; make_layer hasL1 2
make_dept noL1  10800;                     make_layer noL1 2   # has L2, NOT L1
set_enabled hasL1 noL1
unset BUBBLE_BACKUP_DEPTS

: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"; : > "$WORK/loop-backup.jsonl"
with_dryrun -unset- -unset- "$SCRIPT" --layer 1

# G1: under --layer 1, only the dept WITH layers/1 ticks; the one without skips.
ran="$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)"
pinged="$(cut -f1 "$NOTIFY_LOG" | sort | tr '\n' ' ')"
if [[ "$ran" == "1" && "$pinged" == "hasL1 " ]]; then
    ok "G1 --layer 1: dept without layers/1/PROMPT.md skipped (only hasL1 ticked)"
else
    bad "G1 expected only hasL1 ticked under --layer 1; ran=$ran pinged='$pinged'"
fi

# G2: the no-L1 skip names the missing layer in the event log.
if grep -q '"slug": "noL1"' "$WORK/loop-backup.jsonl" \
   && grep -q "doesn't run L1" "$WORK/loop-backup.jsonl"; then
    ok "G2 missing-layer skip recorded ('doesn't run L1') in the event log"
else
    bad "G2 missing-layer skip not logged; jsonl=$(cat "$WORK/loop-backup.jsonl")"
fi

# G3: the SAME dept set under --layer 2 ticks BOTH (both have layer 2).
reset_fixtures
common_env
make_dept hasL1 10800; make_layer hasL1 1; make_layer hasL1 2
make_dept noL1  10800;                     make_layer noL1 2
set_enabled hasL1 noL1
unset BUBBLE_BACKUP_DEPTS
: > "$CLAUDE_LOG"
with_dryrun -unset- -unset- "$SCRIPT" --layer 2
if [[ "$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)" == "2" ]]; then
    ok "G3 --layer 2: both depts (both have layers/2) tick — layer-specific gate, not a blanket skip"
else
    bad "G3 expected 2 ticks under --layer 2; got $(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)"
fi

# =============================================================================
# H. Result-relay (B5) under --layer — the work summary from the claude json
#    envelope is appended to the fired ping for a forced-layer tick.
# =============================================================================
reset_fixtures
common_env
make_dept relay 10800; make_layer relay 3
set_enabled relay
export BUBBLE_BACKUP_DEPTS="relay"
export CLAUDE_STUB_RESULT="Ran L3. Risk check: 2 positions flagged, 1 gate opened."

: > "$NOTIFY_LOG"
with_dryrun -unset- -unset- "$SCRIPT" --layer 3

# H1: the ping carries BOTH the L3-floor fired-line AND the parsed work summary.
if grep -q '🛟 L3 floor tick fired for relay ' "$NOTIFY_LOG" \
   && grep -q 'Risk check: 2 positions flagged' "$NOTIFY_LOG"; then
    ok "H1 result-relay under --layer 3: ping carries fired-line + parsed work summary"
else
    bad "H1 result-relay missing under --layer; notify.log=$(cat "$NOTIFY_LOG")"
fi
unset CLAUDE_STUB_RESULT

# =============================================================================
# I. Host-skip (B1) — a `host: local` dept (onboarding/STATE.yaml) is DISCOVERED
#    (its files stay on disk for the cockpit) but its OODA layer is NOT executed
#    by the VPS backup floor (it runs on its own machine). A `host: vps` dept,
#    and a dept with NO host field (back-compat), still execute. A malformed
#    STATE.yaml fails SAFE → treated as vps (never crash the floor).
# =============================================================================
reset_fixtures
common_env
# vpsdept: host: vps   → MUST tick.        absent: no host field → MUST tick (back-compat).
# localdept: host: local → MUST be skipped. broken: malformed STATE → fail-safe vps → MUST tick.
make_dept vpsdept   10800; make_layer vpsdept   1; make_host vpsdept   vps
make_dept localdept 10800; make_layer localdept 1; make_host localdept local
make_dept absent    10800; make_layer absent    1   # no make_host → no STATE.yaml host
make_dept broken    10800; make_layer broken    1; make_host broken    MALFORMED
set_enabled vpsdept localdept absent broken
unset BUBBLE_BACKUP_DEPTS

: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"; : > "$WORK/loop-backup.jsonl"
with_dryrun -unset- -unset- "$SCRIPT" --layer 1

# I1: the host:local dept is SKIPPED — no tick, no ping. The others all tick.
ran="$(grep -c CLAUDE_STUB_RAN "$CLAUDE_LOG" || true)"
pinged="$(cut -f1 "$NOTIFY_LOG" | sort | tr '\n' ' ')"
if [[ "$ran" == "3" && "$pinged" == "absent broken vpsdept " ]]; then
    ok "I1 host:local SKIPPED by the VPS floor; vps + host-absent + malformed(fail-safe vps) all tick"
else
    bad "I1 expected 3 ticks {absent,broken,vpsdept}, NOT localdept; ran=$ran pinged='$pinged'"
fi

# I2: the host:local skip is RECORDED (cockpit visibility) and names the reason.
if grep -q '"slug": "localdept"' "$WORK/loop-backup.jsonl" \
   && grep -q 'host: local' "$WORK/loop-backup.jsonl"; then
    ok "I2 host:local skip recorded in the event log ('host: local' reason)"
else
    bad "I2 host:local skip not logged; jsonl=$(cat "$WORK/loop-backup.jsonl")"
fi

# I3: the localdept dir STAYS on disk (discovered/visible) — the skip must not
#     delete or hide it; only the EXECUTION is withheld.
if [[ -d "$AGENTS_ROOT/bubble-ops-localdept" \
   && -f "$AGENTS_ROOT/bubble-ops-localdept/onboarding/STATE.yaml" ]]; then
    ok "I3 host:local dept dir + STATE.yaml remain on disk (still rendered by the cockpit)"
else
    bad "I3 localdept dir/STATE.yaml unexpectedly gone after the run"
fi

# I4: the human-readable skip line is logged to stdout/stderr for the journal.
if [[ "$ALL" == *"skip localdept (host: local"* ]] \
   || [[ "$ALL" == *"localdept"*"host: local"* ]]; then
    ok "I4 clear journal line for the host:local skip"
else
    bad "I4 missing clear host:local skip line; got: $ALL"
fi

# =============================================================================
# J. Truthful external heartbeat (Rick 2026-06-19) — when the floor intervenes
#    on a STALE dept it appends a TRUTHFUL outcome line to the dept's OWN
#    outputs/<today>/heartbeat.log:
#      backup ran OK    → `tick BACKUP-RAN-FOR-DEPT layer=N exit=0`
#      backup FAILED    → `tick BACKUP-FAILED exit=N — dept DOWN`
#    A FRESH dept (healthy live loop) gets NO floor line (it writes its own).
# =============================================================================
# J1: a stale dept whose backup tick succeeds → BACKUP-RAN line appended.
reset_fixtures
common_env
make_dept jran 10800; make_layer jran 2
set_enabled jran
export BUBBLE_BACKUP_DEPTS="jran"
: > "$NOTIFY_LOG"; : > "$CLAUDE_LOG"
with_dryrun -unset- -unset- "$SCRIPT" --layer 2
TODAY="$(date -u +%Y-%m-%d)"
HB_JRAN="$AGENTS_ROOT/bubble-ops-jran/outputs/$TODAY/heartbeat.log"
if grep -q 'BACKUP-RAN-FOR-DEPT layer=2 exit=0' "$HB_JRAN" 2>/dev/null; then
    ok "J1 stale dept + backup OK → truthful 'BACKUP-RAN-FOR-DEPT layer=2 exit=0' appended to its heartbeat.log"
else
    bad "J1 missing BACKUP-RAN line; heartbeat=$(cat "$HB_JRAN" 2>/dev/null)"
fi

# J2: a stale dept whose backup tick FAILS → BACKUP-FAILED '… dept DOWN' line.
#     Override the claude stub with a failing one for this run.
FAIL_STUB="$WORK/claude-fail.sh"
cat > "$FAIL_STUB" <<'EOF'
#!/usr/bin/env bash
echo "CLAUDE_STUB_RAN" >> "$CLAUDE_LOG_J"
echo '{"type":"result","result":"boom"}'
exit 1
EOF
chmod +x "$FAIL_STUB"
reset_fixtures
common_env
export CLAUDE_LOG_J="$CLAUDE_LOG"
make_dept jfail 10800; make_layer jfail 2
set_enabled jfail
export BUBBLE_BACKUP_DEPTS="jfail"
export BUBBLE_BACKUP_CLAUDE_BIN="$FAIL_STUB"
: > "$CLAUDE_LOG"
with_dryrun -unset- -unset- "$SCRIPT" --layer 2
HB_JFAIL="$AGENTS_ROOT/bubble-ops-jfail/outputs/$TODAY/heartbeat.log"
if grep -q 'BACKUP-FAILED exit=1' "$HB_JFAIL" 2>/dev/null \
   && grep -q 'dept DOWN' "$HB_JFAIL" 2>/dev/null; then
    ok "J2 stale dept + backup FAIL → truthful 'BACKUP-FAILED exit=1 — dept DOWN' appended (the missing 'I'm down' signal)"
else
    bad "J2 missing BACKUP-FAILED line; heartbeat=$(cat "$HB_JFAIL" 2>/dev/null)"
fi

# J3: a FRESH dept (healthy live loop) gets NO floor heartbeat line — it writes
#     its own. The floor must not stamp a fresh dept's heartbeat.
reset_fixtures
common_env
make_dept jfresh 600; make_layer jfresh 2   # 10 min old → fresh, skipped
set_enabled jfresh
export BUBBLE_BACKUP_DEPTS="jfresh"
with_dryrun -unset- -unset- "$SCRIPT" --layer 2
HB_JFRESH="$AGENTS_ROOT/bubble-ops-jfresh/outputs/$TODAY/heartbeat.log"
if ! grep -qE 'BACKUP-RAN-FOR-DEPT|BACKUP-FAILED' "$HB_JFRESH" 2>/dev/null; then
    ok "J3 fresh (healthy) dept gets NO floor heartbeat line (it writes its own)"
else
    bad "J3 floor wrote a truthful line for a FRESH dept; heartbeat=$(cat "$HB_JFRESH" 2>/dev/null)"
fi
unset BUBBLE_BACKUP_CLAUDE_BIN CLAUDE_LOG_J

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
