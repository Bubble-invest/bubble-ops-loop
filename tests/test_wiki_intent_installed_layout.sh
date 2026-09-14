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
DELTA="$DEPLOY_HOME/scripts/wiki-delta.py"
SKILL="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/SKILL.md"
MISSION="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/missions/intent-backfill.md"

test -x "$LAUNCHER"
test -x "$AUDIT"
test -x "$DELTA"
test -r "$SKILL"
test -r "$MISSION"
grep -qF '/home/claude/scripts/wiki-intent-audit.py' "$LAUNCHER"
grep -qF '/home/claude/scripts/wiki-delta.py' "$LAUNCHER"
grep -qF '/home/claude/.claude/skills/cloud-wiki-compile/missions/intent-backfill.md' "$SKILL"

WIKI="$TEST_ROOT/wiki"
MIRROR="$TEST_ROOT/mirror"
mkdir -p "$MIRROR/operator-intents" "$WIKI/shared/systems"
printf '%s\n' '---' 'title: Root intent' 'core: true' '---' '# Root' \
  > "$MIRROR/operator-intents/root.md"
printf '%s\n' '---' 'title: Candidate' 'intent: []' '---' '# Candidate' \
  > "$WIKI/shared/systems/candidate.md"

REPORT="$TEST_ROOT/latest.json"
# #1333: the operational CLI no longer requires a root-owned, symlinked,
# manifest-verified mirror (#1267) — a plain readable directory containing an
# operator-intents/ collection (e.g. the wiki's own copy) is now accepted.
AUDIT_ERR="$TEST_ROOT/audit.err"
if ! python3 "$AUDIT" --wiki "$WIKI" --intents-root "$MIRROR" --output "$REPORT" 2>"$AUDIT_ERR"; then
  echo "FAIL: operational audit rejected a plain readable intents directory (#1333)" >&2
  cat "$AUDIT_ERR" >&2
  exit 1
fi
python3 - "$AUDIT" "$WIKI" "$MIRROR" "$REPORT" <<'PY'
import importlib.util, json, pathlib, sys

audit_path, wiki, mirror, report_path = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(audit_path.parent))
spec = importlib.util.spec_from_file_location("installed_wiki_intent_audit", audit_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report = module._audit_validated_release_for_tests(wiki, mirror)
module._atomic_write(report_path, json.dumps(report) + "\n")
assert report["summary"]["pages_scanned"] == 1
assert report["summary"]["candidate_pages"] == 1
assert report["candidates"][0]["issues"] == ["empty_intent"]
PY

echo "PASS: cloud-wiki intent assets work from installed layout"
