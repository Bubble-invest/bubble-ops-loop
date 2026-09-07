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
#     twin: a PERSISTENT interactive harness session (claude --channels telegram,
#     or hermes via the #1133 selector) running inside tmux, rendered from the
#     aligned wrapper (#748). Secrets come from a per-Mac SOPS vault when the
#     LOOP_VAULT_PATH knob is set (legacy .env fallback otherwise); push is via
#     the Mac's own `gh`/git credential (no VPS-style token-broker/tmpfs).
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
# (boot-rearm), and its Telegram bot — its only channel to {{OPERATOR_2}}/{{OPERATOR}} — needs the
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

# ── render_loop_wrapper <dept-dir> <slug> <claude-bin> <tmux-bin> <telegram-state-dir> <extra-path> [workspace-dir] [channel-patches-script] ──
# Echo a generic MAIN-runner wrapper script to stdout. This is the Mac twin of
# the VPS systemd ExecStart: a PERSISTENT interactive `claude --channels` session
# (NOT a per-tick headless relaunch). It mirrors the proven production pattern on
# {{OPERATOR_2}}'s Mac (com.claude.miranda + miranda-wrapper.sh, 2026-06-04): claude runs
# INSIDE a tmux session so a human can `tmux attach -t ops-loop-<slug>` to watch
# it live, and the wrapper blocks until the session ends so launchd KeepAlive
# restarts it on crash.
#
# WHY interactive `--channels`, not `claude -p`/StartInterval:
#   - the dept's Telegram bot (its ONLY channel to {{OPERATOR_2}}/{{OPERATOR}} — every gate card,
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
# extra-path/workspace-dir/channel-patches-script (positional) PLUS the optional
# ALIGNMENT KNOBS below (#748/#1133) read from the environment. With NO knobs set
# it renders the plain generic wrapper (telegram env sourced from
# <telegram-state-dir>/.env; push via the Mac's own gh/git credential — no
# token-broker/tmpfs). The knobs fold in the customizations that used to be
# HAND-EDITED into each live Mac wrapper (#748: launchers had drifted into 3
# different shapes), making THIS the single source of truth:
#   LOOP_VAULT_PATH   SOPS-encrypted secrets.sops.env (this Mac's age key) —
#                     decrypted to a chmod-600 tmpfile, sourced, shredded; legacy
#                     .env is the fallback. Empty = no vault (source .env only).
#   LOOP_LEGACY_ENV   plaintext fallback path (default <telegram-state-dir>/.env).
#   LOOP_AGE_KEY_FILE age key for the vault (default $HOME/.config/sops/age/keys.txt).
#   LOOP_MODEL        model pin, e.g. claude-opus-4-8[1m] (empty = no --model; a
#                     bare `--model opus` silently drifts to the latest Opus — the
#                     drift #748 also fixes on M1 content).
#   LOOP_CHROME=1     add --chrome (Chrome-extension agents).
#   LOOP_CONTINUE=1   add --continue + the resume-gate auto-answer + fresh-fallback.
#   LOOP_INLINE_ENV   space-sep VAR names passed INLINE into the tmux command
#                     (default: TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN) —
#                     REQUIRED because `tmux new-session` runs the inner command in
#                     the tmux SERVER's global env, NOT this wrapper's env, so
#                     exported secrets would NOT reach the harness otherwise.
#   LOOP_ENV_UNSET    space-sep VAR names to `env -u` before exec (e.g.
#                     CLAUDE_CODE_OAUTH_TOKEN so a keychain login wins — Géraldine).
#   LOOP_HARNESS_SELECTOR_DIR  dir holding harness-<slug> (default
#                     $HOME/Library/Application Support/bubble-ops-loop).
#   LOOP_HERMES_BIN   hermes binary (default `hermes`).
# HARNESS SELECTOR (#1133): every rendered wrapper reads
# <selector-dir>/harness-<slug> at launch — "hermes" runs `hermes -p <slug>
# gateway run --replace`, anything else (default) runs claude. The Mac twin of the
# VPS /etc/bubble-harness/<slug> selector; a switch flips the file then
# `launchctl kickstart -k` restarts the wrapper, which re-reads it.
#
# channel-patches-script (board #956): before every claude launch, self-heal the
# telegram plugin's boot_rearm + bubble-inject patches — the plugin cache is
# VOLATILE (re-extracted on auto-update, wiping both hand-applied patches; this
# is exactly how the 2026-08-15 incident, board #1047, happened on this class of
# Mac dept). Resolved by install-local-loop.sh to the sibling
# scripts/install-channel-patches.sh in the SAME bubble-ops-loop checkout that is
# rendering this wrapper. Runs in default (fail-open) mode: it never blocks the
# dept from starting, even if a patch fails to (re)apply. Omitted entirely
# (no-op) when not resolvable, so this stays backward-compatible with any caller
# that doesn't pass one.
render_loop_wrapper() {
    local dept_dir="$1" slug="$2" claude_bin="$3" tmux_bin="$4" tg_state="$5" extra_path="$6" workspace_dir="${7:-}" channel_patches_script="${8:-}"
    local add_dir_arg=""
    [[ -n "$workspace_dir" ]] && add_dir_arg=" --add-dir '${workspace_dir}'"

    # ── ALIGNMENT KNOBS (#748 / #1133) — optional, read from the env, safe defaults ──
    # These fold the customizations that were previously HAND-EDITED into each live
    # Mac wrapper (SOPS vault, inline-env for the tmux-server-env gotcha, model pin,
    # --chrome, --continue + resume-gate + fresh-fallback, per-agent env -u) back
    # into this single source of truth, PLUS the #1133 harness selector.
    local vault_path="${LOOP_VAULT_PATH:-}"                                   # SOPS vault; empty = no vault block
    local legacy_env="${LOOP_LEGACY_ENV:-${tg_state}/.env}"                   # plaintext fallback
    local age_key_file="${LOOP_AGE_KEY_FILE:-\$HOME/.config/sops/age/keys.txt}"
    local model="${LOOP_MODEL:-}"                                             # e.g. claude-opus-4-8[1m]; empty = no --model
    local chrome="${LOOP_CHROME:-}"                                          # 1 = add --chrome
    local do_continue="${LOOP_CONTINUE:-}"                                    # 1 = --continue + resume-gate + fresh-fallback
    # Vars passed INLINE into the tmux command (server env != wrapper env). DEFAULT
    # IS EMPTY — an uncustomized dept keeps the plain bare-exec launch and does NOT
    # get its token baked into the tmux argv (visible via ps); aligned agents that
    # NEED secrets to reach the harness opt in explicitly (--inline-env). This keeps
    # the no-knobs render behaviour-identical to the pre-alignment generic wrapper.
    local inline_env_vars="${LOOP_INLINE_ENV-}"
    local env_unset_vars="${LOOP_ENV_UNSET:-}"                               # e.g. CLAUDE_CODE_OAUTH_TOKEN (Géraldine keychain override)
    local selector_dir="${LOOP_HARNESS_SELECTOR_DIR:-\$HOME/Library/Application Support/bubble-ops-loop}"
    local hermes_bin="${LOOP_HERMES_BIN:-hermes}"
    # Extra per-agent wrapper exports (newline-separated KEY=VALUE entries, e.g.
    # Géraldine's PYTHONPATH for the department-onboarding-guide skill). Emitted
    # verbatim as `export KEY=VALUE` in the wrapper body — the caller owns the
    # value's quoting and any runtime refs ($HOME, ${PYTHONPATH:-} …) stay literal.
    local extra_exports="${LOOP_EXTRA_EXPORTS:-}"
    local extra_export_block="" _line
    if [[ -n "$extra_exports" ]]; then
        while IFS= read -r _line; do
            [[ -n "$_line" ]] && extra_export_block+="export ${_line}"$'\n'
        done <<<"$extra_exports"
    fi

    # SOPS_AGE_KEY_FILE export + vault-decrypt block (only when a vault is given).
    local age_export="" vault_block=""
    if [[ -n "$vault_path" ]]; then
        age_export="export SOPS_AGE_KEY_FILE=\"${age_key_file}\""
        vault_block="
# --- Load dept secrets: SOPS vault primary, legacy plaintext .env fallback ---
VAULT=\"${vault_path}\"
LEGACY_ENV=\"${legacy_env}\"
if [ -f \"\$VAULT\" ]; then
  _sec=\"\$(mktemp -t ${slug}-sops)\"
  chmod 600 \"\$_sec\"
  if sops --decrypt --output \"\$_sec\" \"\$VAULT\" 2>/dev/null; then
    set -a; . \"\$_sec\"; set +a
  fi
  rm -f \"\$_sec\"
fi
if [ -z \"\${TELEGRAM_BOT_TOKEN:-}\" ] && [ -f \"\$LEGACY_ENV\" ]; then
  echo \"[wrapper] vault yielded no token; falling back to legacy .env\" >&2
  set -a; . \"\$LEGACY_ENV\"; set +a
fi"
    else
        # No vault: preserve the original behaviour (source the dept .env if present).
        vault_block="
# Source the dept telegram bot env (sets TELEGRAM_BOT_TOKEN etc.) if present.
if [ -f \"${legacy_env}\" ]; then
  set -a; . \"${legacy_env}\"; set +a
fi"
    fi

    local patch_block=""
    if [[ -n "$channel_patches_script" ]]; then
        patch_block="
# Self-heal the telegram plugin's boot_rearm + bubble-inject patches before
# every launch (board #956) — fail-open, never blocks this dept from starting.
if [ -x '${channel_patches_script}' ]; then
  '${channel_patches_script}' >>\"${tg_state}/install-channel-patches.log\" 2>&1 || true
fi
"
    fi

    # env -u prefix (drop vars so a lower-precedence source wins, e.g. keychain).
    local env_unset_prefix="" _v
    for _v in $env_unset_vars; do env_unset_prefix+="-u ${_v} "; done
    [[ -n "$env_unset_prefix" ]] && env_unset_prefix="env ${env_unset_prefix}"
    # claude flags
    local chrome_flag="" model_flag=""
    [[ "$chrome" == "1" ]] && chrome_flag=" --chrome"
    [[ -n "$model" ]] && model_flag=" --model '${model}'"
    local cont_flag=""
    [[ "$do_continue" == "1" ]] && cont_flag="--continue"

    # Build the claude launch string (the inner command handed to tmux new-session).
    # When there is inline-env or env -u to apply, use the INLINE form: cd + a
    # TELEGRAM_STATE_DIR literal + each requested var as a runtime-expanded,
    # single-quoted assignment (survives tmux's re-parse) + env -u + exec. Otherwise
    # emit the plain bare-exec form (identical to the pre-alignment generic wrapper —
    # no secrets in argv). \$1 is the leading flag ("--continue" or "").
    local claude_flags="\$1${chrome_flag}${model_flag} --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official${add_dir_arg}"
    local claude_launch
    if [[ -n "$inline_env_vars" || -n "$env_unset_prefix" ]]; then
        local inline_prefix="TELEGRAM_STATE_DIR='${tg_state}' "
        for _v in $inline_env_vars; do inline_prefix+="${_v}='\${${_v}:-}' "; done
        claude_launch="cd '${dept_dir}' && ${inline_prefix}exec ${env_unset_prefix}'${claude_bin}' ${claude_flags}"
    else
        claude_launch="exec '${claude_bin}' ${claude_flags}"
    fi
    # Hermes launch (#1133): cd + inline PATH so the tmux SERVER env (which is NOT
    # this wrapper's env) still resolves the hermes binary + its profile cwd. Hermes
    # reads its own profile secrets, so no inline-env forwarding is needed.
    local hermes_launch="cd '${dept_dir}' && PATH='${extra_path}:\$PATH' exec ${hermes_bin} -p '${slug}' gateway run --replace"

    # Resume-gate + fresh-fallback blocks (only meaningful with --continue).
    local resume_block=""
    if [[ "$do_continue" == "1" ]]; then
        resume_block="
  # Auto-answer the resume gate: pick \"Resume full session as-is\" (option 2).
  # Poll the pane; stop early once the REPL is ready (small sessions: no gate).
  # NB: guarded with '|| true' so a transient non-zero (e.g. the session dying
  # mid-poll — exactly the race the fresh-fallback below handles) does NOT abort
  # the wrapper under 'set -e' before that fallback can run.
  for _i in \$(seq 1 30); do
    sleep 1
    \"\$TMUX_BIN\" has-session -t \"\$SESSION\" 2>/dev/null || break
    _pane=\"\$(\"\$TMUX_BIN\" capture-pane -t \"\$SESSION\" -p 2>/dev/null || true)\"
    if printf '%s' \"\$_pane\" | grep -q 'Resume full session as-is'; then
      \"\$TMUX_BIN\" send-keys -t \"\$SESSION\" Down Enter 2>/dev/null || true  # 1 -> 2, full resume
      break
    fi
    printf '%s' \"\$_pane\" | grep -q 'bypass permissions on' && break   # REPL ready, no gate
  done
  # Fresh-fallback: if --continue produced an unresumable (dead) session, retry
  # once WITHOUT --continue instead of crash-looping on KeepAlive.
  if ! \"\$TMUX_BIN\" has-session -t \"\$SESSION\" 2>/dev/null; then
    echo \"[wrapper] --continue session gone (unresumable?); retrying fresh\" >&2
    start_claude \"\"
  fi"
    fi

    cat <<WRAPPER
#!/bin/bash
# ops-loop LOCAL main-runner wrapper for dept '${slug}' (host: local).
# Rendered by install-local-loop.sh (ALIGNED — #748 single source of truth).
# Runs the selected HARNESS inside a tmux session "ops-loop-${slug}" so a human
# can \`tmux attach -t ops-loop-${slug}\` to watch it live; launchd (KeepAlive=true)
# supervises THIS wrapper and restarts it on crash. Mac twin of the VPS systemd
# dept unit + the #1133 harness selector (claude default | hermes).
set -e
export PATH="${extra_path}:\$PATH"
export TELEGRAM_STATE_DIR="${tg_state}"
export OPS_LOOP_DEPT="${slug}"
export BUBBLE_DEPT="${slug}"
export BUBBLE_HOST="local"
export OPS_LOOP_BOOT_REARM=1
${age_export}
${extra_export_block}cd "${dept_dir}"
${vault_block}
${patch_block}
TMUX_BIN="${tmux_bin}"
SESSION="ops-loop-${slug}"

# --- HARNESS SELECTOR (#1133): claude (default) | hermes ---
# The Mac twin of the VPS /etc/bubble-harness/<slug> selector. A switch flips this
# file then \`launchctl kickstart -k\` restarts the wrapper, which re-reads it here.
SELECTOR="${selector_dir}/harness-${slug}"
HARNESS="claude"
[ -f "\$SELECTOR" ] && HARNESS="\$(tr -d '[:space:]' < "\$SELECTOR" 2>/dev/null)"
[ -n "\$HARNESS" ] || HARNESS="claude"

CONT_FLAG="${cont_flag}"

# The inner command is handed to tmux new-session, which runs it in the tmux
# SERVER's global env (NOT this wrapper's env) — hence the inline cd/PATH/secrets.
start_claude() {  # \$1 = leading flag(s): "--continue" or "" (fresh)
  "\$TMUX_BIN" new-session -d -s "\$SESSION" \\
    "${claude_launch}"
}

start_hermes() {  # #1133 alternate harness — hermes reads its own profile (${slug})
  "\$TMUX_BIN" new-session -d -s "\$SESSION" \\
    "${hermes_launch}"
}

# Kill any stale session from a previous run so we never stack sessions.
"\$TMUX_BIN" kill-session -t "\$SESSION" 2>/dev/null || true

if [ "\$HARNESS" = "hermes" ]; then
  start_hermes
else
  start_claude "\$CONT_FLAG"${resume_block}
fi

# Block in the foreground until the session ends. When the harness exits, the
# session disappears, this loop ends, the wrapper exits non-zero, and launchd
# KeepAlive relaunches the wrapper (which recreates the session). Poll is cheap.
while "\$TMUX_BIN" has-session -t "\$SESSION" 2>/dev/null; do
  sleep 5
done

# Session gone => harness exited. Exit non-zero so launchd KeepAlive restarts us.
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

    <!-- The wrapper launches a PERSISTENT interactive \`claude\` Telegram-channels
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
