#!/usr/bin/env python3
"""
test_fixture_step1.py — Step 1 acceptance tests for the MVP fixture dept.yaml.

Strict TDD harness for MVP-ROADMAP v2 Step 1 ("Lock fixture's dept.yaml w/ full
Notion shape"). Notion source-of-truth: page edited 2026-05-20T15:04 UTC, dump
at /tmp/notion_final.txt. Canonical reference passage: line 654 — "L'agent
raisonne. Python calcule. Filesystem connecte. Git audit."

These 9 tests assert the Step-1 fixture is strictly richer than Step 0's
minimum-viable example: at least 2 gate_policies (one "never-auto", one
auto-eligible), at least one recurring_mission cross-referencing a
gate_policy_id, an explicit `optional_domain_ledger: null` (NOT absent), and
the full hierarchy block coherent with an ops-leaf dept (parent=null,
children=[], no visibility, secrets-readable hardcoded false).

The 9 assertions are split across 9 functions for clear failure attribution
when run under pytest (`pytest -v` shows one PASS/FAIL line per assertion).
The module is also runnable standalone (`python3 tests/test_fixture_step1.py`)
in which case it counts pass/fail and exits 0 iff all 9 pass — required by
ACCEPTANCE in the Step 1 brief because pytest may not be on PATH in CI.

Per CLAUDE.md (R&D workspace): tests live alongside the schemas they probe.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
    from jsonschema import Draft7Validator
except ImportError as exc:
    print(f"[FATAL] missing dep: {exc}. Install via:")
    print("  pip3 install jsonschema pyyaml --break-system-packages")
    sys.exit(2)


# Resolve paths relative to this file so the runner is invocation-safe.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
FIXTURE_PATH = ROOT / "examples" / "dept-ops-leaf-fixture.yaml"
SCHEMA_PATH = ROOT / "dept.schema.yaml"


def _load_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _fixture() -> dict:
    """Load the fixture and assert it's a mapping. Cached at module load is
    avoided on purpose — tests may run after the file is rewritten between
    RED and GREEN iterations and we want fresh reads every time."""
    doc = _load_yaml(FIXTURE_PATH)
    assert isinstance(doc, dict), f"{FIXTURE_PATH} did not parse as a mapping"
    return doc


def _schema() -> dict:
    doc = _load_yaml(SCHEMA_PATH)
    assert isinstance(doc, dict), f"{SCHEMA_PATH} did not parse as a mapping"
    return doc


# ---------------------------------------------------------------------------
# Test 1 — schema validation
# ---------------------------------------------------------------------------
def test_validates_against_dept_schema():
    """The fixture MUST validate against dept.schema.yaml with zero errors.

    This is the load-bearing acceptance criterion of Step 1 per
    MVP-ROADMAP §3 Step 1: "dept.yaml validates against dept.schema.yaml".
    Run Draft7Validator and collect every error; report all of them so
    a single failure run surfaces the full set of problems.
    """
    instance = _fixture()
    schema = _schema()
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, (
        "fixture failed schema validation:\n  - "
        + "\n  - ".join(f"{list(e.path) or '<root>'}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Test 2 — department wrapper with canonical slug
# ---------------------------------------------------------------------------
def test_has_department_wrapper_with_canonical_slug():
    """Per Notion v3 §"Gates levables & missions récurrentes" line 279, every
    dept.yaml MUST use the `department:` wrapper (slug/level/mandate)
    introduced in v3 — the v1 flat `name:` shape is forbidden. Slug must be
    `fixture`, level must be `ops` (leaf), mandate must be ≥10 chars
    (matches the schema's minLength)."""
    doc = _fixture()
    assert "department" in doc, "missing top-level `department:` wrapper (v3 mandatory)"
    dept = doc["department"]
    assert isinstance(dept, dict), "`department:` must be a mapping"
    assert dept.get("slug") == "fixture", f"slug must be 'fixture', got {dept.get('slug')!r}"
    assert dept.get("level") == "ops", f"level must be 'ops' (leaf), got {dept.get('level')!r}"
    mandate = dept.get("mandate")
    assert isinstance(mandate, str) and len(mandate) >= 10, (
        f"mandate must be a string ≥10 chars, got {mandate!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — all 4 layers subscribed
# ---------------------------------------------------------------------------
def test_subscribes_to_all_four_layers():
    """Per MVP-ROADMAP §3 Step 1: fixture subscribes to [1,2,3,4]. The
    fixture exists precisely to exercise the full 4-layer OODA roundtrip;
    subscribing to a subset would defeat its purpose."""
    doc = _fixture()
    subscribed = doc.get("layers", {}).get("subscribed")
    assert subscribed == [1, 2, 3, 4], (
        f"layers.subscribed must be [1,2,3,4] (full OODA), got {subscribed!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — at least one recurring mission, cadence matches schema pattern
# ---------------------------------------------------------------------------
def test_has_at_least_one_recurring_mission():
    """Per Notion §"Exemple Maya" lines 337-373: recurring_missions REPLACE
    the v1 crons-as-mini-apps. The fixture MUST declare ≥1 mission (the
    echo_heartbeat stub) so Layer 1 has something to materialize into a
    queue item. The cadence must match the schema's documented pattern
    (daily|weekly|hourly|every_Nh|every_Nm|cron:expr)."""
    import re

    doc = _fixture()
    missions = doc.get("recurring_missions")
    assert isinstance(missions, list) and len(missions) >= 1, (
        f"recurring_missions must be a non-empty list, got {missions!r}"
    )
    cadence_pattern = re.compile(r"^(daily|weekly|hourly|every_\d+h|every_\d+m|cron:.+)$")
    first = missions[0]
    cadence = first.get("cadence")
    assert isinstance(cadence, str) and cadence_pattern.match(cadence), (
        f"first mission's cadence {cadence!r} does not match schema pattern"
    )


# ---------------------------------------------------------------------------
# Test 5 — recurring_mission.gate_policy_id resolves to a real gate_policy
# ---------------------------------------------------------------------------
def test_recurring_mission_gate_policy_id_resolves():
    """Cross-reference integrity: when a recurring_mission declares
    `gate_policy_id:`, the value MUST be a key under top-level
    `gate_policies:`. The schema validates the field shape but cannot
    validate the cross-reference (that's a runtime check). This is the
    failure mode that produces "gate policy 'foo' not found" at runtime
    if not caught here."""
    doc = _fixture()
    missions = doc.get("recurring_missions", [])
    policies = doc.get("gate_policies", {})
    assert isinstance(policies, dict)
    for mission in missions:
        gp_id = mission.get("gate_policy_id")
        if gp_id is None:
            continue
        assert gp_id in policies, (
            f"recurring_mission {mission.get('id')!r} references gate_policy_id "
            f"{gp_id!r} but it's not a key under top-level gate_policies "
            f"(keys present: {sorted(policies.keys())})"
        )


# ---------------------------------------------------------------------------
# Test 6 — at least 2 gate_policies, one "never-auto", one auto-eligible
# ---------------------------------------------------------------------------
def test_has_at_least_two_gate_policies_one_never_auto():
    """Per Notion §"Doctrine d'autonomie progressive" lines 250-267: the
    5-mode ladder includes both raisable policies (e.g. prospect_dm: can
    be raised to auto_with_veto_window) AND policies that must NEVER
    auto-approve (mandate breaches, capital reallocation). The schema
    permits both via `eligible_future_modes: []` for never-auto policies.

    The fixture MUST declare both kinds so the schema's full range is
    exercised — a fixture with only auto-eligible policies would silently
    let a never-auto policy slip through schema validation when a real
    dept later forgets to set eligible_future_modes correctly.
    """
    doc = _fixture()
    policies = doc.get("gate_policies", {})
    assert isinstance(policies, dict) and len(policies) >= 2, (
        f"gate_policies must declare ≥2 entries (one never-auto, one auto-eligible), "
        f"got {len(policies) if isinstance(policies, dict) else 'non-dict'}"
    )
    has_never_auto = any(
        p.get("eligible_future_modes") == [] for p in policies.values()
    )
    has_auto_eligible = any(
        isinstance(p.get("eligible_future_modes"), list)
        and len(p.get("eligible_future_modes", [])) >= 1
        for p in policies.values()
    )
    assert has_never_auto, (
        "expected at least one gate_policy with `eligible_future_modes: []` "
        "(the 'never-auto' kind — Notion §Doctrine line 250+)"
    )
    assert has_auto_eligible, (
        "expected at least one gate_policy with non-empty `eligible_future_modes` "
        "(the auto-eligible kind — Notion §Doctrine line 250+)"
    )


# ---------------------------------------------------------------------------
# Test 7 — hierarchy block coherent with ops-leaf
# ---------------------------------------------------------------------------
def test_hierarchy_block_present_for_ops_leaf():
    """Per Notion §"Hiérarchie & visibilité cross-dept" lines 138-249 +
    MVP-ROADMAP Step 1: even ops-leaf depts ship the full hierarchy block
    so Tony migration can copy the shape verbatim.

    Fixture invariants (ops-leaf, standalone):
      - hierarchy.level == "ops"
      - parent == null (fixture has no parent; real ops depts → "tony")
      - children == [] (ops depts have no children)
      - visibility.read_secrets == False (schema-hardcoded but assert anyway)
      - visibility.read_raw_artifacts == False (Notion line 239: Tony NEVER
        reads raw artifacts; ops depts even less so)
    """
    doc = _fixture()
    hier = doc.get("hierarchy")
    assert isinstance(hier, dict), "missing or non-mapping `hierarchy:` block"
    assert hier.get("level") == "ops", f"hierarchy.level must be 'ops', got {hier.get('level')!r}"
    assert hier.get("parent") is None, f"hierarchy.parent must be null, got {hier.get('parent')!r}"
    assert hier.get("children") == [], f"hierarchy.children must be [], got {hier.get('children')!r}"
    vis = hier.get("visibility", {})
    assert vis.get("read_secrets") is False, "hierarchy.visibility.read_secrets must be False"
    assert vis.get("read_raw_artifacts") is False, (
        "hierarchy.visibility.read_raw_artifacts must be False"
    )


# ---------------------------------------------------------------------------
# Test 8 — optional_domain_ledger is EXPLICITLY null
# ---------------------------------------------------------------------------
def test_optional_domain_ledger_is_explicitly_null():
    """Per Notion line 49: "Maya ou Tony peuvent rester filesystem-only en
    v1" + Notion line 628: "Les domain ledgers restent autorisés quand ils
    sont nécessaires" → the field MUST be present from day 1 (slot exists)
    even when value is null. This is anti-footgun: a future dept that
    needs a ledger will fail-loud if the slot is missing.

    Critical: this test asserts the KEY is present AND the value is None
    — NOT that the key is absent. A `dict.get(k)` returning None for a
    missing key would falsely pass; we use `in` first.
    """
    doc = _fixture()
    assert "optional_domain_ledger" in doc, (
        "`optional_domain_ledger:` key must be present at root (Notion §"
        "Doctrine storage — slot present from day 1 even if value null)"
    )
    assert doc["optional_domain_ledger"] is None, (
        f"`optional_domain_ledger:` must be explicit null for the fixture "
        f"(filesystem-only), got {doc['optional_domain_ledger']!r}"
    )


# ---------------------------------------------------------------------------
# Test 9 — skills + tools both have content
# ---------------------------------------------------------------------------
def test_skills_and_tools_declared():
    """Per Notion §"Skill vs tool" lines 76-97 + MVP-ROADMAP non-negotiable
    #2: every dept declares BOTH layers' skills AND tools, even with
    stubs. The fixture's echo-skill (4 layer entries) + echo-tool (1
    tool slug) prove the two-tier pattern is present.

    Required:
      - skills.layer_1..layer_4 each have ≥1 entry (full 4-layer roundtrip)
      - tools has ≥1 entry
    """
    doc = _fixture()
    skills = doc.get("skills", {})
    assert isinstance(skills, dict), "`skills:` must be a mapping"
    for k in ("layer_1", "layer_2", "layer_3", "layer_4"):
        entries = skills.get(k)
        assert isinstance(entries, list) and len(entries) >= 1, (
            f"skills.{k} must be a non-empty list, got {entries!r}"
        )
    tools = doc.get("tools")
    assert isinstance(tools, list) and len(tools) >= 1, (
        f"tools must be a non-empty list, got {tools!r}"
    )


# ---------------------------------------------------------------------------
# Standalone runner — exit 0 iff all 9 tests pass.
# ---------------------------------------------------------------------------
def _all_tests():
    return [
        test_validates_against_dept_schema,
        test_has_department_wrapper_with_canonical_slug,
        test_subscribes_to_all_four_layers,
        test_has_at_least_one_recurring_mission,
        test_recurring_mission_gate_policy_id_resolves,
        test_has_at_least_two_gate_policies_one_never_auto,
        test_hierarchy_block_present_for_ops_leaf,
        test_optional_domain_ledger_is_explicitly_null,
        test_skills_and_tools_declared,
    ]


def main() -> int:
    tests = _all_tests()
    passed = 0
    failed = 0
    print(f"Running {len(tests)} Step-1 fixture acceptance tests")
    print(f"Fixture: {FIXTURE_PATH.relative_to(ROOT)}")
    print(f"Schema:  {SCHEMA_PATH.relative_to(ROOT)}")
    print()
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"[FAIL] {t.__name__}")
            for line in str(exc).splitlines():
                print(f"       {line}")
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 — surface any unexpected error
            print(f"[ERROR] {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        print(f"[PASS] {t.__name__}")
        passed += 1
    print()
    print(f"Summary: {passed} passed, {failed} failed (of {len(tests)})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
