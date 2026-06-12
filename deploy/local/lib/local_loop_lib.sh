#!/usr/bin/env bash
# =============================================================================
# local_loop_lib.sh — shared helpers for the MAC-SIDE (launchd) local-dept
# runtime: the main /loop runner (install-local-loop.sh) and the backup floor
# (install-local-loop-backup.sh).
#
# Context (Hybrid local/VPS agent, MIRANDA-BUILD-SPEC B2): a dept may run its
# /loop on its OWN Mac (`host: local` in onboarding/STATE.yaml) instead of the
# VPS. The VPS systemd dept-runner + the VPS loop-backup floor have no reach to
# that Mac (the VPS loop-backup SKIPS host:local depts — B1). So the Mac needs
# its OWN launchd analogues:
#   - a MAIN loop runner   (launchd plist, StartInterval) — the systemd unit's
#     Mac twin, but with NO SOPS / NO token-broker / NO tmpfs: push is via the
#     Mac's own `gh`/git credential.
#   - a BACKUP floor       (launchd plist, StartInterval) — the VPS loop-backup's
#     Mac twin: force-tick the dept's /loop iff its heartbeat is STALE.
#
# This file is sourced by both installers. It provides:
#   - is_heartbeat_stale  : THE testable core — fresh vs stale vs missing.
#   - render_loop_plist   : render the main-runner launchd plist (StartInterval).
#   - render_backup_plist : render the backup-floor launchd plist (StartInterval).
#
# DOCTRINE — StartInterval (NOT StartCalendarInterval): launchd coalesces a
# missed StartInterval and FIRES IT ON WAKE. So when the Mac is closed/asleep
# and reopened, the agent fires on wake → STEP A safe_pull pulls any approvals
# committed while asleep → decide_dispatch's morning-floor catches up the missed
# layers ("work since last run"). A fixed StartCalendarInterval whose wall-clock
# time passed while asleep would be silently MISSED. No special catch-up code is
# needed — the loop's own floor logic + this StartInterval handle it. See
# deploy/local/README.md.
#
# Fail-safe everywhere: a staleness-check error → treat as STALE (tick) rather
# than skip; never crash the caller.
# =============================================================================

# Resolve the bubble-ops-loop repo root from THIS file's location
# (deploy/local/lib/local_loop_lib.sh → repo root is three levels up). Lets the
# Python staleness core reuse the canonical scripts/lib/loop_backup.py decision
# rather than re-implementing it (single source of truth).
_LLL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLL_REPO_ROOT="$(cd "$_LLL_DIR/../../.." && pwd)"

# Default stale threshold: mirror the VPS BUBBLE_BACKUP_STALE_SEC (90 min).
LOCAL_LOOP_STALE_SEC_DEFAULT=5400

# pick a python3 (the repo's loop_backup.py is plain stdlib).
_lll_py() { command -v python3 || command -v python; }

# ── is_heartbeat_stale <dept-dir> [stale_sec] ────────────────────────────────
# Echoes "stale" or "fresh" and returns 0 (the caller branches on the WORD, not
# rc — rc is reserved for "the check itself blew up"). Reads the dept's
# outputs/<*>/heartbeat.log via the canonical scripts/lib/loop_backup.py
# (latest_heartbeat_epoch + backup_decision) so the Mac floor and the VPS floor
# share ONE staleness definition.
#
# FAIL-SAFE: no heartbeat file at all → STALE (the loop never ticked / output
# missing → tick). Any error reading/parsing → STALE (never skip on doubt).
is_heartbeat_stale() {
    local dept_dir="$1"
    local stale_sec="${2:-$LOCAL_LOOP_STALE_SEC_DEFAULT}"
    local outputs_dir="${dept_dir%/}/outputs"
    local py; py="$(_lll_py)"

    # No python or no repo lib → cannot make a precise decision → fail-safe stale.
    if [[ -z "$py" || ! -f "$LLL_REPO_ROOT/scripts/lib/loop_backup.py" ]]; then
        echo "stale"
        return 0
    fi

    local action
    action="$(
        cd "$LLL_REPO_ROOT" 2>/dev/null && "$py" - "$outputs_dir" "$stale_sec" <<'PYEOF'
import sys, time
try:
    from scripts.lib.loop_backup import latest_heartbeat_epoch, backup_decision
    outputs, stale = sys.argv[1], int(sys.argv[2])
    hb = latest_heartbeat_epoch(outputs)
    d = backup_decision(hb, time.time(), stale)
    # action "run" == loop stale (tick), "skip" == loop fresh (no tick)
    print("stale" if d.get("action") == "run" else "fresh")
except Exception:
    # ANY failure in the decision path is fail-safe to stale (tick).
    print("stale")
PYEOF
    )"

    # Empty / unexpected output (subshell died) → fail-safe stale.
    case "$action" in
        fresh) echo "fresh" ;;
        *)     echo "stale" ;;
    esac
    return 0
}

# ── _lll_xml_escape <string> ─────────────────────────────────────────────────
# Minimal XML entity escaping for values dropped into the plist (paths are
# user-controlled args).
_lll_xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

# ── render_loop_plist <label> <dept-dir> <slug> <interval_sec> <claude_bin> <log_dir> ──
# Echo a complete launchd plist for the MAIN /loop runner to stdout. The agent,
# on its StartInterval, cd's into the dept clone and launches `claude` so the
# dept's CLAUDE.md /loop protocol drives the STEP A-F tick. NO systemd, NO SOPS,
# NO token-broker — push uses the Mac's own gh/git credential (inherited env).
render_loop_plist() {
    local label="$1" dept_dir="$2" slug="$3" interval="$4" claude_bin="$5" log_dir="$6"
    local e_dept e_claude e_out e_err
    e_dept="$(_lll_xml_escape "$dept_dir")"
    e_claude="$(_lll_xml_escape "$claude_bin")"
    e_out="$(_lll_xml_escape "${log_dir%/}/${label}.out.log")"
    e_err="$(_lll_xml_escape "${log_dir%/}/${label}.err.log")"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>

    <!-- cd into the dept clone, then drive the dept's /loop. The dept's
         CLAUDE.md carries the STEP A-F protocol; STEP A safe_pull lands any
         approvals committed while the Mac was asleep, decide_dispatch's
         morning-floor catches up missed layers, STEP E pushes via the Mac's
         own gh/git credential (NO token-broker). -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>cd '${e_dept}' &amp;&amp; exec '${e_claude}' --dangerously-skip-permissions</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${e_dept}</string>

    <!-- DOCTRINE: StartInterval (NOT StartCalendarInterval) so a tick missed
         while the Mac was asleep COALESCES and fires on WAKE. -->
    <key>StartInterval</key>
    <integer>${interval}</integer>

    <key>RunAtLoad</key>
    <true/>

    <!-- Self-correlation for journald-style debugging on the Mac. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPS_LOOP_DEPT</key>
        <string>${slug}</string>
        <key>BUBBLE_DEPT</key>
        <string>${slug}</string>
        <key>BUBBLE_HOST</key>
        <string>local</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${e_out}</string>
    <key>StandardErrorPath</key>
    <string>${e_err}</string>
</dict>
</plist>
PLIST
}

# ── render_backup_plist <label> <dept-dir> <slug> <interval_sec> <runner> <log_dir> ──
# Echo a launchd plist for the BACKUP floor. On its StartInterval it runs the
# backup runner script (which checks heartbeat staleness and force-ticks the
# /loop only if stale). The runner path is baked in so the plist is the only
# scheduling surface.
render_backup_plist() {
    local label="$1" dept_dir="$2" slug="$3" interval="$4" runner="$5" log_dir="$6"
    local e_dept e_runner e_out e_err
    e_dept="$(_lll_xml_escape "$dept_dir")"
    e_runner="$(_lll_xml_escape "$runner")"
    e_out="$(_lll_xml_escape "${log_dir%/}/${label}.out.log")"
    e_err="$(_lll_xml_escape "${log_dir%/}/${label}.err.log")"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>

    <!-- The backup floor: force-tick the dept's /loop IFF its heartbeat is
         stale. The runner does the staleness check + the force-tick; the plist
         only schedules it. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>exec '${e_runner}' --dept-dir '${e_dept}' --slug '${slug}'</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${e_dept}</string>

    <!-- DOCTRINE: StartInterval so the backstop also fires on wake. -->
    <key>StartInterval</key>
    <integer>${interval}</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>OPS_LOOP_DEPT</key>
        <string>${slug}</string>
        <key>BUBBLE_DEPT</key>
        <string>${slug}</string>
        <key>BUBBLE_HOST</key>
        <string>local</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${e_out}</string>
    <key>StandardErrorPath</key>
    <string>${e_err}</string>
</dict>
</plist>
PLIST
}
