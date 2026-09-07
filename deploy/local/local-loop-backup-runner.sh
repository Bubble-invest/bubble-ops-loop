#!/usr/bin/env bash
# Mac backup floor: wake the existing persistent session when its heartbeat is stale.
# This runner never launches Claude, Hermes, or any other model process.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/local_loop_lib.sh
. "$SCRIPT_DIR/lib/local_loop_lib.sh"

DEPT_DIR=""
SLUG=""
TELEGRAM_STATE_DIR=""
SESSION_NAME=""
HARNESS_SELECTOR=""
TMUX_BIN="${LOCAL_LOOP_TMUX_BIN:-tmux}"
STALE_SEC="$LOCAL_LOOP_STALE_SEC_DEFAULT"
COOLDOWN_SEC="${LOCAL_LOOP_BACKUP_COOLDOWN_SEC:-900}"
ACTIVATE_INJECT=0
LEGACY_ACTIVATION=0

die() { echo "ERR: $*" >&2; exit 2; }
TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [local-loop-backup:${SLUG:-unknown}] $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir) DEPT_DIR="${2:?--dept-dir needs a value}"; shift 2 ;;
        --dept-dir=*) DEPT_DIR="${1#--dept-dir=}"; shift ;;
        --slug) SLUG="${2:?--slug needs a value}"; shift 2 ;;
        --slug=*) SLUG="${1#--slug=}"; shift ;;
        --telegram-state-dir) TELEGRAM_STATE_DIR="${2:?--telegram-state-dir needs a value}"; shift 2 ;;
        --telegram-state-dir=*) TELEGRAM_STATE_DIR="${1#--telegram-state-dir=}"; shift ;;
        --session-name) SESSION_NAME="${2:?--session-name needs a value}"; shift 2 ;;
        --session-name=*) SESSION_NAME="${1#--session-name=}"; shift ;;
        --harness-selector) HARNESS_SELECTOR="${2:?--harness-selector needs a value}"; shift 2 ;;
        --harness-selector=*) HARNESS_SELECTOR="${1#--harness-selector=}"; shift ;;
        --tmux-bin) TMUX_BIN="${2:?--tmux-bin needs a value}"; shift 2 ;;
        --tmux-bin=*) TMUX_BIN="${1#--tmux-bin=}"; shift ;;
        --stale-sec) STALE_SEC="${2:?--stale-sec needs a value}"; shift 2 ;;
        --stale-sec=*) STALE_SEC="${1#--stale-sec=}"; shift ;;
        --cooldown-sec) COOLDOWN_SEC="${2:?--cooldown-sec needs a value}"; shift 2 ;;
        --cooldown-sec=*) COOLDOWN_SEC="${1#--cooldown-sec=}"; shift ;;
        --activate-inject) ACTIVATE_INJECT=1; shift ;;
        # Old rendered plists may carry these. Accept data-only flags so a
        # runner-first rollout fails clearly instead of launching a model.
        --claude-bin|--workspace-dir|--extra-path) shift 2 ;;
        --claude-bin=*|--workspace-dir=*|--extra-path=*) shift ;;
        --activate-tick) LEGACY_ACTIVATION=1; shift ;;
        -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

[[ "$SLUG" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "invalid --slug"
[[ -n "$DEPT_DIR" && "$DEPT_DIR" == /* ]] || die "--dept-dir must be absolute"
[[ -n "$TELEGRAM_STATE_DIR" && "$TELEGRAM_STATE_DIR" == /* ]] || die "--telegram-state-dir must be absolute"
[[ -n "$SESSION_NAME" ]] || die "--session-name is required"
[[ -n "$HARNESS_SELECTOR" && "$HARNESS_SELECTOR" == /* ]] || die "--harness-selector must be absolute"
[[ "$STALE_SEC" =~ ^[0-9]+$ ]] || die "--stale-sec must be an integer"
[[ "$COOLDOWN_SEC" =~ ^[0-9]+$ ]] || die "--cooldown-sec must be an integer"
[[ "$LEGACY_ACTIVATION" == 0 ]] || { log "ERROR: legacy --activate-tick is disabled; re-render this LaunchAgent for existing-session injection"; exit 64; }

NOW_EPOCH="${LOCAL_LOOP_NOW_EPOCH:-$(date +%s)}"
[[ "$NOW_EPOCH" =~ ^[0-9]+$ ]] || die "LOCAL_LOOP_NOW_EPOCH must be an integer"
STATE="$(is_heartbeat_stale "$DEPT_DIR" "$STALE_SEC" "$NOW_EPOCH")"
if [[ "$STATE" == "fresh" ]]; then
    log "heartbeat FRESH (<= ${STALE_SEC}s) — existing session healthy"
    exit 0
fi
log "heartbeat STALE (> ${STALE_SEC}s, or missing) — existing-session wake required"

if [[ "$ACTIVATE_INJECT" != 1 ]]; then
    log "DEFERRED: injection not activated; no model/session was launched"
    exit 1
fi
if [[ -L "$HARNESS_SELECTOR" ]]; then
    log "ERROR: harness selector is a symlink; injection not written"
    exit 1
fi
HARNESS="claude"
if [[ -e "$HARNESS_SELECTOR" && ! -f "$HARNESS_SELECTOR" ]]; then
    log "ERROR: harness selector is not a regular file; injection not written"
    exit 1
fi
if [[ -f "$HARNESS_SELECTOR" ]]; then
    if [[ ! -r "$HARNESS_SELECTOR" ]]; then
        log "ERROR: harness selector is unreadable; injection not written"
        exit 1
    fi
    HARNESS="$(tr -d '[:space:]' <"$HARNESS_SELECTOR" 2>/dev/null)" || {
        log "ERROR: harness selector read failed; injection not written"
        exit 1
    }
fi
case "$HARNESS" in
    ""|claude) ;;
    hermes)
        log "DEFERRED: Hermes is active and has no reviewed channel-inject consumer; injection not written"
        exit 1
        ;;
    *)
        log "ERROR: unsupported harness selector; injection not written"
        exit 1
        ;;
esac
if [[ ! -x "$TMUX_BIN" ]] || ! "$TMUX_BIN" has-session -t "$SESSION_NAME" 2>/dev/null; then
    log "ERROR: existing session unavailable; injection not written"
    exit 1
fi

WAKE_MESSAGE='Resume your OODA loop (self-paced). Run one normal full tick now: STEP A safe pull, STEP B read queues, STEP C apply the existing layer/mission/approval gates, STEP D dispatch only the selected work, STEP E commit only allowed runtime paths, STEP F notify, then arm the next normal wake. Preserve every human approval gate.'
RESULT="$(inject_loop_wake "$TELEGRAM_STATE_DIR" "$SLUG" "$NOW_EPOCH" "$COOLDOWN_SEC" "$WAKE_MESSAGE")" || {
    log "ERROR: secure existing-session injection failed"
    exit 1
}
case "$RESULT" in
    injected) log "WAKE_APPENDED_UNCONFIRMED: existing-session inbox accepted one wake; delivery, execution, and heartbeat advancement are not yet confirmed" ;;
    cooldown) log "COOLDOWN: a recent wake is already pending; no duplicate appended" ;;
    *) log "ERROR: unexpected injection result"; exit 1 ;;
esac
exit 0
