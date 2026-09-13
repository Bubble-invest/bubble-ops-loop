#!/usr/bin/env bash
# Exercise the actual cloud-wiki installer asset path without systemd/root.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT

CLOUD_WIKI_INSTALL_ROOT="$TEST_ROOT/root" \
  bash "$REPO_ROOT/scripts/install-cloud-wiki-compile.sh" >/dev/null

DEPLOY_HOME="$TEST_ROOT/root/home/claude"
LAUNCHER="$DEPLOY_HOME/scripts/cloud-wiki-compile.sh"
AUDIT="$DEPLOY_HOME/scripts/wiki-intent-audit.py"
SKILL="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/SKILL.md"
MISSION="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/missions/intent-backfill.md"

test -x "$LAUNCHER"
test -x "$AUDIT"
test -r "$SKILL"
test -r "$MISSION"
grep -qF '/home/claude/scripts/wiki-intent-audit.py' "$LAUNCHER"
grep -qF '/home/claude/.claude/skills/cloud-wiki-compile/missions/intent-backfill.md' "$SKILL"

WIKI="$TEST_ROOT/wiki"
mkdir -p "$WIKI/shared/operator-intents" "$WIKI/shared/systems"
printf '%s\n' '---' 'title: Root intent' 'core: true' '---' '# Root' \
  > "$WIKI/shared/operator-intents/root.md"
printf '%s\n' '---' 'title: Candidate' 'intent: []' '---' '# Candidate' \
  > "$WIKI/shared/systems/candidate.md"

REPORT="$TEST_ROOT/latest.json"
python3 "$AUDIT" --wiki "$WIKI" --output "$REPORT" 2>/dev/null
python3 - "$REPORT" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
assert report["summary"]["pages_scanned"] == 1
assert report["summary"]["candidate_pages"] == 1
assert report["candidates"][0]["issues"] == ["empty_intent"]
PY

echo "PASS: cloud-wiki intent assets work from installed layout"
