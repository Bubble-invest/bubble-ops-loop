#!/usr/bin/env bash
# check-vendor-drift.sh — READ-ONLY drift report for the vendored shared libs (#1312).
#
# THE GAP #1312 CLOSES: shared helpers like scripts/lib/codex_write.sh used to be
# hand-copied into every dept repo. A single fix meant N pull requests, and if one
# was forgotten that dept silently kept the old bytes forever with NOTHING reporting
# the divergence (#1301: one ~40-line fix needed SIX PRs). Those helpers now ride the
# vendoring path (vendor-dept-libs.sh MAP), so one canonical edit in the framework
# propagates mechanically. This script is the LOUD half of that: it compares every
# dept's vendored copy against the framework canonical and reports any divergence, so
# a missed/lagging propagation is caught instead of rotting silently.
#
# It writes NOTHING. It only reads bytes and prints a report. Copying/self-healing is
# vendor-dept-libs.sh's job (boot-time) and revendor-all-depts.sh's job (fleet sweep);
# this script is their read-only twin — safe to run from a cron/monitor/CI.
#
# For each managed dept x each vendored file, the dept copy is classified:
#   IN-SYNC  dept copy == framework canonical (the goal).
#   STALE    dept copy != canonical BUT == this dept's last-vendored baseline
#            (.git/vendor-dept-libs/<rel>): canonical advanced, the sweep just
#            has not re-vendored this box yet. Transient — a revendor fixes it.
#   FORK     dept copy != canonical AND != baseline (or no baseline recorded):
#            an unmanaged divergence — a hand-edit, a stale un-propagated fix, or
#            a legitimately-deferred local fork. This is the silent-drift class
#            #1312 exists to surface. Always reported; never auto-touched here.
#
# Usage:
#   check-vendor-drift.sh [--framework <dir>] [--agents-root <dir>] [--quiet]
#
#   --framework    canonical bubble-ops-loop root. Defaults to $BUBBLE_FRAMEWORK_ROOT,
#                  else this script's own repo root (dirname/..). Same resolution
#                  intent as revendor-all-depts.sh so "what is canonical" has one answer.
#   --agents-root  base dir holding bubble-ops-<slug> clones. Default /home/claude/agents
#                  (parameterized for tests). Override for the Mac host:local layout.
#   --quiet        print only the summary line and any drift (suppress per-file IN-SYNC).
#
# host:local depts are SKIPPED for the same reason revendor-all-depts.sh skips them:
# on the VPS they are read-only mirrors whose /loop never runs here, so the mirror can
# legitimately lag and reporting it as drift would be noise. Run this script ON that
# dept's own host (point --agents-root at its parent) to check it there.
#
# Exit codes (deliberately loud, unlike the fail-open copy scripts):
#   0  no FORK divergence found (STALE-only is not fatal — the sweep will heal it).
#   1  at least one FORK divergence found — a real, unmanaged drift to look at.
#   2  usage / unrecoverable setup error (bad flag, missing framework).
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FRAMEWORK="${BUBBLE_FRAMEWORK_ROOT:-}"
AGENTS_ROOT="${BUBBLE_REVENDOR_AGENTS_ROOT:-/home/claude/agents}"
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --framework) FRAMEWORK="${2:?--framework needs a value}"; shift 2 ;;
    --framework=*) FRAMEWORK="${1#--framework=}"; shift ;;
    --agents-root) AGENTS_ROOT="${2:?--agents-root needs a value}"; shift 2 ;;
    --agents-root=*) AGENTS_ROOT="${1#--agents-root=}"; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help)
      sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "ERR: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [[ -z "$FRAMEWORK" ]]; then
  FRAMEWORK="$(cd "$SELF_DIR/.." && pwd)"
fi

TS()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [check-vendor-drift] $*"; }

[[ -d "$FRAMEWORK" ]] || { log "ERR: framework '$FRAMEWORK' missing"; exit 2; }

# The vendored src/dst map — MUST mirror vendor-dept-libs.sh's MAP (kept in sync by
# hand, matching the existing DRY_RUN_MAP convention in revendor-all-depts.sh). Only
# the "always fill existing dir" libs are checked here; the emit-kanban CREATE-dir
# capability files are intentionally out of scope for a divergence report.
MAP=(
  "scripts/lib/dispatch_helpers.py   scripts/lib/dispatch_helpers.py"
  "scripts/lib/notify.py             scripts/lib/notify.py"
  "scripts/lib/loop_notify.py        scripts/lib/loop_notify.py"
  "scripts/lib/notion_logbook.py     scripts/lib/notion_logbook.py"
  "scripts/lib/budget.py             scripts/lib/budget.py"
  "scripts/lib/codex_write.sh        scripts/lib/codex_write.sh"
  "tools/notify_layer.py             tools/notify_layer.py"
)

# dept_host <dir>: identical fail-safe resolution to revendor-all-depts.sh —
# missing/malformed STATE.yaml or any value other than exactly "local" => "vps".
dept_host() {
  local dir="$1"
  local state="${dir}/onboarding/STATE.yaml"
  [[ -f "$state" ]] || { echo "vps"; return 0; }
  local val
  val="$(grep -E '^host:[[:space:]]*' "$state" 2>/dev/null | head -n1 \
          | sed -E 's/^host:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
  [[ "$val" == "local" ]] && echo "local" || echo "vps"
}

log "START framework=${FRAMEWORK} agents_root=${AGENTS_ROOT}"

TOTAL=0; CHECKED=0; SKIPPED=0
INSYNC=0; STALE=0; FORK=0

for dir in "${AGENTS_ROOT}"/bubble-ops-*; do
  [[ -d "$dir" ]] || continue
  slug="$(basename "$dir")"; slug="${slug#bubble-ops-}"
  TOTAL=$((TOTAL + 1))

  if [[ "$(dept_host "$dir")" == "local" ]]; then
    log "skip ${slug}: host:local (read-only mirror here — check on its own host)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  GIT_DIR="$(git -C "$dir" rev-parse --absolute-git-dir 2>/dev/null || true)"
  STATE_DIR="${GIT_DIR:+${GIT_DIR}/vendor-dept-libs}"

  for pair in "${MAP[@]}"; do
    # shellcheck disable=SC2086
    set -- $pair
    src="$FRAMEWORK/$1"; dst="$dir/$2"; rel="$2"
    [[ -f "$src" ]] || continue                 # not a canonical file — nothing to compare
    dst_dir="$(dirname "$dst")"
    [[ -d "$dst_dir" ]] || continue             # dept does not use this surface (mirror MAP rule)
    [[ -e "$dst" ]] || {                        # dir present but file missing — a real gap
      log "  FORK ${slug}: ${rel} MISSING (dept has the dir but not the vendored file)"
      FORK=$((FORK + 1)); continue
    }
    if cmp -s "$src" "$dst" 2>/dev/null; then
      [[ "$QUIET" == 1 ]] || log "  in-sync ${slug}: ${rel}"
      INSYNC=$((INSYNC + 1))
      continue
    fi
    # Divergent. Classify STALE (matches recorded baseline) vs FORK (does not).
    last="${STATE_DIR:+${STATE_DIR}/$rel}"
    if [[ -n "$last" && -f "$last" ]] && cmp -s "$dst" "$last" 2>/dev/null; then
      log "  STALE ${slug}: ${rel} != canonical but == last-vendored baseline (revendor pending)"
      STALE=$((STALE + 1))
    else
      log "  FORK  ${slug}: ${rel} diverges from canonical (unmanaged — hand-edit / un-propagated / deferred fork)"
      FORK=$((FORK + 1))
    fi
  done
  CHECKED=$((CHECKED + 1))
done

log "DONE depts_total=${TOTAL} checked=${CHECKED} skipped=${SKIPPED} in_sync=${INSYNC} stale=${STALE} fork=${FORK}"

# A zero-depts-checked run must NOT masquerade as a verified-clean fleet: a wrong
# --agents-root / wrong host / typo would otherwise print "clean" and exit 0, the
# exact silent-pass this script exists to prevent. Treat it as a setup error (2).
if [[ "$CHECKED" -eq 0 ]]; then
  log "ERR: checked 0 depts under '${AGENTS_ROOT}' (all host:local, or no bubble-ops-* clones here). Nothing verified — NOT reporting clean."
  exit 2
fi

if [[ "$FORK" -gt 0 ]]; then
  log "RESULT: DRIFT — ${FORK} unmanaged divergence(s). Investigate (a forgotten propagation is exactly this)."
  exit 1
fi
if [[ "$STALE" -gt 0 ]]; then
  log "RESULT: ${STALE} stale copy(ies) pending a revendor sweep; no unmanaged drift."
  exit 0
fi
log "RESULT: clean — every vendored copy matches canonical."
exit 0
