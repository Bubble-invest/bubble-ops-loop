#!/usr/bin/env bash
# emit.sh — thin, portable wrapper around the canonical kanban emitter.
#
# WHY: the underlying tool (emit_kanban_item.sh) is vendored into each dept at
# tools/kanban/, with a framework fallback at /home/claude/bubble-ops-loop/.
# Rather than have every agent re-derive that path resolution, this wrapper
# finds the tool and forwards all args verbatim. The skill calls THIS, so the
# path logic lives in one place.
#
# USAGE (identical arg surface to emit_kanban_item.sh):
#   scripts/emit.sh task=<id> title="..." [body="..." type=... priority=... \
#                   owner=... proj=... due=YYYY-MM-DD host=local|vps actions=... \
#                   context_url=... diagram_mermaid="..." visual_attachments="..."]
#
# Resolution order:
#   1. <dept-repo-root>/tools/kanban/emit_kanban_item.sh  (the vendored copy)
#   2. /home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh  (framework)
#   3. ~/claude-workspaces/Rick_RnD/tools/kanban/emit_kanban_item.sh  (Rick dev Mac)
#
# Exit code is the tool's own (which is always 0 — emission must never break a
# tick). On no tool found, prints a clear error and exits 1 so the caller knows.

set -uo pipefail

EMIT=""
# 1. dept-local vendored copy (relative to the current git repo root)
root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$root" ] && [ -x "$root/tools/kanban/emit_kanban_item.sh" ]; then
  EMIT="$root/tools/kanban/emit_kanban_item.sh"
fi
# 2. framework path (VPS)
if [ -z "$EMIT" ] && [ -x "/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh" ]; then
  EMIT="/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh"
fi
# 3. Rick dev-Mac path
if [ -z "$EMIT" ] && [ -x "$HOME/claude-workspaces/Rick_RnD/tools/kanban/emit_kanban_item.sh" ]; then
  EMIT="$HOME/claude-workspaces/Rick_RnD/tools/kanban/emit_kanban_item.sh"
fi

if [ -z "$EMIT" ]; then
  echo "emit.sh: could not locate emit_kanban_item.sh (checked dept tools/kanban/, framework, Rick dev path)" >&2
  exit 1
fi

exec "$EMIT" "$@"
