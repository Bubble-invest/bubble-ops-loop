#!/usr/bin/env bash
# install-kanban-drain.sh — deploy kanban-queue-drain timer + service (#440)
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
#
# WHY: emit_kanban_item.sh falls back to a local kanban_queue.jsonl when the
# board is unreachable and warns LOUD (stderr [WARN] + Telegram). Until now
# nothing drained that queue — tools/kanban/drain_kanban_queue.sh existed and
# worked (tested), but no timer ever called it. This wires the existing
# drainer into a periodic systemd unit, mirroring install-secrets-sweep.sh.
#
# Board #1251: also installs (but does NOT enable) the per-dept templated
# kanban-queue-drain@.service/.timer. The central kanban-queue-drain.timer
# above runs as the shared `claude` user and cannot read a uid-isolated
# dept's own queue file (e.g. /srv/agents/morty, owned agent-morty:agent-
# morty, post-#1120). A dept whose emit_kanban_item.sh now writes its
# fallback queue under its own $BUBBLE_AGENT_WORKDIR needs its OWN drain
# instance running as that same uid. Enabling is a per-dept, explicit
# follow-up (not automatic here) — run for each uid-isolated dept that
# emits kanban cards from a sandboxed session:
#   sudo systemctl enable --now kanban-queue-drain@<dept>.timer
# (requires tools/kanban/drain_kanban_queue.sh to be vendored into that
# dept's tree first — scripts/vendor-dept-libs.sh / revendor-all-depts.sh).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"

echo "[install-kanban-drain] deploying units..."
sudo cp "$DEPLOY/templates/kanban-queue-drain.service" "$UNIT_DIR/kanban-queue-drain.service" 2>/dev/null || true
sudo cp "$DEPLOY/templates/kanban-queue-drain.timer" "$UNIT_DIR/kanban-queue-drain.timer" 2>/dev/null || true
sudo cp "$DEPLOY/templates/kanban-queue-drain@.service" "$UNIT_DIR/kanban-queue-drain@.service" 2>/dev/null || true
sudo cp "$DEPLOY/templates/kanban-queue-drain@.timer" "$UNIT_DIR/kanban-queue-drain@.timer" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now kanban-queue-drain.timer 2>/dev/null || true
echo "[install-kanban-drain] done (per-dept kanban-queue-drain@<dept>.timer installed but NOT enabled — enable per dept, see header comment)"
