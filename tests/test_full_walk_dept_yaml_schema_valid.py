"""
test_full_walk_dept_yaml_schema_valid.py — Sprint correctif sentinel #1.

Drives all 5 conversational step runners (mandate, missions, layers,
skills_tools, gates_kpis) with synthetic operator answers, then validates
the resulting `dept.yaml.draft` against the canonical `dept.schema.yaml`.

This test is the **shape sentinel**: it fails if a runner writes any of
the v3 root-level sections (`recurring_missions`, `layers`, `skills`,
`tools`, `gate_policies`) under the `department:` wrapper. The canonical
shape per `schemas-draft/examples/dept-ops-maya.yaml` and v3 schema
requires those sections at TOP LEVEL.

QA-E2E (2026-05-20) found 9 schema violations because the runners nest
them under `department:`. After Fix 1 the count drops to ≤ 0 for the
runner-controlled root sections.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
SCHEMAS_DIR = PROJECT_ROOT / "schemas-draft"
sys.path.insert(0, str(SKILL_ROOT))


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Configuring",
        "validated_steps": [],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text("", encoding="utf-8")
    return draft


def _drive_mandate(tmp_path: Path) -> None:
    """Walk Step 1 (mandate) with synthetic answers.

    Sprint correctif Fix 2 (2026-05-21): substep B captures 3 lines
    (interdits / niveau / owner), not 4 — outputs and success_criteria
    are out of scope for dept.yaml (live in MANDATE.md narrative).
    """
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("mandate")
    runner.start(state, draft)
    # Substep A: pick style 1, approve seed sentence
    runner.on_answer("1")
    runner.on_answer("approuve")
    # Substep B: 3-line clarifications: forbidden / level / owner
    body = (
        "publier sans validation, nommer clients\n"
        "ops\n"
        "operator"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")


def _drive_missions(tmp_path: Path) -> None:
    """Walk Step 2 (missions) — one mission, then close."""
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal_scan")  # topic list
    runner.on_answer("approuve")  # approve the proposal
    runner.on_answer("non")  # close


def _drive_layers(tmp_path: Path) -> None:
    """Walk Step 3 (layers) — subscribe to all 4, approve each focalisation."""
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("layers")
    runner.start(state, draft)
    runner.on_answer("tous")  # subscribe to all 4
    # Approve each layer's focalisation
    for _ in range(4):
        runner.next_prompt()
        runner.on_answer("approuve")


def _drive_skills_tools(tmp_path: Path) -> None:
    """Walk Step 4 (skills_tools) — approve 1 skill per layer + all tools."""
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    safety = 0
    while not runner.is_done() and safety < 100:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        lp = p.lower()
        if "autres" in lp and ("layer" in lp or "skill" in lp or "tool" in lp):
            runner.on_answer("non")
        elif ("tool" in lp and "besoin" in lp) or ("skill" in lp and "besoin" in lp):
            runner.on_answer("ok")
        else:
            runner.on_answer("approuve")


def _drive_gates_kpis(tmp_path: Path) -> None:
    """Walk Step 5 (gates_kpis) — accept the detected class list + approve each policy."""
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    safety = 0
    while not runner.is_done() and safety < 30:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("approuve")


# ----- helpers: schema -----


def _load_dept_schema() -> dict:
    return yaml.safe_load(
        (SCHEMAS_DIR / "dept.schema.yaml").read_text(encoding="utf-8"))


def _validate_dept(draft_doc: dict) -> list:
    import jsonschema
    schema = _load_dept_schema()
    v = jsonschema.Draft7Validator(schema)
    return [
        f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
        for e in v.iter_errors(draft_doc)
    ]


# ----- the sentinel test -----


def test_full_walk_writes_root_level_sections(tmp_path):
    """After all 5 runners have walked, the dept.yaml.draft must place
    `recurring_missions`, `layers`, `skills`, `tools`, `gate_policies`
    at TOP LEVEL (siblings of `department:`), not nested under it.
    """
    _seed_state(tmp_path)
    _seed_draft(tmp_path)
    _drive_mandate(tmp_path)
    _drive_missions(tmp_path)
    _drive_layers(tmp_path)
    _drive_skills_tools(tmp_path)
    _drive_gates_kpis(tmp_path)

    draft_path = tmp_path / "dept.yaml.draft"
    doc = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # The 5 v3 sections MUST be at root level.
    for key in ("recurring_missions", "layers", "skills", "tools",
                "gate_policies"):
        assert key in doc, (
            f"Missing root-level `{key}` in dept.yaml.draft. "
            f"It is nested under `department:` (the v2 layout). "
            f"Schema requires it at root (cf. dept.schema.yaml lines 17-25)."
        )

    # None of the 5 v3 sections may live under `department:` anymore.
    dept = doc.get("department") or {}
    for key in ("recurring_missions", "layers", "skills", "tools",
                "gate_policies"):
        assert key not in dept, (
            f"`{key}` is nested under `department:` — schema rejects "
            f"(`additionalProperties: false` on department block, line 35)."
        )


def test_full_walk_dept_yaml_passes_schema_for_section_shape(tmp_path):
    """Stronger assertion: after Fix 1 (and 2), the dept.yaml.draft validates
    against the canonical dept.schema.yaml for everything the 5 conversational
    runners control (department block, the 5 root sections). Hierarchy + ledger
    are step-7 / promotion concerns and are filled with empty defaults here.
    """
    _seed_state(tmp_path)
    _seed_draft(tmp_path)
    _drive_mandate(tmp_path)
    _drive_missions(tmp_path)
    _drive_layers(tmp_path)
    _drive_skills_tools(tmp_path)
    _drive_gates_kpis(tmp_path)

    draft_path = tmp_path / "dept.yaml.draft"
    doc = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # Fill hierarchy + optional_domain_ledger (promotion-time concerns)
    # so the schema can run end-to-end. The schema validation is what we
    # actually care about for the dept block + the 5 root sections.
    doc.setdefault("hierarchy", {
        "level": "ops",
        "parent": "tony",
        "children": [],
        "visibility": {
            "read_outputs": [],
            "read_risk_kpis": False,
            "read_risk_briefs": False,
            "read_raw_artifacts": False,
            "read_secrets": False,
        },
        "directive_policy": {
            "can_open_priority_prs": False,
            "target_queue": "queues/management/",
            "requires_human_gate_for": [],
        },
    })
    doc.setdefault("optional_domain_ledger", None)

    errors = _validate_dept(doc)
    assert errors == [], (
        "dept.yaml.draft after a full walk should validate. "
        f"Errors:\n  - " + "\n  - ".join(errors)
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
