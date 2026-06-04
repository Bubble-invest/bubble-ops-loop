#!/usr/bin/env bash
# install-loop-backup.sh — install the ops-loop "4-cron layer FLOOR".
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run
# (e.g. on every deploy / fresh box bring-up).
#
# WHAT IT INSTALLS (the floor):
#   EXACTLY 4 cron units, one per OODA layer, forever:
#     loop-layer1.timer → L1 (Observe)  07:00 Europe/Paris
#     loop-layer2.timer → L2 (Orient)   12:00 Europe/Paris
#     loop-layer3.timer → L3 (Decide)   16:00 Europe/Paris
#     loop-layer4.timer → L4 (Act)      19:00 Europe/Paris
#   All four share ONE template service `loop-layer@.service`
#   (ExecStart=…/loop-backup.sh --layer %i). Each fires its layer for EVERY
#   eligible dept, discovered at RUNTIME (glob /home/claude/agents/bubble-ops-*).
#   A NEW dept being born adds ZERO new units — it's picked up automatically.
#
# WHY a floor: each dept runs a persistent `/loop` session. If that session
# dies for any reason (auth lapse, crash, OOM, "parked" after a restart) the
# dept silently stops working while systemd still reports `active`. These four
# crons GUARANTEE each OODA layer fires >=1x/day per dept even if the live
# /loop is dead. A heartbeat freshness gate skips depts whose live loop is
# healthy, so a working dept is never double-ticked; a flock mutex guarantees a
# floor tick never overlaps a live tick. See scripts/loop-backup.sh +
# scripts/lib/loop_backup.py.
#
# LEGACY: the old twice-daily generic `loop-backup.timer` (decide_dispatch tick,
# no forced layer) is RETIRED by this installer — the 4-layer floor supersedes
# it (it fired ~L1 only; the floor fires all four). We `disable --now` it if
# present. The generic mode of the script (no --layer) is KEPT for manual /
# emergency use; only the redundant TIMER is retired.
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

# The 4 layer timers + the one template service that backs them.
LAYER_TIMERS=(loop-layer1.timer loop-layer2.timer loop-layer3.timer loop-layer4.timer)
TEMPLATE_SERVICE="loop-layer@.service"

say() { echo "[install-loop-backup] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

# 1) The runner script + decision lib are part of the cloned repo already
#    (scripts/loop-backup.sh, scripts/lib/loop_backup.py). Just ensure the
#    runner is executable.
[[ -f "$PROJECT_ROOT/scripts/loop-backup.sh" ]] || { echo "ERR: scripts/loop-backup.sh missing in repo" >&2; exit 2; }
run "chmod +x '$PROJECT_ROOT/scripts/loop-backup.sh'"

# 2) Install the template service + the 4 layer timers from templates.
for unit in "$TEMPLATE_SERVICE" "${LAYER_TIMERS[@]}"; do
    src="$TEMPLATE_DIR/$unit"
    [[ -f "$src" ]] || { echo "ERR: template $src missing" >&2; exit 2; }
    say "installing $unit"
    run "sudo install -m 0644 -o root -g root '$src' '$SYSTEMD_DIR/$unit'"
done

# 3) Reload, then enable+start each layer timer (the template service is
#    oneshot, fired by its timer — never enabled directly).
run "sudo systemctl daemon-reload"
for t in "${LAYER_TIMERS[@]}"; do
    run "sudo systemctl enable --now '$t'"
done

# 4) Retire the legacy generic timer if it's present (superseded by the floor).
#    Idempotent: disable --now is a no-op if it's already gone/stopped.
if systemctl cat loop-backup.timer >/dev/null 2>&1; then
    say "retiring legacy generic loop-backup.timer (superseded by the 4-layer floor)"
    run "sudo systemctl disable --now loop-backup.timer || true"
else
    say "legacy loop-backup.timer not present — nothing to retire"
fi

# 5) Show next fire times.
if [[ "$DRY" != "1" ]]; then
    say "installed. Next runs:"
    systemctl list-timers 'loop-layer*.timer' --no-pager 2>/dev/null | grep -E "loop-layer|NEXT" || true
    say "Manual smoke test (no side effects): BUBBLE_BACKUP_DRY_RUN=1 $PROJECT_ROOT/scripts/loop-backup.sh --layer 1"
else
    say "DRY RUN complete — nothing installed."
fi
