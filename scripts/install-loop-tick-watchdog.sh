#!/usr/bin/env bash
# install-loop-tick-watchdog.sh — install the ops-loop TICK watchdog (board #724)
# on the VPS: ONE systemd timer that re-kicks any dept loop whose tick died
# mid-flight on a transient API error (inject a re-arm turn into the LIVE
# session; stop→start only if the session proved deaf to the inject).
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run on
# every deploy. Mirrors scripts/install-loop-backup.sh.
#
# WHAT IT INSTALLS
#   /etc/systemd/system/loop-tick-watchdog.service   (oneshot, User=claude)
#   /etc/systemd/system/loop-tick-watchdog.timer     (every 10 min)
#   Requires: the `claude` user's EXISTING scoped sudoers grant
#   (`systemctl stop|start ops-loop-*`) — already present for the
#   telegram-watchdog; nothing new is granted here.
#
# WHY a separate unit and not a change to loop-backup.sh / telegram-watchdog:
#   - loop-backup.sh is the daily LAYER FLOOR (fires at fixed hours, keyed on
#     heartbeat staleness) — a stall must be caught in minutes, not hours, and
#     heartbeat freshness is not the right signal (a stalled dept may have a
#     fresh heartbeat from the tick that then died).
#   - telegram-watchdog-<dept> guards the PLUGIN (deaf/wedged/401/session-limit)
#     and lives in bubble-vps-platform; the tick watchdog reads the same
#     transcript but judges the LOOP, and it honours the telegram-watchdog's
#     cooldown mark so the two never both restart a dept in one window.
#
# Usage (on the box, as a sudoer):
#   bash scripts/install-loop-tick-watchdog.sh
#   bash scripts/install-loop-tick-watchdog.sh --dry-run
#   bash scripts/install-loop-tick-watchdog.sh --soak      # install with BUBBLE_TICKWD_DRY_RUN=1 (decide+log only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/deploy/templates"
SYSTEMD_DIR="${BUBBLE_TICKWD_SYSTEMD_DIR:-/etc/systemd/system}"
DRY=0; SOAK=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --soak)    SOAK=1 ;;
        *) echo "usage: $0 [--dry-run] [--soak]" >&2; exit 2 ;;
    esac
done

say() { echo "[install-loop-tick-watchdog] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

[[ -f "$PROJECT_ROOT/scripts/loop-tick-watchdog.py" ]] || { echo "ERR: scripts/loop-tick-watchdog.py missing" >&2; exit 2; }
[[ -f "$PROJECT_ROOT/scripts/lib/loop_tick_watchdog.py" ]] || { echo "ERR: scripts/lib/loop_tick_watchdog.py missing" >&2; exit 2; }
run "chmod +x '$PROJECT_ROOT/scripts/loop-tick-watchdog.py'"

for unit in loop-tick-watchdog.service loop-tick-watchdog.timer; do
    src="$TEMPLATE_DIR/$unit"
    [[ -f "$src" ]] || { echo "ERR: template $src missing" >&2; exit 2; }
    say "installing $unit"
    run "sudo install -m 0644 -o root -g root '$src' '$SYSTEMD_DIR/$unit'"
done

# Soak mode: first deploy decides + logs only (BUBBLE_TICKWD_DRY_RUN=1) so a
# human can grade the verdicts in the journal before letting it act.
DROPIN_DIR="$SYSTEMD_DIR/loop-tick-watchdog.service.d"
if [[ "$SOAK" == "1" ]]; then
    say "SOAK mode: installing drop-in BUBBLE_TICKWD_DRY_RUN=1 (decide+log only)"
    run "sudo mkdir -p '$DROPIN_DIR'"
    run "printf '[Service]\nEnvironment=BUBBLE_TICKWD_DRY_RUN=1\n' | sudo tee '$DROPIN_DIR/soak.conf' >/dev/null"
else
    if [[ -f "$DROPIN_DIR/soak.conf" ]]; then
        say "removing soak drop-in (watchdog will ACT from now on)"
        run "sudo rm -f '$DROPIN_DIR/soak.conf'"
    fi
fi

run "sudo systemctl daemon-reload"
run "sudo systemctl enable --now loop-tick-watchdog.timer"

if [[ "$DRY" != "1" ]]; then
    say "installed. Next run:"
    systemctl list-timers 'loop-tick-watchdog.timer' --no-pager 2>/dev/null | grep -E "loop-tick|NEXT" || true
    say "Manual smoke test (no side effects): $PROJECT_ROOT/scripts/loop-tick-watchdog.py --host vps --dry-run"
    say "Journal: journalctl -u loop-tick-watchdog.service -n 50 --no-pager"
else
    say "DRY RUN complete — nothing installed."
fi
