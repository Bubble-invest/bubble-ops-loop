#!/usr/bin/env bash
# Publish both the previous completed Paris day and today's partial snapshot.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COST_DIR="${BUBBLE_FLEET_COST_DIR:-/var/lib/bubble-fleet/costs}"
export BUBBLE_COST_PROJECTS_DIR="${BUBBLE_COST_PROJECTS_DIR:-/home/claude/.claude/projects}"
mapfile -t COST_DAYS < <(python3 -I -c 'from datetime import datetime,timedelta; from zoneinfo import ZoneInfo; d=datetime.now(ZoneInfo("Europe/Paris")).date(); print(d-timedelta(days=1)); print(d)')
python3 "$REPO_ROOT/console/services/cost_tracker.py" --fleet-summary --day "${COST_DAYS[0]}" --out "$COST_DIR/${COST_DAYS[0]}.json"
python3 "$REPO_ROOT/console/services/cost_tracker.py" --fleet-summary --day "${COST_DAYS[1]}" --out "$COST_DIR/${COST_DAYS[1]}.json" --latest "$COST_DIR/latest.json"
