#!/usr/bin/env bash
# Install the per-department four-layer VPS floor (#606 isolation repair).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/deploy/templates"
SYSTEMD_DIR="${BUBBLE_FLOOR_SYSTEMD_DIR:-/etc/systemd/system}"
AGENTS_ROOT="${BUBBLE_FLOOR_AGENTS_ROOT:-/srv/agents}"
SYSTEMCTL="${BUBBLE_FLOOR_SYSTEMCTL:-systemctl}"
DRY=0
ACTIVATE=0
ONLY_DEPT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --install-only) ACTIVATE=0; shift ;; # compatibility alias; now the safe default
        --activate) ACTIVATE=1; shift ;;
        --dept) ONLY_DEPT="${2:-}"; shift 2 ;;
        --dept=*) ONLY_DEPT="${1#--dept=}"; shift ;;
        *) echo "ERR: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

# A scoped activation cannot retire the four global timers safely: those
# timers still provide floor coverage for every department omitted from the
# selection. Keep --dept/BUBBLE_FLOOR_DEPTS useful for install-only and dry
# inspection, but require a fleet-wide discovery for the actual cutover.
if [[ "$ACTIVATE" == 1 && ( -n "$ONLY_DEPT" || -n "${BUBBLE_FLOOR_DEPTS:-}" ) ]]; then
    echo "ERR: --activate must cover every enabled department; scoped activation would drop floor coverage" >&2
    exit 64
fi

say() { echo "[install-loop-backup] $*"; }
root_run() {
    if [[ "$DRY" == "1" ]]; then
        printf '  DRY:'; printf ' %q' "$@"; printf '\n'
    else
        sudo -- "$@"
    fi
}
valid_slug() { [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]]; }

[[ -f "$PROJECT_ROOT/scripts/loop-backup.sh" ]] || { echo "ERR: runner missing" >&2; exit 2; }
for layer in 1 2 3 4; do
    for kind in service timer; do
        f="$TEMPLATE_DIR/loop-layer${layer}@.${kind}"
        [[ -f "$f" ]] || { echo "ERR: template missing: $f" >&2; exit 2; }
    done
done

VERIFY_UNITS=()
for layer in 1 2 3 4; do
    VERIFY_UNITS+=("$TEMPLATE_DIR/loop-layer${layer}@.service")
    VERIFY_UNITS+=("$TEMPLATE_DIR/loop-layer${layer}@.timer")
done
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "${VERIFY_UNITS[@]}"
elif [[ "$DRY" == "1" ]]; then
    say "systemd-analyze unavailable; static dry-run only"
else
    echo "ERR: systemd-analyze required before installing units" >&2
    exit 2
fi

DEPTS=()
if [[ -n "$ONLY_DEPT" ]]; then
    valid_slug "$ONLY_DEPT" || { echo "ERR: invalid dept slug" >&2; exit 2; }
    DEPTS+=("$ONLY_DEPT")
elif [[ -n "${BUBBLE_FLOOR_DEPTS:-}" ]]; then
    for slug in $BUBBLE_FLOOR_DEPTS; do
        valid_slug "$slug" || { echo "ERR: invalid dept slug" >&2; exit 2; }
        DEPTS+=("$slug")
    done
else
    while read -r unit _rest; do
        [[ "$unit" =~ ^bubble-agent@([a-z][a-z0-9-]{0,31})\.service$ ]] || continue
        slug="${BASH_REMATCH[1]}"
        "$SYSTEMCTL" is-enabled "$unit" >/dev/null 2>&1 || continue
        DEPTS+=("$slug")
    done < <("$SYSTEMCTL" list-units 'bubble-agent@*.service' --all --plain --no-legend 2>/dev/null || true)
fi
[[ "${#DEPTS[@]}" -gt 0 ]] || { echo "ERR: no enabled departments discovered" >&2; exit 2; }

ELIGIBLE=()
for slug in "${DEPTS[@]}"; do
    wd="$AGENTS_ROOT/$slug"
    [[ -f "$wd/dept.yaml" ]] || { say "skip $slug: no dept.yaml (not a department floor)"; continue; }
    if [[ "${BUBBLE_FLOOR_TEST_UIDS:-0}" != "1" ]]; then
        id "agent-$slug" >/dev/null 2>&1 || { echo "ERR: OS user agent-$slug missing" >&2; exit 2; }
    fi
    has_layer=0
    for layer in 1 2 3 4; do [[ -f "$wd/layers/$layer/PROMPT.md" ]] && has_layer=1; done
    [[ "$has_layer" == 1 ]] || { say "skip $slug: no layer prompts"; continue; }
    ELIGIBLE+=("$slug")
done
[[ "${#ELIGIBLE[@]}" -gt 0 ]] || { echo "ERR: no eligible departments" >&2; exit 2; }

for layer in 1 2 3 4; do
    for kind in service timer; do
        unit="loop-layer${layer}@.${kind}"
        root_run install -m 0644 -o root -g root "$TEMPLATE_DIR/$unit" "$SYSTEMD_DIR/$unit"
    done
done
root_run "$SYSTEMCTL" daemon-reload

if [[ "$ACTIVATE" == 0 ]]; then
    say "unit templates installed only; timers unchanged"
    exit 0
fi

NEW_TIMERS=()
for slug in "${ELIGIBLE[@]}"; do
    for layer in 1 2 3 4; do
        [[ -f "$AGENTS_ROOT/$slug/layers/$layer/PROMPT.md" ]] || continue
        NEW_TIMERS+=("loop-layer${layer}@${slug}.timer")
    done
done
[[ "${#NEW_TIMERS[@]}" -gt 0 ]] || { echo "ERR: no timer instances selected" >&2; exit 2; }

# Explicit cutover only. Stop the global timers before starting persistent
# replacements so a catch-up activation cannot overlap the legacy floor.
OLD_TIMERS=(loop-layer1.timer loop-layer2.timer loop-layer3.timer loop-layer4.timer)
if [[ "$DRY" == "1" ]]; then
    for timer in "${OLD_TIMERS[@]}"; do root_run "$SYSTEMCTL" disable --now "$timer"; done
    for timer in "${NEW_TIMERS[@]}"; do root_run "$SYSTEMCTL" enable --now "$timer"; done
else
    # Snapshot every global AND replacement timer before touching any of them.
    # Both `disable --now` and `enable --now` may mutate their target before
    # returning nonzero. A rerun may also begin with some already-active
    # replacements. Rollback therefore restores every timer's exact prior
    # enablement kind and active state, including the failing command's target.
    OLD_ENABLED=()
    OLD_ACTIVE=()
    for timer in "${OLD_TIMERS[@]}"; do
        OLD_ENABLED+=("$("$SYSTEMCTL" is-enabled "$timer" 2>/dev/null || true)")
        OLD_ACTIVE+=("$("$SYSTEMCTL" is-active "$timer" 2>/dev/null || true)")
    done
    NEW_ENABLED=()
    NEW_ACTIVE=()
    for timer in "${NEW_TIMERS[@]}"; do
        NEW_ENABLED+=("$("$SYSTEMCTL" is-enabled "$timer" 2>/dev/null || true)")
        NEW_ACTIVE+=("$("$SYSTEMCTL" is-active "$timer" 2>/dev/null || true)")
    done

    restore_one_timer() {
        local timer="$1" enabled="$2" active="$3"
        case "$enabled" in
            enabled) sudo -- "$SYSTEMCTL" enable "$timer" || true ;;
            enabled-runtime)
                # Remove any persistent link created by the failed attempt
                # before restoring the original runtime-only enablement.
                sudo -- "$SYSTEMCTL" disable "$timer" || true
                sudo -- "$SYSTEMCTL" enable --runtime "$timer" || true
                ;;
            masked) sudo -- "$SYSTEMCTL" mask "$timer" || true ;;
            masked-runtime)
                sudo -- "$SYSTEMCTL" unmask "$timer" || true
                sudo -- "$SYSTEMCTL" mask --runtime "$timer" || true
                ;;
            disabled|not-found|*) sudo -- "$SYSTEMCTL" disable "$timer" || true ;;
        esac
        case "$active" in
            active|activating) sudo -- "$SYSTEMCTL" start "$timer" || true ;;
            inactive|failed|deactivating) sudo -- "$SYSTEMCTL" stop "$timer" || true ;;
        esac
    }

    restore_all_timers() {
        local i timer
        # Stop/disable changed replacement timers first so restoring the global
        # floor does not create new duplicate coverage. Pre-existing active
        # replacements are restored to their prior state below.
        for i in "${!NEW_TIMERS[@]}"; do
            timer="${NEW_TIMERS[$i]}"
            restore_one_timer "$timer" "${NEW_ENABLED[$i]}" "${NEW_ACTIVE[$i]}"
        done
        for i in "${!OLD_TIMERS[@]}"; do
            timer="${OLD_TIMERS[$i]}"
            restore_one_timer "$timer" "${OLD_ENABLED[$i]}" "${OLD_ACTIVE[$i]}"
        done
    }

    rollback_cutover() {
        restore_all_timers
    }

    for timer in "${OLD_TIMERS[@]}"; do
        if ! sudo -- "$SYSTEMCTL" disable --now "$timer"; then
            echo "ERR: global timer retirement failed; restoring exact prior timer states" >&2
            rollback_cutover
            exit 1
        fi
    done

    for timer in "${NEW_TIMERS[@]}"; do
        if sudo -- "$SYSTEMCTL" enable --now "$timer"; then
            :
        else
            echo "ERR: replacement activation failed; restoring exact prior timer states" >&2
            rollback_cutover
            exit 1
        fi
    done
fi
say "installed ${#NEW_TIMERS[@]} per-department layer timers; retired four global timers"
