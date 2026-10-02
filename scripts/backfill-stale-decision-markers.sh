#!/usr/bin/env bash
# One-shot (#1639): on a host:local dept's READ-ONLY VPS mirror, remove stale
# cockpit hide-markers inbox/decisions/<gate>.yaml whose git-tracked archive
# inbox/decisions/.processed/<gate>.yaml exists (evidence the dept executed it).
# Decisions with NO archive are only listed (still genuinely unprocessed).
# Default is dry-run; pass --apply to delete. Never touches .processed/.
# Usage: backfill-stale-decision-markers.sh [--apply] [mirror_dir]
set -euo pipefail
APPLY=0; [[ "${1:-}" == "--apply" ]] && { APPLY=1; shift; }
D="${1:-/home/claude/agents/bubble-ops-content}/inbox/decisions"
[[ -d "$D/.processed" ]] || { echo "no $D/.processed" >&2; exit 1; }
n=0
for f in "$D"/*.yaml; do
  [[ -e "$f" ]] || continue
  b=$(basename "$f")
  if [[ -e "$D/.processed/$b" ]]; then
    n=$((n+1)); echo "stale (archived): $b"
    [[ $APPLY -eq 1 ]] && rm -f -- "$f"
  else
    echo "still pending (no archive): $b"
  fi
done
echo "stale markers: $n (apply=$APPLY)"
