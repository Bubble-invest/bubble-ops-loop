#!/usr/bin/env bash
# revendor-all-depts.sh — proactive fleet-wide re-vendor sweep (#419).
#
# THE GAP: vendor-dept-libs.sh self-heals a dept's vendored framework libs, but
# only runs at that dept's SERVICE START (boot). When a fix merges into the
# canonical bubble-ops-loop framework, a dept that doesn't restart keeps running
# the stale copy until its next boot — hours or days later. #403's emit fix sat
# un-propagated across 5 depts for 2+ hours until Rick manually re-ran
# vendor-dept-libs.sh for each one by hand.
#
# THIS SCRIPT: the operator-run (or hook-invoked) sweep that replaces the manual
# per-dept loop. Given a framework root and an agents root, it discovers every
# LOCAL dept clone the VPS can re-vendor into and runs vendor-dept-libs.sh for
# each. It does NOT restart any agent — re-vendoring only refreshes the on-disk
# files; the running process picks up the change on its own next boot (import-
# time / restart), matching vendor-dept-libs.sh's existing contract. Wiring an
# auto-restart is a deliberate non-goal here (bigger blast radius, separate
# decision) — see #419 discussion.
#
# host:local depts (e.g. Miranda's bubble-ops-content on a Mac) are SKIPPED: the
# VPS only holds a READ-ONLY MIRROR of those (see sync-local-dept-clones.sh) and
# their /loop never executes on the VPS, so re-vendoring the mirror would be a
# no-op at best and could stage a spurious local diff on a repo the VPS doesn't
# own. Only host:vps (default) depts are re-vendored.
#
# Usage:
#   revendor-all-depts.sh [--framework <dir>] [--agents-root <dir>] [--dry-run]
#
#   --framework    canonical bubble-ops-loop root. Defaults to
#                  $BUBBLE_FRAMEWORK_ROOT, else the resolved location of this
#                  script's own repo (dirname/..). Passed through to
#                  vendor-dept-libs.sh via BUBBLE_FRAMEWORK_ROOT so its own
#                  host-aware resolution logic (env > sibling > VPS default)
#                  stays the single source of truth for "what is canonical".
#   --agents-root  base dir holding bubble-ops-<slug> clones.
#                  Default /home/claude/agents (parameterized for tests).
#   --dry-run      report which dept/file pairs WOULD be refreshed, without
#                  copying anything. Compares framework vs dept bytes directly
#                  (does not shell out to vendor-dept-libs.sh, which has no
#                  read-only mode).
#
# Idempotent, fail-OPEN per dept: a bad/missing dept dir is SKIPPED (logged),
# never aborts the sweep — one broken dept must not block the other five from
# getting the fix.
#
# Exit codes: always 0 (sweep summary + fail-open dept errors are logged, not
# fatal — this mirrors vendor-dept-libs.sh's own fail-open contract so the
# sweep is safe to run from a cron/hook without flapping it).
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FRAMEWORK="${BUBBLE_FRAMEWORK_ROOT:-}"
AGENTS_ROOT="${BUBBLE_REVENDOR_AGENTS_ROOT:-/home/claude/agents}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --framework) FRAMEWORK="${2:?--framework needs a value}"; shift 2 ;;
    --framework=*) FRAMEWORK="${1#--framework=}"; shift ;;
    --agents-root) AGENTS_ROOT="${2:?--agents-root needs a value}"; shift 2 ;;
    --agents-root=*) AGENTS_ROOT="${1#--agents-root=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "ERR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

# Default framework to this script's own repo root (sibling of scripts/) when
# nothing else was given — the common case of running the sweep from a
# checked-out framework working tree.
if [[ -z "$FRAMEWORK" ]]; then
  FRAMEWORK="$(cd "$SELF_DIR/.." && pwd)"
fi

VENDOR_SCRIPT="$SELF_DIR/vendor-dept-libs.sh"

TS()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [revendor-all-depts] $*"; }

[[ -x "$VENDOR_SCRIPT" || -f "$VENDOR_SCRIPT" ]] || {
  log "WARN: vendor-dept-libs.sh not found at $VENDOR_SCRIPT — nothing to run"
  exit 0
}
[[ -d "$FRAMEWORK" ]] || {
  log "WARN: framework '$FRAMEWORK' missing — skip (fail-open)"
  exit 0
}

# dept_host <dir>: mirrors sync-local-dept-clones.sh's fail-safe resolution —
# missing/unreadable/malformed STATE.yaml, or any value other than exactly
# "local", resolves to "vps" (re-vendor it; never silently skip a real VPS dept).
dept_host() {
  local dir="$1"
  local state="${dir}/onboarding/STATE.yaml"
  [[ -f "$state" ]] || { echo "vps"; return 0; }
  local val
  val="$(grep -E '^host:[[:space:]]*' "$state" 2>/dev/null | head -n1 \
          | sed -E 's/^host:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
  [[ "$val" == "local" ]] && echo "local" || echo "vps"
}

# The same src/dst map vendor-dept-libs.sh vendors, kept in sync by hand (it's
# short and rarely changes). Used ONLY for --dry-run's byte-diff report; the
# real (non-dry-run) sweep delegates all copying to vendor-dept-libs.sh so the
# copy/skip-worktree/fail-open logic lives in exactly one place.
DRY_RUN_MAP=(
  "scripts/lib/dispatch_helpers.py   scripts/lib/dispatch_helpers.py"
  "scripts/lib/notify.py             scripts/lib/notify.py"
  "scripts/lib/loop_notify.py        scripts/lib/loop_notify.py"
  "scripts/lib/notion_logbook.py     scripts/lib/notion_logbook.py"
  "scripts/lib/budget.py             scripts/lib/budget.py"
  "tools/notify_layer.py             tools/notify_layer.py"
  "skills/emit-kanban-task/SKILL.md          skills/emit-kanban-task/SKILL.md"
  "skills/emit-kanban-task/scripts/emit.sh   skills/emit-kanban-task/scripts/emit.sh"
  "tools/kanban/emit_kanban_item.sh          tools/kanban/emit_kanban_item.sh"
)

log "START framework=${FRAMEWORK} agents_root=${AGENTS_ROOT} dry_run=${DRY_RUN}"

TOTAL=0
SWEPT=0
SKIPPED=0
STALE_TOTAL=0

for dir in "${AGENTS_ROOT}"/bubble-ops-*; do
  [[ -d "$dir" ]] || continue   # no match → glob stays literal; -d guards it
  slug="$(basename "$dir")"; slug="${slug#bubble-ops-}"
  TOTAL=$((TOTAL + 1))

  if [[ "$(dept_host "$dir")" == "local" ]]; then
    log "skip ${slug}: host:local (VPS holds a read-only mirror only, never re-vendored)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    stale=0
    for pair in "${DRY_RUN_MAP[@]}"; do
      # shellcheck disable=SC2086
      set -- $pair
      src="$FRAMEWORK/$1"; dst="$dir/$2"
      [[ -f "$src" ]] || continue
      dst_dir="$(dirname "$dst")"
      # mirror vendor-dept-libs.sh's own creation rule: the two kanban-capability
      # files get their dest dir created if missing; the rest only refresh an
      # EXISTING dest dir. Approximate that here by checking existence OR (for
      # the kanban paths) always counting a missing file as stale.
      if [[ "$dst" == *"skills/emit-kanban-task"* || "$dst" == *"tools/kanban/emit_kanban_item.sh"* ]]; then
        if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst" 2>/dev/null; then
          log "  [dry-run] ${slug}: would re-vendor $2"
          stale=$((stale + 1))
        fi
      else
        [[ -d "$dst_dir" ]] || continue
        if ! cmp -s "$src" "$dst" 2>/dev/null; then
          log "  [dry-run] ${slug}: would re-vendor $2"
          stale=$((stale + 1))
        fi
      fi
    done
    if [[ "$stale" -eq 0 ]]; then
      log "${slug}: already up to date (dry-run)"
    else
      log "${slug}: ${stale} file(s) would be refreshed (dry-run)"
    fi
    STALE_TOTAL=$((STALE_TOTAL + stale))
    SWEPT=$((SWEPT + 1))
    continue
  fi

  # Real sweep: delegate to vendor-dept-libs.sh — same script the boot-time
  # self-heal uses, so behaviour (copy set, skip-worktree, fail-open, chown)
  # is identical whether triggered by a restart or by this proactive sweep.
  out="$(BUBBLE_FRAMEWORK_ROOT="$FRAMEWORK" "$VENDOR_SCRIPT" "$dir" 2>&1)"
  rc=$?
  # vendor-dept-libs.sh is itself fail-open (always exits 0), but guard anyway:
  # one dept's unexpected failure must never abort the sweep.
  if [[ "$rc" -ne 0 ]]; then
    log "WARN ${slug}: vendor-dept-libs.sh exited ${rc} — skipping (sweep continues)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  while IFS= read -r line; do echo "  [${slug}] ${line}"; done <<< "$out"
  SWEPT=$((SWEPT + 1))
done

if [[ "$DRY_RUN" == "1" ]]; then
  log "DONE (dry-run) depts_total=${TOTAL} checked=${SWEPT} skipped=${SKIPPED} would_refresh=${STALE_TOTAL}"
else
  log "DONE depts_total=${TOTAL} swept=${SWEPT} skipped=${SKIPPED}"
fi
exit 0
