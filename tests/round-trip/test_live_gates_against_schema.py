"""
Gap C1 — Live gates schema-drift regression alarm.

Empirical evidence (STEP-8-ROUND-TRIP-RESULTS.md §Schema note):
  > "the gate uses `kind: research_decision`, which is NOT in the
  > gate-item v3 schema enum [...]. The schema falls through to the
  > `domain:<snake_case>` pattern but requires the `domain:` prefix.
  > So strictly speaking this gate would fail `gate-item.schema.yaml
  > v3` validation."

STEP-9-SCHEMA-RELAXATION-v3.1.md chose to relax the management-export
schema instead of tightening Layer-2's prompt. This test takes the
OPPOSITE direction: scan LIVE gates emitted by the deployed /loop on
/tmp/bubble-ops-fixture/ and report each one's validity against the
canonical schemas-draft/gate-item.schema.yaml.

Failing rows are marked xfail (strict=False) so the gap stays visible
in CI without breaking the build. Once Maya (or whoever owns Layer-2
prompts later) fixes the prompt to emit `kind: decision` or
`kind: domain:research_decision`, the xfail will XPASS — that's the
signal to remove the xfail decorator and tighten the contract.

Conforming gates (e.g. ones that use `kind: decision` natively) DO
assert positively — this isn't an all-xfail test, it's a hybrid that
graduates each gate to GREEN as the prompts get fixed.

Run:
    python3 -m pytest tests/round-trip/test_live_gates_against_schema.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent  # bubble-ops-loop/
SCHEMA_PATH = PROJECT_ROOT / "schemas-draft" / "gate-item.schema.yaml"
LIVE_GATES_DIR = Path("/tmp/bubble-ops-fixture/queues/gates")

# Gates currently known to fail strict gate-item.schema.yaml v3 validation.
# When a gate is migrated to a conforming `kind`, REMOVE it from this set —
# the test will then assert positively and XPASS if the gate now validates.
#
# Tracked: STEP-8-ROUND-TRIP-RESULTS.md §Schema note — Layer-2 PROMPT.md
# emits `kind: research_decision`, which violates both the enum and the
# `domain:<snake_case>` pattern of gate-item.schema.yaml v3.
#
# xfail remains until Layer 2 prompt fix lands AND a fresh runtime produces
# new gate files. The canonical template now lives at
# `skills/department-onboarding-guide/templates/layer_2_prompt.md.template`
# (guarded by `tests/test_layer2_prompt_specifies_required_fields.py`). Once
# Rick pushes that template into the live fixture's `layers/2/PROMPT.md`
# via broker+guard and a fresh `/loop` tick emits new gates, these xfails
# will XPASS and should be removed. Track via:
#     git log --oneline -- queues/gates/
KNOWN_DRIFT_GATES: frozenset[str] = frozenset(
    {
        "gate-roundtrip-test-001.yaml",
        "gate-notif-test-002.yaml",
    }
)


# -----------------------------------------------------------------------------
# Loaders
# -----------------------------------------------------------------------------


def _load_schema() -> dict:
    with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _discover_live_gates() -> list[Path]:
    """Enumerate all gate YAML files under the live fixture clone."""
    if not LIVE_GATES_DIR.exists():
        return []
    return sorted(LIVE_GATES_DIR.glob("*.yaml"))


def _violation_summary(schema: dict, gate_doc: dict) -> list[str]:
    """Return a list of human-readable JSON-Schema violation strings."""
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(gate_doc), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors
    ]


# -----------------------------------------------------------------------------
# Discovery + parametrization
# -----------------------------------------------------------------------------

_GATE_FILES = _discover_live_gates()


def test_live_gates_directory_exists() -> None:
    """Pre-flight: the fixture clone is present and has at least one gate.

    If this fails, the rest of the suite is meaningless — likely
    /tmp/bubble-ops-fixture/ was wiped or git pull never ran."""
    assert LIVE_GATES_DIR.exists(), (
        f"Live gates directory missing: {LIVE_GATES_DIR}. "
        "Run `git -C /tmp/bubble-ops-fixture pull` first."
    )
    assert _GATE_FILES, (
        f"No gate YAML files under {LIVE_GATES_DIR}. "
        "Has the loop emitted any gates yet?"
    )


def _xfail_marker_for(gate_filename: str):
    """Return an xfail mark when the gate is in the known-drift set, else None."""
    if gate_filename in KNOWN_DRIFT_GATES:
        return pytest.mark.xfail(
            reason=(
                "known drift: prompt produces research_decision but schema enum "
                "lacks it. Tracked for Maya migration."
            ),
            strict=False,
        )
    return None


def _params() -> list:
    """Build parametrize entries, attaching xfail to known-drift gates."""
    out = []
    for gate_path in _GATE_FILES:
        marks = []
        mark = _xfail_marker_for(gate_path.name)
        if mark is not None:
            marks.append(mark)
        out.append(pytest.param(gate_path, id=gate_path.name, marks=marks))
    return out


@pytest.mark.parametrize("gate_path", _params())
def test_live_gate_matches_gate_item_schema_v3(gate_path: Path) -> None:
    """Each live gate must validate against gate-item.schema.yaml v3.

    Gates listed in KNOWN_DRIFT_GATES are marked xfail (the failure stays
    visible without breaking CI). When the prompt is fixed and a gate
    starts validating, the xfail will XPASS — that's the signal to remove
    the entry from KNOWN_DRIFT_GATES."""
    schema = _load_schema()
    with gate_path.open("r", encoding="utf-8") as fh:
        gate_doc = yaml.safe_load(fh)
    assert isinstance(gate_doc, dict), f"{gate_path.name} is not a YAML mapping"

    violations = _violation_summary(schema, gate_doc)
    assert not violations, (
        f"{gate_path.name} violates gate-item.schema.yaml v3:\n  - "
        + "\n  - ".join(violations)
    )
