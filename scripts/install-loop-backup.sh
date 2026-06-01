#!/usr/bin/env bash
# install-loop-backup.sh — install the ops-loop dept BACKUP execution timer.
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run
# (e.g. on every deploy / fresh box bring-up). Installs the systemd
# service+timer from deploy/templates/ and enables the twice-daily timer.
#
# What the backup does: twice a day (08:00 + 14:00 Europe/Paris) it checks
# each dept's heartbeat freshness; if a dept's persistent /loop is
# dead/parked (stale heartbeat) it runs ONE dispatch tick via `claude -p`.
# A safety net so a dead loop doesn't silently halt a dept. See
# scripts/loop-backup.sh + scripts/lib/loop_backup.py.
#
# Usage (on the box, as a sudoer):
#   bash scripts/install-loop-backup.sh
#   bash scripts/install-loop-backup.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/deploy/templates"
SYSTEMD_DIR="/etc/systemd/system"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { echo "[install-loop-backup] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

# 1) The runner script + decision lib are part of the cloned repo already
#    (scripts/loop-backup.sh, scripts/lib/loop_backup.py). Just ensure the
#    runner is executable.
[[ -f "$PROJECT_ROOT/scripts/loop-backup.sh" ]] || { echo "ERR: scripts/loop-backup.sh missing in repo" >&2; exit 2; }
run "chmod +x '$PROJECT_ROOT/scripts/loop-backup.sh'"

# 2) Install the systemd unit + timer from templates.
for unit in loop-backup.service loop-backup.timer; do
    src="$TEMPLATE_DIR/$unit"
    [[ -f "$src" ]] || { echo "ERR: template $src missing" >&2; exit 2; }
    say "installing $unit"
    run "sudo install -m 0644 -o root -g root '$src' '$SYSTEMD_DIR/$unit'"
done

# 3) Reload + enable + start the timer (service is oneshot, fired by timer).
run "sudo systemctl daemon-reload"
run "sudo systemctl enable --now loop-backup.timer"

# 4) Show next fire times.
if [[ "$DRY" != "1" ]]; then
    say "installed. Next runs:"
    systemctl list-timers loop-backup.timer --no-pager 2>/dev/null | grep -E "loop-backup|NEXT" || true
    say "Manual test (no side effects): BUBBLE_BACKUP_DRY_RUN=1 $PROJECT_ROOT/scripts/loop-backup.sh"
else
    say "DRY RUN complete — nothing installed."
fi
