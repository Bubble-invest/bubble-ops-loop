#!/usr/bin/env bash
# install-loop-backup.sh — install the ops-loop "4-cron layer FLOOR".
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run
# (e.g. on every deploy / fresh box bring-up).
#
# WHAT IT INSTALLS (the floor):
#   5 cron units — 4 one-per-OODA-layer, PLUS a late L4 extension:
#     loop-layer1.timer      → L1 (Observe)      07:00 Europe/Paris
#     loop-layer2.timer      → L2 (Orient)       12:00 Europe/Paris
#     loop-layer3.timer      → L3 (Decide)       16:00 Europe/Paris
#     loop-layer4.timer      → L4 (Act)          19:00 Europe/Paris
#     loop-layer4-late.timer → L4 (Act) LATE ext. 23:00 Europe/Paris
#   (board #508, 2026-07-03: the original 4-timer ladder topped out at 21:00
#   Paris — before same-layer missions with a later `time:`, e.g. a daily
#   22:30 mission, ever got a second floor chance. loop-layer4-late.timer is
#   a pure floor EXTENSION, not a new mechanism: same template service, same
#   staleness/prereq gates in loop-backup.sh.)
#   All five share ONE template service `loop-layer@.service`
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

# The 5 layer timers (4 base + 1 late-L4 extension, #508) + the one template
# service that backs them all.
LAYER_TIMERS=(loop-layer1.timer loop-layer2.timer loop-layer3.timer loop-layer4.timer loop-layer4-late.timer)
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

# 2b) Scoped sudoers grant so the layer service (User=claude) can clear its OWN
#     stale `failed` marker via the ExecStartPre `sudo -n systemctl reset-failed`
#     (unit hygiene — a non-zero tick must not leave the templated instance red
#     forever once the next tick succeeds). Tightly scoped: ONLY reset-failed,
#     ONLY the loop-layer@*.service instances. Validate with visudo before
#     installing so a malformed drop-in can never lock sudo.
SUDOERS_FILE="/etc/sudoers.d/bubble-loop-layer-resetfailed"
SUDOERS_LINE='claude ALL=(root) NOPASSWD: /bin/systemctl reset-failed loop-layer@*.service'
say "installing scoped sudoers grant ($SUDOERS_FILE)"
if [[ "$DRY" == "1" ]]; then
    echo "  DRY: install $SUDOERS_FILE = '$SUDOERS_LINE' (visudo-validated, mode 0440)"
else
    _tmp_sudoers="$(mktemp)"
    printf '# Installed by bubble-ops-loop/scripts/install-loop-backup.sh (Rick 2026-06-19).\n# Lets the loop-layer@N.service ExecStartPre clear its OWN stale failed state.\n%s\n' "$SUDOERS_LINE" > "$_tmp_sudoers"
    if sudo visudo -cf "$_tmp_sudoers" >/dev/null 2>&1; then
        sudo install -m 0440 -o root -g root "$_tmp_sudoers" "$SUDOERS_FILE"
        say "sudoers grant installed + visudo-validated"
    else
        echo "ERR: sudoers drop-in failed visudo validation — NOT installing" >&2
        rm -f "$_tmp_sudoers"
        exit 2
    fi
    rm -f "$_tmp_sudoers"
fi

# 2c) One-shot cleanup: clear any EXISTING stale failed-state on the four
#     instances right now (e.g. Ben's loop-layer@4.service left failed by the
#     2026-06-18 wedge), so the fix takes effect without waiting for the next
#     tick. Never fatal — a clean box has nothing to reset.
for n in 1 2 3 4; do
    run "sudo systemctl reset-failed 'loop-layer@${n}.service' 2>/dev/null || true"
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
