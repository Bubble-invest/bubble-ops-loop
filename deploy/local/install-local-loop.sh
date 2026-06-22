#!/usr/bin/env bash
# =============================================================================
# install-local-loop.sh — install the MAIN /loop runner for a host:local dept
# as a macOS launchd agent. The Mac twin of the VPS systemd dept unit
# (deploy/templates/ops-loop-dept.service.template), with the VPS-only plumbing
# (systemd, SOPS env pre-decrypt, token-broker, tmpfs) DROPPED: on the Mac the
# dept pushes via the operator's own `gh`/git credential.
#
# WHAT IT INSTALLS:
#   ~/Library/Application Support/bubble-ops-loop/ops-loop-<slug>-wrapper.sh
#   ~/Library/LaunchAgents/com.bubble.ops-loop-<slug>.plist
#   A KeepAlive launchd agent that supervises a generic WRAPPER which launches a
#   PERSISTENT interactive `claude --dangerously-skip-permissions --channels
#   plugin:telegram@claude-plugins-official` session inside a tmux session
#   (ops-loop-<slug>). The dept's CLAUDE.md /loop protocol + its OWN armed /loop
#   cron drive the STEP A-F tick; its Telegram bot reaches {{OPERATOR_2}}/{{OPERATOR}}. This is the
#   exact Mac twin of the VPS systemd dept unit's interactive `--channels` runner.
#
# DOCTRINE — KeepAlive (NOT StartInterval/`claude -p`): the runner is a long-lived
# interactive session (the dept needs its Telegram channel + hooks; `claude -p`
# would lose both — VPS Ban #2). launchd KeepAlive relaunches the wrapper on
# crash; RunAtLoad starts it on login/wake. A tick missed while the Mac was
# asleep is caught by decide_dispatch's morning-floor on the first post-wake tick;
# the separate backup floor (install-local-loop-backup.sh) is the stale-heartbeat
# backstop. See deploy/local/README.md.
#
# GENERIC: parameterized by --dept-dir / --slug / --claude-bin / --tmux-bin /
# --telegram-state-dir / --extra-path / --workspace-dir — any future local dept
# (ours or a client's) uses the same script. NOT Miranda-hardcoded.
#
# --workspace-dir (brain↔body): a host:local dept that REUSES an existing
# workspace's skills/tools (e.g. Miranda → Miranda_Socials, whose 8 skills live
# only in its .claude/skills, not at user scope) passes its workspace here. It is
# rendered into the wrapper as `claude --add-dir <workspace-dir>`, which loads
# that dir's .claude/skills automatically while cwd stays the dept repo (so the
# loop's outputs/queues/inbox + git push resolve natively). Omit it for a
# self-contained dept that ships its own skills.
#
# TEST-SAFE: WITHOUT --activate it only RENDERS the wrapper + plist (writes the
# files + prints what it would do) and NEVER calls launchctl. `launchctl load`
# happens ONLY when --activate is passed. Idempotent: re-running overwrites the
# wrapper + plist (and reloads under --activate). --uninstall removes both.
#
# Usage:
#   install-local-loop.sh --dept-dir <path> --slug <slug>
#                         [--workspace-dir <dir>] [--claude-bin <path>]
#                         [--tmux-bin <path>] [--telegram-state-dir <dir>]
#                         [--extra-path <PATH>] [--launch-agents-dir <dir>]
#                         [--log-dir <dir>] [--wrapper-dir <dir>] [--activate]
#   install-local-loop.sh --uninstall --slug <slug> [--launch-agents-dir <dir>]
#                         [--wrapper-dir <dir>]
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/local_loop_lib.sh
. "$SCRIPT_DIR/lib/local_loop_lib.sh"

DEPT_DIR=""
SLUG=""
LAUNCH_AGENTS_DIR="${LOCAL_LOOP_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOCAL_LOOP_LOG_DIR:-$HOME/Library/Logs/bubble-ops-loop}"
WRAPPER_DIR="${LOCAL_LOOP_WRAPPER_DIR:-$HOME/Library/Application Support/bubble-ops-loop}"
CLAUDE_BIN="${LOCAL_LOOP_CLAUDE_BIN:-claude}"
TMUX_BIN="${LOCAL_LOOP_TMUX_BIN:-tmux}"
TELEGRAM_STATE_DIR=""               # default derived from slug below if unset
EXTRA_PATH="${LOCAL_LOOP_EXTRA_PATH:-/opt/homebrew/bin:$HOME/.bun/bin:$HOME/.npm-global/bin}"
WORKSPACE_DIR="${LOCAL_LOOP_WORKSPACE_DIR:-}"   # optional: existing workspace whose .claude/skills the dept reuses (passed as --add-dir)
ACTIVATE=0
UNINSTALL=0

die() { echo "ERR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir)           DEPT_DIR="${2:?--dept-dir needs a value}"; shift 2 ;;
        --dept-dir=*)         DEPT_DIR="${1#--dept-dir=}"; shift ;;
        --slug)               SLUG="${2:?--slug needs a value}"; shift 2 ;;
        --slug=*)             SLUG="${1#--slug=}"; shift ;;
        --launch-agents-dir)  LAUNCH_AGENTS_DIR="${2:?}"; shift 2 ;;
        --launch-agents-dir=*) LAUNCH_AGENTS_DIR="${1#--launch-agents-dir=}"; shift ;;
        --log-dir)            LOG_DIR="${2:?}"; shift 2 ;;
        --log-dir=*)          LOG_DIR="${1#--log-dir=}"; shift ;;
        --wrapper-dir)        WRAPPER_DIR="${2:?}"; shift 2 ;;
        --wrapper-dir=*)      WRAPPER_DIR="${1#--wrapper-dir=}"; shift ;;
        --claude-bin)         CLAUDE_BIN="${2:?}"; shift 2 ;;
        --claude-bin=*)       CLAUDE_BIN="${1#--claude-bin=}"; shift ;;
        --tmux-bin)           TMUX_BIN="${2:?}"; shift 2 ;;
        --tmux-bin=*)         TMUX_BIN="${1#--tmux-bin=}"; shift ;;
        --telegram-state-dir) TELEGRAM_STATE_DIR="${2:?}"; shift 2 ;;
        --telegram-state-dir=*) TELEGRAM_STATE_DIR="${1#--telegram-state-dir=}"; shift ;;
        --extra-path)         EXTRA_PATH="${2:?}"; shift 2 ;;
        --extra-path=*)       EXTRA_PATH="${1#--extra-path=}"; shift ;;
        --workspace-dir)      WORKSPACE_DIR="${2:?}"; shift 2 ;;
        --workspace-dir=*)    WORKSPACE_DIR="${1#--workspace-dir=}"; shift ;;
        --activate)           ACTIVATE=1; shift ;;
        --uninstall)          UNINSTALL=1; shift ;;
        -h|--help)            sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

[[ -n "$SLUG" ]] || die "--slug is required"
# Default telegram state dir matches the workspace convention (channels/telegram-<slug>).
[[ -n "$TELEGRAM_STATE_DIR" ]] || TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-${SLUG}"
LABEL="com.bubble.ops-loop-${SLUG}"
PLIST_PATH="${LAUNCH_AGENTS_DIR%/}/${LABEL}.plist"
WRAPPER_PATH="${WRAPPER_DIR%/}/ops-loop-${SLUG}-wrapper.sh"

say() { echo "[install-local-loop] $*"; }

# ── uninstall ────────────────────────────────────────────────────────────────
if [[ "$UNINSTALL" == "1" ]]; then
    say "uninstalling $LABEL"
    if [[ "$ACTIVATE" == "1" ]]; then
        # Only touch launchctl when explicitly activated (mirrors install).
        say "launchctl unload '$PLIST_PATH'"
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    else
        say "(dry) would: launchctl unload '$PLIST_PATH'  (pass --activate to actually unload)"
    fi
    for f in "$PLIST_PATH" "$WRAPPER_PATH"; do
        if [[ -f "$f" ]]; then
            rm -f "$f" && say "removed $f"
        else
            say "no file at $f — nothing to remove"
        fi
    done
    exit 0
fi

# ── install / render ─────────────────────────────────────────────────────────
[[ -n "$DEPT_DIR" ]] || die "--dept-dir is required (the dept repo clone on the Mac)"

# Warn (don't fail) if the dept dir doesn't exist yet — install can precede the
# clone in a scripted bring-up; the agent simply does nothing until it appears.
[[ -d "$DEPT_DIR" ]] || say "WARNING: dept-dir '$DEPT_DIR' does not exist yet (agent will idle until it does)"
[[ -z "$WORKSPACE_DIR" || -d "$WORKSPACE_DIR" ]] || say "WARNING: workspace-dir '$WORKSPACE_DIR' does not exist (--add-dir would be skipped by claude)"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$WRAPPER_DIR"

say "rendering main /loop runner (persistent KeepAlive session):"
say "  label         = $LABEL"
say "  dept-dir      = $DEPT_DIR"
say "  slug          = $SLUG"
say "  claude        = $CLAUDE_BIN"
say "  tmux          = $TMUX_BIN"
say "  telegram-dir  = $TELEGRAM_STATE_DIR"
say "  extra-path    = $EXTRA_PATH"
say "  workspace-dir = ${WORKSPACE_DIR:-<none> (no --add-dir; dept must hold its own skills)}"
say "  wrapper       = $WRAPPER_PATH"
say "  plist         = $PLIST_PATH"

# 1) Render the generic persistent-session wrapper (the Mac twin of the VPS
#    systemd ExecStart): claude --channels telegram inside tmux, KeepAlive-supervised.
render_loop_wrapper "$DEPT_DIR" "$SLUG" "$CLAUDE_BIN" "$TMUX_BIN" "$TELEGRAM_STATE_DIR" "$EXTRA_PATH" "$WORKSPACE_DIR" > "$WRAPPER_PATH" \
    || die "failed to render wrapper to $WRAPPER_PATH"
chmod +x "$WRAPPER_PATH"
say "wrote $WRAPPER_PATH (chmod +x)"

# 2) Render the launchd plist (KeepAlive) that supervises the wrapper.
#    launchd does NOT expand $HOME inside plist <string> values, so expand it for
#    the plist's PATH (the wrapper keeps the literal $HOME — it's a real shell).
PLIST_PATH_ENV="${EXTRA_PATH//\$HOME/$HOME}"
render_loop_plist "$LABEL" "$WRAPPER_PATH" "$DEPT_DIR" "$SLUG" "$LOG_DIR" "$PLIST_PATH_ENV" > "$PLIST_PATH" \
    || die "failed to render plist to $PLIST_PATH"
say "wrote $PLIST_PATH"

# Validate the rendered plist if plutil is present (Mac).
if command -v plutil >/dev/null 2>&1; then
    if plutil -lint "$PLIST_PATH" >/dev/null 2>&1; then
        say "plutil -lint OK"
    else
        die "rendered plist failed plutil -lint: $PLIST_PATH"
    fi
fi

if [[ "$ACTIVATE" == "1" ]]; then
    say "activating: launchctl unload (if loaded) then load '$PLIST_PATH'"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH" || die "launchctl load failed"
    say "ACTIVATED — $LABEL is now a persistent KeepAlive session (tmux: ops-loop-${SLUG})."
else
    say "DRY RENDER complete (no launchctl). To activate:"
    say "  launchctl load '$PLIST_PATH'   # or re-run with --activate"
fi
