#!/usr/bin/env bash
# bubble-ops-fixture/tests/run.sh
# Local test harness for the 4 levels per Notion v4 §"Testabilité par isolation":
#   tool  → skill → layer → department
# Each level has at least one passing test (stub-grade is OK for MVP).
# Exits 0 on full pass; exits with the count of failed levels otherwise.
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0
FAIL=0
LEVELS_FAILED=()

# ----------------------------------------------------------------------------
# Level 1: TOOL — run echo-tool against fixture input, validate output shape.
# ----------------------------------------------------------------------------
TOOL_STATUS=FAIL
TOOL_OUT="$(python3 tools/echo-tool/tool.py < tests/fixtures/tool/echo-input.json 2>&1)"
if echo "$TOOL_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'echoed' in d and 'ts' in d and 'input_keys' in d
assert d['echoed'] == 'hello fixture'
" 2>/dev/null; then
  TOOL_STATUS=PASS
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  LEVELS_FAILED+=("tool")
fi
echo "tool: $TOOL_STATUS"

# ----------------------------------------------------------------------------
# Level 2: SKILL — stub-check that echo-skill SKILL.md has valid frontmatter.
# (Real skill-level tests would run the skill against a mocked context.)
# ----------------------------------------------------------------------------
SKILL_STATUS=FAIL
if python3 -c "
import yaml
with open('skills/echo-skill/SKILL.md') as f:
    content = f.read()
assert content.startswith('---'), 'no frontmatter'
parts = content.split('---', 2)
assert len(parts) >= 3, 'malformed frontmatter'
fm = yaml.safe_load(parts[1])
assert isinstance(fm, dict)
assert 'name' in fm and 'description' in fm
" 2>/dev/null; then
  SKILL_STATUS=PASS
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  LEVELS_FAILED+=("skill")
fi
echo "skill: $SKILL_STATUS"

# ----------------------------------------------------------------------------
# Level 3: LAYER — stub-check that queue-item fixture parses + has required keys.
# (Real layer tests would run a layer prompt against the fixture and verify
# the 4-file output schema lands. We don't have the operator-side schema
# bundled here, so we check structural sanity only.)
# ----------------------------------------------------------------------------
LAYER_STATUS=FAIL
if python3 -c "
import yaml
with open('tests/fixtures/layer/queue-item.yaml') as f:
    d = yaml.safe_load(f)
assert 'id' in d and 'kind' in d
assert 'source_layer' in d and 'target_layer' in d
" 2>/dev/null; then
  LAYER_STATUS=PASS
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  LEVELS_FAILED+=("layer")
fi
echo "layer: $LAYER_STATUS"

# ----------------------------------------------------------------------------
# Level 4: DEPARTMENT — stub-check that dept.yaml parses + has required
# top-level shape (department wrapper, hierarchy, optional_domain_ledger slot).
# ----------------------------------------------------------------------------
DEPT_STATUS=FAIL
if python3 -c "
import yaml
with open('dept.yaml') as f:
    d = yaml.safe_load(f)
assert 'department' in d and d['department']['slug'] == 'fixture'
assert 'hierarchy' in d
assert 'optional_domain_ledger' in d
assert d['optional_domain_ledger'] is None
" 2>/dev/null; then
  DEPT_STATUS=PASS
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  LEVELS_FAILED+=("department")
fi
echo "department: $DEPT_STATUS"

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "Failed levels: ${LEVELS_FAILED[*]}"
fi
exit $FAIL
