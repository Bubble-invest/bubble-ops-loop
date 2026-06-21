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

FRAMEWORK="${BUBBLE_FRAMEWORK_ROOT:-/home/claude/bubble-ops-loop}"
DEPT="${1:-}"

log() { logger -t vendor-dept-libs "$*" 2>/dev/null; echo "[vendor-dept-libs] $*" >&2; }

[[ -n "$DEPT" && -d "$DEPT" ]] || { log "WARN: dept dir '$DEPT' missing — skip (fail-open)"; exit 0; }
[[ -d "$FRAMEWORK" ]] || { log "WARN: framework '$FRAMEWORK' missing — skip (fail-open)"; exit 0; }

# Canonical shared libs: "src-relative-to-framework  dest-relative-to-dept".
# Only files the dept actually uses; a dept missing the dest dir is skipped.
MAP=(
  "scripts/lib/dispatch_helpers.py   scripts/lib/dispatch_helpers.py"
  "scripts/lib/notify.py             scripts/lib/notify.py"
  "scripts/lib/loop_notify.py        scripts/lib/loop_notify.py"
  "scripts/lib/notion_logbook.py     scripts/lib/notion_logbook.py"
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
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    if cp -f "$src" "$dst" 2>/dev/null; then
      chown claude:claude "$dst" 2>/dev/null || true
      log "re-vendored $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
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
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    if cp -f "$src" "$dst" 2>/dev/null; then
      chmod +x "$dst" 2>/dev/null || true   # the .sh files must stay executable
      chown claude:claude "$dst" 2>/dev/null || true
      log "re-vendored kanban $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
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
