#!/usr/bin/env bash
# vendor-dept-libs.sh — boot-time re-vendor of the canonical shared libs into a
# dept tree, so they can NEVER drift stale ({{OPERATOR}} msg 4025, 2026-06-07).
#
# WHY: dispatch_helpers.py / notify.py / loop_notify.py / notion_logbook.py +
# tools/notify_layer.py are SHARED libs owned by the framework
# (/home/claude/bubble-ops-loop). They're vendored into each dept's scripts/lib
# + tools at onboarding, but they live ON-DISK and are NOT committed to the dept
# repo (non-structural-but-not-runtime-pushable). So any `git checkout`/reset/
# clean-reclone reverts them to the dept's ported baseline — which is exactly how
# safe_pull + the min-time dispatch model silently disappeared from tony/maya/ben
# (2026-06-07). Re-vendoring at EVERY service start makes the framework the single
# source of truth: drift self-heals on the next restart, no per-dept commit needed.
#
# Usage:  vendor-dept-libs.sh <dept-workdir>
#   e.g.  vendor-dept-libs.sh /home/claude/agents/bubble-ops-ben
#
# Idempotent, fail-OPEN (a copy problem must NEVER block the loop from starting):
# any error logs a warning and exits 0. Only copies when the framework file
# differs (cheap) and preserves the dept's own files for anything not in the set.
set -uo pipefail

DEPT="${1:-}"

# Resolve FRAMEWORK with host-aware fallback (fix #234: VPS-only default broke
# Mac host:local agents like Miranda on Jade's machine).
# Resolution order:
#   1. $BUBBLE_FRAMEWORK_ROOT if set (explicit override, highest priority).
#   2. /opt/bubble-ops-loop — board #1115: a ROOT-OWNED checkout (managed by
#      bubble-vps-platform's tasks/access/framework_checkout.py, cloned
#      directly from GitHub via a dedicated read-only deploy key,
#      independent of the claude-writable checkout below). This script is
#      invoked from ExecStartPre=+ (i.e. it runs AS ROOT) — reading the
#      framework source from a directory `claude` cannot write to closes
#      the "root executes claude-controlled code" gap for THIS script.
#      Checked ahead of the legacy candidates below so a box that HAS
#      completed the #1115 cutover automatically prefers it, with zero
#      further changes needed once /opt/bubble-ops-loop exists.
#   3. Sibling of the dept dir: $(dirname <dept-dir>)/bubble-ops-loop
#      — the Mac host:local layout where the dept workspace sits next to
#      bubble-ops-loop in the same parent directory.
#   4. /home/claude/bubble-ops-loop — the canonical VPS path (original
#      default; claude-owned — see board #1115, kept as the fallback for
#      boxes that haven't staged the #1115 cutover yet).
# The first candidate that actually exists on disk wins.
# If none resolve, FRAMEWORK stays empty and the existing fail-open guard below
# catches it (logs WARN and exits 0).
if [[ -n "${BUBBLE_FRAMEWORK_ROOT:-}" ]]; then
  FRAMEWORK="$BUBBLE_FRAMEWORK_ROOT"
else
  FRAMEWORK=""
  # candidate 2: root-owned checkout (board #1115)
  _root_owned="/opt/bubble-ops-loop"
  [[ -d "$_root_owned" ]] && FRAMEWORK="$_root_owned"
  # candidate 3: sibling of dept dir (Mac host:local layout)
  if [[ -z "$FRAMEWORK" && -n "$DEPT" ]]; then
    _sibling="$(dirname "$DEPT")/bubble-ops-loop"
    [[ -d "$_sibling" ]] && FRAMEWORK="$_sibling"
  fi
  # candidate 4: VPS path
  if [[ -z "$FRAMEWORK" ]]; then
    _vps="/home/claude/bubble-ops-loop"
    [[ -d "$_vps" ]] && FRAMEWORK="$_vps"
  fi
fi

log() { logger -t vendor-dept-libs "$*" 2>/dev/null; echo "[vendor-dept-libs] $*" >&2; }

[[ -n "$DEPT" && -d "$DEPT" ]] || { log "WARN: dept dir '$DEPT' missing — skip (fail-open)"; exit 0; }
[[ -d "$FRAMEWORK" ]] || { log "WARN: framework '$FRAMEWORK' missing — skip (fail-open)"; exit 0; }

# Keep the exact bytes last installed by this script under .git, away from the
# dept worktree and its `git add`/clean flows.  That gives the next run a
# three-way comparison: canonical source vs current destination vs the prior
# vendored baseline.  A destination that matches neither is a hand-patch (or
# otherwise unexpected local edit) and must be made recoverable before refresh.
GIT_DIR="$(git -C "$DEPT" rev-parse --absolute-git-dir 2>/dev/null || true)"
VENDOR_STATE_DIR="${GIT_DIR:+${GIT_DIR}/vendor-dept-libs}"

record_last_vendored() {
  local dst="$1" rel="$2" last
  [[ -n "$VENDOR_STATE_DIR" ]] || {
    log "WARN: cannot record last-vendored copy for $rel — dept is not a git worktree"
    return 0
  }
  last="$VENDOR_STATE_DIR/$rel"
  mkdir -p "$(dirname "$last")" 2>/dev/null || {
    log "WARN: cannot create last-vendored state dir for $rel"
    return 0
  }
  cp -f "$dst" "$last" 2>/dev/null || \
    log "WARN: cannot record last-vendored copy for $rel"
}

protect_hand_patch() {
  local src="$1" dst="$2" rel="$3" last backup ts
  [[ -e "$dst" || -L "$dst" ]] || return 0
  cmp -s "$dst" "$src" 2>/dev/null && return 0
  last="${VENDOR_STATE_DIR:+${VENDOR_STATE_DIR}/$rel}"
  # Safe stale copy: it still equals exactly what our prior successful run
  # installed.  No backup/no warning is needed for a routine source upgrade.
  if [[ -n "$last" && -f "$last" ]] && cmp -s "$dst" "$last" 2>/dev/null; then
    return 0
  fi
  # The caller already established dst != src.  If it also differs from the
  # last-vendored bytes (or no baseline exists yet), preserve it before cp -f.
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="${dst}.pre-vendor-${ts}"
  if [[ -e "$backup" ]]; then
    backup="${backup}-$$"
  fi
  if cp -p "$dst" "$backup" 2>/dev/null; then
    chown claude:claude "$backup" 2>/dev/null || true
    log "WARN: $rel differs from canonical and last-vendored copy — backed up hand-patch to $backup"
    return 0
  fi
  log "WARN: $rel differs from canonical and last-vendored copy, but backup to $backup FAILED — preserving destination (not overwritten)"
  return 1
}

# Canonical shared libs: "src-relative-to-framework  dest-relative-to-dept".
# Only files the dept actually uses; a dept missing the dest dir is skipped.
MAP=(
  "scripts/lib/dispatch_helpers.py   scripts/lib/dispatch_helpers.py"
  "scripts/lib/notify.py             scripts/lib/notify.py"
  "scripts/lib/loop_notify.py        scripts/lib/loop_notify.py"
  "scripts/lib/notion_logbook.py     scripts/lib/notion_logbook.py"
  "scripts/lib/budget.py             scripts/lib/budget.py"
  "tools/notify_layer.py             tools/notify_layer.py"
)

vendored=0
for pair in "${MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  src="$FRAMEWORK/$1"; dst="$DEPT/$2"
  [[ -f "$src" ]] || { log "skip $1 — not in framework"; continue; }
  # only copy if the dest dir exists (don't create new surfaces a dept doesn't use)
  dst_dir="$(dirname "$dst")"
  [[ -d "$dst_dir" ]] || { log "skip $2 — dept has no $dst_dir/"; continue; }
  # board #1115: refuse a symlink DEST outright rather than writing through
  # it. Plain `cp` (without --remove-destination) opens+truncates whatever
  # an existing dest symlink points to — this script runs as ROOT
  # (ExecStartPre=+), so a dept dir with a dst path replaced by a symlink
  # (e.g. by a compromised claude session with write access to the dept
  # tree) could otherwise redirect a root-run write to an arbitrary
  # root-writable path on the NEXT service restart.
  if [[ -L "$dst" ]]; then
    log "WARN: refusing $2 — dest is a symlink, not a regular file (fail-open, not copied)"
    continue
  fi
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    protect_hand_patch "$src" "$dst" "$2" || continue
    # -T: dst is always a normal file target (never "copy into directory").
    # --no-dereference: never follow a symlink SRC either (defense in depth).
    if cp -T --no-dereference "$src" "$dst" 2>/dev/null; then
      chown claude:claude "$dst" 2>/dev/null || true
      record_last_vendored "$dst" "$2"
      log "re-vendored $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
  else
    # Bootstrap/repair the baseline even when no refresh was necessary.
    record_last_vendored "$dst" "$2"
  fi
done

# Fleet-wide kanban-emit capability — a DELIBERATE new shared surface for EVERY
# dept (unlike MAP above, which only fills existing dirs). Every agent must be
# able to file a board card; Ben hit this gap 2026-06-21 (no emit-kanban skill →
# fell back to an unwired local DB). So here we CREATE the dest dirs. The skill
# makes the capability discoverable; the tool is the executable; emit.sh is the
# portable wrapper the skill calls.
KANBAN_MAP=(
  "skills/emit-kanban-task/SKILL.md          skills/emit-kanban-task/SKILL.md"
  "skills/emit-kanban-task/scripts/emit.sh   skills/emit-kanban-task/scripts/emit.sh"
  "tools/kanban/emit_kanban_item.sh          tools/kanban/emit_kanban_item.sh"
)
for pair in "${KANBAN_MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  src="$FRAMEWORK/$1"; dst="$DEPT/$2"
  [[ -f "$src" ]] || { log "skip $1 — not in framework"; continue; }
  mkdir -p "$(dirname "$dst")" 2>/dev/null || true
  # board #1115: same symlink-dest refusal as the MAP loop above.
  if [[ -L "$dst" ]]; then
    log "WARN: refusing kanban $2 — dest is a symlink, not a regular file (fail-open, not copied)"
    continue
  fi
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    protect_hand_patch "$src" "$dst" "$2" || continue
    if cp -T --no-dereference "$src" "$dst" 2>/dev/null; then
      chmod +x "$dst" 2>/dev/null || true   # the .sh files must stay executable
      chown claude:claude "$dst" 2>/dev/null || true
      record_last_vendored "$dst" "$2"
      log "re-vendored kanban $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
  else
    record_last_vendored "$dst" "$2"
  fi
done

# skip-worktree the vendored TRACKED files so the loop's git add never picks up
# the framework-overwrite (else it commits structural libs → push 403; Tony
# 2026-06-07). Best-effort, fail-open. Covers BOTH the core libs and the
# kanban-capability files.
for pair in "${MAP[@]}" "${KANBAN_MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  dst="$DEPT/$2"
  [[ -f "$dst" ]] || continue
  if git -C "$DEPT" ls-files --error-unmatch "$2" >/dev/null 2>&1; then
    # TRACKED → tell git to ignore the framework-overwrite in the worktree.
    git -C "$DEPT" update-index --skip-worktree "$2" 2>/dev/null \
      && log "skip-worktree set on $2" || true
  else
    # UNTRACKED → add to .git/info/exclude (local, uncommitted) so `git add`
    # never stages the vendored file into a runtime commit.
    excl="$DEPT/.git/info/exclude"
    if [[ -f "$excl" ]] && ! grep -qxF "$2" "$excl" 2>/dev/null; then
      printf '%s\n' "$2" >> "$excl" && log "git-excluded untracked vendored $2" || true
    fi
  fi
done

log "done — $vendored file(s) refreshed for $(basename "$DEPT")"
exit 0
