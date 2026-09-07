#!/usr/bin/env bash
# =============================================================================
# install-local-loop.sh — install the MAIN /loop runner for a host:local dept
# as a macOS launchd agent. The Mac twin of the VPS systemd dept unit
# (deploy/templates/ops-loop-dept.service.template). The VPS systemd/token-broker
# plumbing is replaced by Mac-native equivalents: secrets from a per-Mac SOPS
# vault decrypted IN the wrapper (--vault; legacy .env fallback), and push via the
# operator's own `gh`/git credential. Renders from ONE aligned wrapper (#748) with
# the harness selector (#1133): the per-agent customizations (vault, model pin,
# --chrome, --continue, inline-env, env -u) are FLAGS, not hand-edits.
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
# --telegram-state-dir / --extra-path / --workspace-dir / --channel-patches-script
# — any future local dept (ours or a client's) uses the same script. NOT
# Miranda-hardcoded.
#
# ALIGNMENT FLAGS (#748/#1133) — fold the per-agent customizations that had drifted
# into hand-edited live wrappers back into flags on this ONE installer:
#   --vault <path>           SOPS secrets.sops.env (this Mac's age key); no vault → source .env only
#   --legacy-env <path>      plaintext fallback (default <telegram-state-dir>/.env)
#   --age-key-file <path>    age key (default $HOME/.config/sops/age/keys.txt)
#   --model <val>            model pin, e.g. 'claude-opus-4-8[1m]' (avoids bare --model opus drift)
#   --chrome                 add --chrome
#   --continue               add --continue + resume-gate auto-answer + fresh-fallback
#   --inline-env "<VARS>"    space-sep var names passed INLINE into tmux (tmux server env != wrapper env);
#                            default TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN; pass "" for none
#   --env-unset "<VARS>"     space-sep var names to `env -u` before exec (e.g. CLAUDE_CODE_OAUTH_TOKEN)
#   --harness-selector-dir <dir>  dir holding harness-<slug> (default: wrapper dir)
#   --hermes-bin <path>      hermes binary (default: hermes)
#   --extra-export "KEY=VAL" extra wrapper export line, emitted verbatim as
#                            `export KEY=VAL` (repeatable; caller owns quoting —
#                            e.g. 'PYTHONPATH="$HOME/x:${PYTHONPATH:-}"'). Emitted
#                            BEFORE the vault decrypt, so a value must NOT depend
#                            on a vault-provided secret (it'd resolve empty). The
#                            rendered wrapper is bash -n'd, so a bad quote fails
#                            the install rather than shipping a broken wrapper.
# Every rendered wrapper reads <selector-dir>/harness-<slug> at launch (claude default | hermes).
#
# --channel-patches-script (board #956): re-applies the telegram plugin's
# boot_rearm + bubble-inject patches before every claude launch, so a plugin
# auto-update (which wipes the plugin cache dir) self-heals instead of
# silently breaking /loop re-arm or agent-to-agent inject. Defaults to the
# sibling scripts/install-channel-patches.sh in THIS checkout, resolved to an
# absolute path; pass "" to disable.
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
#                         [--log-dir <dir>] [--wrapper-dir <dir>]
#                         [--channel-patches-script <path>]
#                         [--vault <path>] [--legacy-env <path>] [--age-key-file <path>]
#                         [--model <val>] [--chrome] [--continue]
#                         [--inline-env "<VARS>"] [--env-unset "<VARS>"]
#                         [--harness-selector-dir <dir>] [--hermes-bin <path>]
#                         [--extra-export "KEY=VAL"]... [--activate]
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
# Board #956: self-heal the telegram plugin's boot_rearm + bubble-inject patches
# on every launch. Defaults to the sibling install-channel-patches.sh in THIS
# bubble-ops-loop checkout (the one running this installer) — resolved to an
# absolute path so the rendered wrapper keeps working even if this checkout
# later moves relative to the dept. Pass --channel-patches-script "" to opt out.
CHANNEL_PATCHES_SCRIPT="${LOCAL_LOOP_CHANNEL_PATCHES_SCRIPT-$SCRIPT_DIR/../../scripts/install-channel-patches.sh}"
ACTIVATE=0
UNINSTALL=0

# ── ALIGNMENT KNOBS (#748 / #1133) — fold the previously hand-edited per-agent
# customizations into flags so every Mac wrapper renders from this ONE source of
# truth. All optional; defaults reproduce the plain generic wrapper. They flow to
# render_loop_wrapper as LOOP_* env vars just before the render call.
VAULT_PATH="${LOCAL_LOOP_VAULT_PATH:-}"                 # SOPS vault (secrets.sops.env); empty = no vault block
LEGACY_ENV_PATH="${LOCAL_LOOP_LEGACY_ENV:-}"            # plaintext fallback (default: <tg-state>/.env)
AGE_KEY_FILE="${LOCAL_LOOP_AGE_KEY_FILE:-}"             # SOPS age key (default: $HOME/.config/sops/age/keys.txt)
MODEL_PIN="${LOCAL_LOOP_MODEL:-}"                       # e.g. claude-opus-4-8[1m]; empty = no --model
USE_CHROME="${LOCAL_LOOP_CHROME:-}"                     # 1 = --chrome
USE_CONTINUE="${LOCAL_LOOP_CONTINUE:-}"                 # 1 = --continue + resume-gate + fresh-fallback
INLINE_ENV="${LOCAL_LOOP_INLINE_ENV-__RENDER_DEFAULT__}"  # space-sep var names passed INLINE into tmux; sentinel = leave the render default (TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN)
ENV_UNSET="${LOCAL_LOOP_ENV_UNSET:-}"                   # space-sep var names to env -u (e.g. CLAUDE_CODE_OAUTH_TOKEN)
HARNESS_SELECTOR_DIR="${LOCAL_LOOP_HARNESS_SELECTOR_DIR:-}"  # dir holding harness-<slug> (default: wrapper dir)
HERMES_BIN="${LOCAL_LOOP_HERMES_BIN:-}"                 # hermes binary (default: hermes)
EXTRA_EXPORTS="${LOCAL_LOOP_EXTRA_EXPORTS:-}"           # newline-joined KEY=VALUE extra wrapper exports (repeatable --extra-export)

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
        --channel-patches-script)   CHANNEL_PATCHES_SCRIPT="${2-}"; shift 2 ;;
        --channel-patches-script=*) CHANNEL_PATCHES_SCRIPT="${1#--channel-patches-script=}"; shift ;;
        --vault)              VAULT_PATH="${2:?}"; shift 2 ;;
        --vault=*)            VAULT_PATH="${1#--vault=}"; shift ;;
        --legacy-env)         LEGACY_ENV_PATH="${2:?}"; shift 2 ;;
        --legacy-env=*)       LEGACY_ENV_PATH="${1#--legacy-env=}"; shift ;;
        --age-key-file)       AGE_KEY_FILE="${2:?}"; shift 2 ;;
        --age-key-file=*)     AGE_KEY_FILE="${1#--age-key-file=}"; shift ;;
        --model)              MODEL_PIN="${2:?}"; shift 2 ;;
        --model=*)            MODEL_PIN="${1#--model=}"; shift ;;
        --chrome)             USE_CHROME=1; shift ;;
        --continue)           USE_CONTINUE=1; shift ;;
        --inline-env)         INLINE_ENV="${2-}"; shift 2 ;;
        --inline-env=*)       INLINE_ENV="${1#--inline-env=}"; shift ;;
        --env-unset)          ENV_UNSET="${2-}"; shift 2 ;;
        --env-unset=*)        ENV_UNSET="${1#--env-unset=}"; shift ;;
        --harness-selector-dir)   HARNESS_SELECTOR_DIR="${2:?}"; shift 2 ;;
        --harness-selector-dir=*) HARNESS_SELECTOR_DIR="${1#--harness-selector-dir=}"; shift ;;
        --hermes-bin)         HERMES_BIN="${2:?}"; shift 2 ;;
        --hermes-bin=*)       HERMES_BIN="${1#--hermes-bin=}"; shift ;;
        --extra-export)       EXTRA_EXPORTS+="${EXTRA_EXPORTS:+$'\n'}${2:?}"; shift 2 ;;
        --extra-export=*)     EXTRA_EXPORTS+="${EXTRA_EXPORTS:+$'\n'}${1#--extra-export=}"; shift ;;
        --activate)           ACTIVATE=1; shift ;;
        --uninstall)          UNINSTALL=1; shift ;;
        -h|--help)            sed -n '2,90p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

# Resolve channel-patches-script to an absolute path if it exists; otherwise
# warn + drop it (fail-open at install time too — never block the dept install
# over a missing optional self-heal script).
if [[ -n "$CHANNEL_PATCHES_SCRIPT" ]]; then
    if [[ -f "$CHANNEL_PATCHES_SCRIPT" ]]; then
        CHANNEL_PATCHES_SCRIPT="$(cd "$(dirname "$CHANNEL_PATCHES_SCRIPT")" && pwd)/$(basename "$CHANNEL_PATCHES_SCRIPT")"
        chmod +x "$CHANNEL_PATCHES_SCRIPT" 2>/dev/null || true
    else
        say "WARNING: channel-patches-script '$CHANNEL_PATCHES_SCRIPT' not found — wrapper will NOT self-heal telegram plugin patches (pass --channel-patches-script to fix, or --channel-patches-script \"\" to silence this)"
        CHANNEL_PATCHES_SCRIPT=""
    fi
fi

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
say "  channel-patches-script = ${CHANNEL_PATCHES_SCRIPT:-<none> (patch self-heal disabled)}"
say "  wrapper       = $WRAPPER_PATH"
say "  plist         = $PLIST_PATH"

# 1) Render the aligned persistent-session wrapper (the Mac twin of the VPS
#    systemd ExecStart): the selected harness (claude default | hermes) inside
#    tmux, KeepAlive-supervised. The #748/#1133 alignment knobs flow to
#    render_loop_wrapper via LOOP_* env vars (only the ones the caller set).
# Selector lives WITH the wrapper unless overridden, so --wrapper-dir moves both.
[[ -n "$HARNESS_SELECTOR_DIR" ]] || HARNESS_SELECTOR_DIR="$WRAPPER_DIR"
[[ -n "$VAULT_PATH" ]]           && export LOOP_VAULT_PATH="$VAULT_PATH"
[[ -n "$LEGACY_ENV_PATH" ]]      && export LOOP_LEGACY_ENV="$LEGACY_ENV_PATH"
[[ -n "$AGE_KEY_FILE" ]]         && export LOOP_AGE_KEY_FILE="$AGE_KEY_FILE"
[[ -n "$MODEL_PIN" ]]            && export LOOP_MODEL="$MODEL_PIN"
[[ -n "$USE_CHROME" ]]           && export LOOP_CHROME="$USE_CHROME"
[[ -n "$USE_CONTINUE" ]]         && export LOOP_CONTINUE="$USE_CONTINUE"
[[ "$INLINE_ENV" != "__RENDER_DEFAULT__" ]] && export LOOP_INLINE_ENV="$INLINE_ENV"
[[ -n "$ENV_UNSET" ]]            && export LOOP_ENV_UNSET="$ENV_UNSET"
[[ -n "$HARNESS_SELECTOR_DIR" ]] && export LOOP_HARNESS_SELECTOR_DIR="$HARNESS_SELECTOR_DIR"
[[ -n "$HERMES_BIN" ]]           && export LOOP_HERMES_BIN="$HERMES_BIN"
[[ -n "$EXTRA_EXPORTS" ]]        && export LOOP_EXTRA_EXPORTS="$EXTRA_EXPORTS"
render_loop_wrapper "$DEPT_DIR" "$SLUG" "$CLAUDE_BIN" "$TMUX_BIN" "$TELEGRAM_STATE_DIR" "$EXTRA_PATH" "$WORKSPACE_DIR" "$CHANNEL_PATCHES_SCRIPT" > "$WRAPPER_PATH" \
    || die "failed to render wrapper to $WRAPPER_PATH"
# Syntax-gate the rendered wrapper BEFORE it can be activated — the twin of the
# plist's plutil -lint below. Catches a malformed knob (esp. a fat-fingered
# --extra-export with an unbalanced quote, which is emitted verbatim) at install
# time instead of silently installing a wrapper that only fails at launchd/tmux
# exec time. On failure, remove the broken wrapper so a prior good one isn't
# shadowed by an unrunnable file.
if ! bash -n "$WRAPPER_PATH" 2>/tmp/.wrapper-lint.$$; then
    say "rendered wrapper failed bash -n: $(cat /tmp/.wrapper-lint.$$ 2>/dev/null)"
    rm -f "$WRAPPER_PATH" /tmp/.wrapper-lint.$$
    die "rendered wrapper is not valid bash (check --extra-export / other knob quoting): $WRAPPER_PATH"
fi
rm -f /tmp/.wrapper-lint.$$
chmod +x "$WRAPPER_PATH"
say "wrote $WRAPPER_PATH (chmod +x, bash -n OK)"

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
