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
#   - a MAIN loop runner   (launchd plist, KeepAlive) — the systemd unit's Mac
#     twin: a PERSISTENT interactive `claude --channels telegram` session (via a
#     generic wrapper, running inside tmux), with NO SOPS / NO token-broker / NO
#     tmpfs: push is via the Mac's own `gh`/git credential.
#   - a BACKUP floor       (launchd plist, StartInterval) — the VPS loop-backup's
#     Mac twin: force-tick the dept's /loop iff its heartbeat is STALE.
#
# This file is sourced by both installers. It provides:
#   - is_heartbeat_stale  : THE testable core — fresh vs stale vs missing.
#   - render_loop_wrapper : render the generic persistent-session wrapper script.
#   - render_loop_plist   : render the main-runner launchd plist (KeepAlive).
#   - render_backup_plist : render the backup-floor launchd plist (StartInterval).
#
# DOCTRINE — MAIN runner = KeepAlive (persistent session), the Mac twin of the
# VPS systemd dept unit which runs interactive `claude --channels` (NOT
# `claude -p`). The dept arms its OWN `/loop` cron inside the session for cadence
# (boot-rearm), and its Telegram bot — its only channel to Jade/Joris — needs the
# interactive `--channels` binary. launchd KeepAlive relaunches the wrapper on
# crash; RunAtLoad starts it on login/wake. A loop tick missed while the Mac was
# asleep is caught by decide_dispatch's morning-floor on the first tick after
# wake. The stale-heartbeat backstop is the separate BACKUP floor below.
#
# DOCTRINE — BACKUP floor = StartInterval (NOT StartCalendarInterval): launchd
# coalesces a missed StartInterval and FIRES IT ON WAKE, so the backstop also
# fires after the Mac reopens. A fixed StartCalendarInterval whose wall-clock
# time passed while asleep would be silently MISSED. See deploy/local/README.md.
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

# ── render_loop_wrapper <dept-dir> <slug> <claude-bin> <tmux-bin> <telegram-state-dir> <extra-path> [workspace-dir] ──
# Echo a generic MAIN-runner wrapper script to stdout. This is the Mac twin of
# the VPS systemd ExecStart: a PERSISTENT interactive `claude --channels` session
# (NOT a per-tick headless relaunch). It mirrors the proven production pattern on
# Jade's Mac (com.claude.miranda + miranda-wrapper.sh, 2026-06-04): claude runs
# INSIDE a tmux session so a human can `tmux attach -t ops-loop-<slug>` to watch
# it live, and the wrapper blocks until the session ends so launchd KeepAlive
# restarts it on crash.
#
# WHY interactive `--channels`, not `claude -p`/StartInterval:
#   - the dept's Telegram bot (its ONLY channel to Jade/Joris — every gate card,
#     escalation, F-step summary) REQUIRES the interactive `--channels` binary;
#   - `claude -p` headless loses hooks + the channel (VPS Ban #2);
#   - the loop cadence comes from the dept arming its OWN `/loop` cron inside the
#     persistent session (boot-rearm), exactly like the VPS depts — NOT from an
#     external timer. A bare per-tick `claude` with no channel would just idle.
#
# BRAIN↔BODY (host:local depts that reuse an existing workspace's skills/tools):
# cwd is the DEPT repo (so the loop's cwd-relative outputs/queues/inbox paths +
# its `git push` of runtime state resolve natively). When <workspace-dir> is
# given, it is passed to claude as `--add-dir <workspace-dir>`: per Claude Code
# docs, `.claude/skills/` inside an --add-dir directory is loaded automatically,
# so the dept reaches the workspace's EXISTING skills + folders + scripts +
# memory WITHOUT moving or copying them (Miranda's 8 skills live only in
# Miranda_Socials/.claude/skills, not at user scope). This is option A:
# port-existing-working-components, brain in the dept repo, body in the workspace.
#
# GENERIC: parameterized by dept-dir/slug/claude-bin/tmux-bin/telegram-state-dir/
# extra-path/workspace-dir. NO SOPS / NO token-broker / NO tmpfs (Mac pushes via
# its own gh/git credential). The telegram env file (TELEGRAM_BOT_TOKEN etc.) is
# sourced from <telegram-state-dir>/.env if present, matching the convention.
render_loop_wrapper() {
    local dept_dir="$1" slug="$2" claude_bin="$3" tmux_bin="$4" tg_state="$5" extra_path="$6" workspace_dir="${7:-}"
    local add_dir_arg=""
    [[ -n "$workspace_dir" ]] && add_dir_arg=" --add-dir '${workspace_dir}'"
    cat <<WRAPPER
#!/bin/bash
# ops-loop LOCAL main-runner wrapper for dept '${slug}' (host: local).
# Rendered by install-local-loop.sh. Runs claude INSIDE a tmux session
# "ops-loop-${slug}" so a human can \`tmux attach -t ops-loop-${slug}\` to watch
# it live; launchd (KeepAlive=true) supervises THIS wrapper and restarts it on
# crash. Mac twin of the VPS systemd dept unit; NO SOPS / NO token-broker — the
# dept pushes via the Mac's own gh/git credential.
set -e
export PATH="${extra_path}:\$PATH"
export TELEGRAM_STATE_DIR="${tg_state}"
export OPS_LOOP_DEPT="${slug}"
export BUBBLE_DEPT="${slug}"
export BUBBLE_HOST="local"
export OPS_LOOP_BOOT_REARM=1
cd "${dept_dir}"

# Source the dept telegram bot env (sets TELEGRAM_BOT_TOKEN etc.) if present.
if [ -f "${tg_state}/.env" ]; then
  set -a
  . "${tg_state}/.env"
  set +a
fi

TMUX_BIN="${tmux_bin}"
SESSION="ops-loop-${slug}"

# Kill any stale session from a previous run so we never stack sessions.
"\$TMUX_BIN" kill-session -t "\$SESSION" 2>/dev/null || true

# Start claude detached inside tmux. tmux gives it a real PTY. The inner shell
# \`exec\`s claude so the pane dies exactly when claude dies. cwd is the dept repo
# (loop state); when a workspace is granted it loads that dir's .claude/skills.
"\$TMUX_BIN" new-session -d -s "\$SESSION" \\
  "exec '${claude_bin}' --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official${add_dir_arg}"

# Block in the foreground until the session ends. When claude exits, the session
# disappears, this loop ends, the wrapper exits non-zero, and launchd KeepAlive
# relaunches the wrapper (which recreates the session). Poll is cheap (5s).
while "\$TMUX_BIN" has-session -t "\$SESSION" 2>/dev/null; do
  sleep 5
done

# Session gone => claude exited. Exit non-zero so launchd KeepAlive restarts us.
exit 1
WRAPPER
}

# ── render_loop_plist <label> <wrapper-path> <dept-dir> <slug> <log_dir> <extra-path> ──
# Echo a complete launchd plist for the MAIN /loop runner to stdout. KeepAlive
# (NOT StartInterval): the runner is a PERSISTENT interactive `claude --channels`
# session (via the wrapper), the Mac twin of the VPS systemd dept unit. launchd
# restarts the wrapper on crash; the dept arms its own /loop cron inside the
# session for cadence. NO SOPS / NO token-broker — push uses the Mac's own
# gh/git credential.
render_loop_plist() {
    local label="$1" wrapper_path="$2" dept_dir="$3" slug="$4" log_dir="$5" extra_path="$6"
    local e_wrap e_dept e_out e_err e_path
    e_wrap="$(_lll_xml_escape "$wrapper_path")"
    e_dept="$(_lll_xml_escape "$dept_dir")"
    e_out="$(_lll_xml_escape "${log_dir%/}/${label}.out.log")"
    e_err="$(_lll_xml_escape "${log_dir%/}/${label}.err.log")"
    e_path="$(_lll_xml_escape "$extra_path")"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>

    <!-- The wrapper launches a PERSISTENT interactive \`claude --channels\`
         session inside tmux (Mac twin of the VPS systemd dept unit). The dept's
         CLAUDE.md /loop protocol + its own armed /loop cron drive the STEP A-F
         tick; STEP A safe_pull lands approvals committed while the Mac slept;
         STEP E pushes via the Mac's own gh/git credential (NO token-broker). -->
    <key>ProgramArguments</key>
    <array>
        <string>${e_wrap}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${e_dept}</string>

    <!-- DOCTRINE: KeepAlive (NOT StartInterval) — the runner is a long-lived
         interactive session, not a per-tick job. launchd relaunches the wrapper
         if claude exits/crashes. RunAtLoad starts it on login + on wake. A loop
         tick missed while the Mac was asleep is caught by decide_dispatch's
         morning-floor on the next tick after wake (no external timer needed);
         the separate backup floor is the stale-heartbeat backstop. -->
    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <!-- launchd's default PATH is minimal; export the same PATH the wrapper
         needs so even pre-source lookups (claude/tmux) resolve. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${e_path}:/usr/local/bin:/usr/bin:/bin</string>
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

# ── render_backup_plist <label> <dept-dir> <slug> <interval_sec> <runner> <log_dir> [claude_bin] [workspace_dir] [extra_path] [activate_tick] ──
# Echo a launchd plist for the BACKUP floor. On its StartInterval it runs the
# backup runner script (which checks heartbeat staleness and force-ticks the
# /loop only if stale). The runner path is baked in so the plist is the only
# scheduling surface.
#
# The optional trailing args make the baked runner invocation actually able to
# tick: claude_bin (else "claude", not on launchd PATH), workspace_dir (--add-dir
# so the tick is skill-aware), extra_path (prepended to PATH), and activate_tick
# ("1" → pass --activate-tick so the floor REALLY ticks; default-empty keeps the
# old render-only/dry behaviour so a test or a deliberately-passive floor is safe).
render_backup_plist() {
    local label="$1" dept_dir="$2" slug="$3" interval="$4" runner="$5" log_dir="$6"
    local claude_bin="${7:-}" workspace_dir="${8:-}" extra_path="${9:-}" activate_tick="${10:-}"
    local e_dept e_runner e_out e_err
    e_dept="$(_lll_xml_escape "$dept_dir")"
    e_runner="$(_lll_xml_escape "$runner")"
    e_out="$(_lll_xml_escape "${log_dir%/}/${label}.out.log")"
    e_err="$(_lll_xml_escape "${log_dir%/}/${label}.err.log")"
    # Build the runner arg string, XML-escaping each interpolated value.
    local runner_args="--dept-dir '$(_lll_xml_escape "$dept_dir")' --slug '$(_lll_xml_escape "$slug")'"
    [[ -n "$claude_bin" ]]    && runner_args="$runner_args --claude-bin '$(_lll_xml_escape "$claude_bin")'"
    [[ -n "$workspace_dir" ]] && runner_args="$runner_args --workspace-dir '$(_lll_xml_escape "$workspace_dir")'"
    [[ -n "$extra_path" ]]    && runner_args="$runner_args --extra-path '$(_lll_xml_escape "$extra_path")'"
    [[ "$activate_tick" == "1" ]] && runner_args="$runner_args --activate-tick"
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
        <string>exec '${e_runner}' ${runner_args}</string>
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
