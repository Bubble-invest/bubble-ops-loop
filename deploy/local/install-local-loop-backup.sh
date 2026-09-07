#!/usr/bin/env bash
# Render/install the Mac existing-session injection floor. Activation is explicit.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/local_loop_lib.sh
. "$SCRIPT_DIR/lib/local_loop_lib.sh"

DEPT_DIR=""; SLUG=""; TELEGRAM_STATE_DIR=""; SESSION_NAME=""; HARNESS_SELECTOR=""
TMUX_BIN="${LOCAL_LOOP_TMUX_BIN:-tmux}"
INTERVAL=10800
STALE_SEC="$LOCAL_LOOP_STALE_SEC_DEFAULT"
COOLDOWN_SEC="${LOCAL_LOOP_BACKUP_COOLDOWN_SEC:-900}"
LAUNCH_AGENTS_DIR="${LOCAL_LOOP_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOCAL_LOOP_LOG_DIR:-$HOME/Library/Logs/bubble-ops-loop}"
RUNNER="${LOCAL_LOOP_BACKUP_RUNNER:-$SCRIPT_DIR/local-loop-backup-runner.sh}"
ACTIVATE=0; UNINSTALL=0

die() { echo "ERR: $*" >&2; exit 2; }
say() { echo "[install-local-loop-backup] $*"; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir) DEPT_DIR="${2:?}"; shift 2;; --dept-dir=*) DEPT_DIR="${1#*=}"; shift;;
        --slug) SLUG="${2:?}"; shift 2;; --slug=*) SLUG="${1#*=}"; shift;;
        --telegram-state-dir) TELEGRAM_STATE_DIR="${2:?}"; shift 2;; --telegram-state-dir=*) TELEGRAM_STATE_DIR="${1#*=}"; shift;;
        --session-name) SESSION_NAME="${2:?}"; shift 2;; --session-name=*) SESSION_NAME="${1#*=}"; shift;;
        --harness-selector) HARNESS_SELECTOR="${2:?}"; shift 2;; --harness-selector=*) HARNESS_SELECTOR="${1#*=}"; shift;;
        --tmux-bin) TMUX_BIN="${2:?}"; shift 2;; --tmux-bin=*) TMUX_BIN="${1#*=}"; shift;;
        --interval) INTERVAL="${2:?}"; shift 2;; --interval=*) INTERVAL="${1#*=}"; shift;;
        --stale-sec) STALE_SEC="${2:?}"; shift 2;; --stale-sec=*) STALE_SEC="${1#*=}"; shift;;
        --cooldown-sec) COOLDOWN_SEC="${2:?}"; shift 2;; --cooldown-sec=*) COOLDOWN_SEC="${1#*=}"; shift;;
        --launch-agents-dir) LAUNCH_AGENTS_DIR="${2:?}"; shift 2;; --launch-agents-dir=*) LAUNCH_AGENTS_DIR="${1#*=}"; shift;;
        --log-dir) LOG_DIR="${2:?}"; shift 2;; --log-dir=*) LOG_DIR="${1#*=}"; shift;;
        --runner) RUNNER="${2:?}"; shift 2;; --runner=*) RUNNER="${1#*=}"; shift;;
        --activate) ACTIVATE=1; shift;; --uninstall) UNINSTALL=1; shift;;
        -h|--help) sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0;;
        *) die "unknown argument '$1'";;
    esac
done
[[ "$SLUG" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "invalid --slug"
LABEL="com.bubble.ops-loop-backup-${SLUG}"
PLIST_PATH="${LAUNCH_AGENTS_DIR%/}/${LABEL}.plist"

if [[ "$UNINSTALL" == 1 ]]; then
    if [[ "$ACTIVATE" == 1 ]]; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; fi
    if [[ -f "$PLIST_PATH" ]]; then rm -f "$PLIST_PATH"; fi
    say "uninstalled $LABEL"
    exit 0
fi

[[ -n "$DEPT_DIR" && "$DEPT_DIR" == /* ]] || die "--dept-dir must be absolute"
[[ -n "$TELEGRAM_STATE_DIR" && "$TELEGRAM_STATE_DIR" == /* ]] || die "--telegram-state-dir must be absolute"
[[ -n "$SESSION_NAME" ]] || die "--session-name is required"
[[ "$INTERVAL" =~ ^[0-9]+$ && "$STALE_SEC" =~ ^[0-9]+$ && "$COOLDOWN_SEC" =~ ^[0-9]+$ ]] || die "intervals must be integers"
[[ -f "$RUNNER" ]] || die "backup runner not found"
[[ -n "$HARNESS_SELECTOR" ]] || HARNESS_SELECTOR="$HOME/Library/Application Support/bubble-ops-loop/harness-$SLUG"
[[ "$HARNESS_SELECTOR" == /* ]] || die "--harness-selector must be absolute"
mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" || die "cannot create launch/log directories"

CANDIDATE="$(mktemp "${LAUNCH_AGENTS_DIR%/}/.${LABEL}.candidate.XXXXXX")" || die "cannot create private candidate"
chmod 600 "$CANDIDATE"
cleanup_candidate() { [[ -n "${CANDIDATE:-}" && -f "$CANDIDATE" ]] && rm -f "$CANDIDATE"; }
trap cleanup_candidate EXIT INT TERM
if ! render_backup_plist "$LABEL" "$DEPT_DIR" "$SLUG" "$INTERVAL" "$RUNNER" "$LOG_DIR" \
    "$TELEGRAM_STATE_DIR" "$SESSION_NAME" "$HARNESS_SELECTOR" "$TMUX_BIN" "$STALE_SEC" "$COOLDOWN_SEC" >"$CANDIDATE"; then
    die "failed to render private plist candidate"
fi
if command -v plutil >/dev/null 2>&1; then plutil -lint "$CANDIDATE" >/dev/null || die "candidate plist lint failed; existing file untouched"; fi

DOMAIN="gui/$(id -u)"
WAS_LOADED=0
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && WAS_LOADED=1
OLD_EXISTS=0; BACKUP=""
if [[ -f "$PLIST_PATH" ]]; then
    OLD_EXISTS=1
    BACKUP="$(mktemp "${LAUNCH_AGENTS_DIR%/}/.${LABEL}.previous.XXXXXX")" || die "cannot preserve existing plist"
    chmod 600 "$BACKUP"
    cp -p "$PLIST_PATH" "$BACKUP" || die "cannot copy existing plist"
    chmod 600 "$BACKUP"
elif [[ "$ACTIVATE" == 1 && "$WAS_LOADED" == 1 ]]; then
    die "loaded job has no plist to restore; refusing activation"
fi
mv -f "$CANDIDATE" "$PLIST_PATH" || die "atomic plist publication failed"
CANDIDATE=""
chmod 600 "$PLIST_PATH"
say "published validated plist $PLIST_PATH; loaded state unchanged"

restore_previous() {
    local restore
    restore="$(mktemp "${LAUNCH_AGENTS_DIR%/}/.${LABEL}.restore.XXXXXX")" || return 1
    cp -p "$BACKUP" "$restore" || { rm -f "$restore"; return 1; }
    chmod 600 "$restore" || { rm -f "$restore"; return 1; }
    mv -f "$restore" "$PLIST_PATH"
}

if [[ "$ACTIVATE" != 1 ]]; then
    say "not activated; review then re-run with --activate"
    exit 0
fi

if [[ "$WAS_LOADED" == 1 ]]; then
    if ! launchctl bootout "$DOMAIN/$LABEL"; then
        [[ "$OLD_EXISTS" == 1 ]] && restore_previous
        die "could not stop prior backup job; prior registration remains"
    fi
fi
if ! launchctl bootstrap "$DOMAIN" "$PLIST_PATH"; then
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    if [[ "$OLD_EXISTS" == 1 ]]; then
        restore_previous || die "activation failed and prior plist restore failed"
        if [[ "$WAS_LOADED" == 1 ]]; then
            launchctl bootstrap "$DOMAIN" "$PLIST_PATH" || die "activation failed; prior file restored but prior registration restore failed"
        fi
    else
        rm -f "$PLIST_PATH"
    fi
    die "activation failed; prior file/registration restored"
fi
say "ACTIVATED $LABEL (existing-session injection only)"
