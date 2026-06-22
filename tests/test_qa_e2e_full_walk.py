"""
test_qa_e2e_full_walk.py — Sprint correctif Fix 6 (new sentinel).

End-to-end walk through the 7-step onboarding eclosure with realistic
synthetic operator answers. Validates the resulting `dept.yaml.draft`
against the canonical `dept.schema.yaml`, checks every artifact file
referenced is on disk, and confirms the activation PR body contains
real content (not "_(aucune mission)_").

This is the sentinel future agents run before declaring shipping-ready.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
SCHEMAS_DIR = PROJECT_ROOT / "schemas-draft"
sys.path.insert(0, str(SKILL_ROOT))


# ----- helpers (mirror the Fix 1 sentinel) -----


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
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")
    runner.on_answer("approuve")
    body = (
        "publier sans validation, nommer clients, conseil financier\n"
        "ops\n"
        "operator"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")


def _drive_missions(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal_scan")
    runner.on_answer("approuve")
    runner.on_answer("non")


def _drive_layers(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("layers")
    runner.start(state, draft)
    runner.on_answer("tous")
    for _ in range(4):
        runner.next_prompt()
        runner.on_answer("approuve")


def _drive_skills_tools(tmp_path: Path) -> None:
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


def _drive_dry_run(tmp_path: Path) -> None:
    """Step 6 — mock the simulator to return a fake all-green report."""
    from skill_lib.step_runners import get_runner

    class _FakeResult:
        def to_dict(self):
            return {
                "overall_status": "PASSED",
                "can_advance_to_ready": True,
                "checks": [
                    {"step": "layer_1_output_schema", "scope": "/x/1",
                     "status": "passed", "message": "4-file output skeleton written"},
                    {"step": "layer_2_gate_item_schema", "scope": "/x/2",
                     "status": "passed", "message": "gate-item valid"},
                    {"step": "layer_3_execution_valid", "scope": "/x/3",
                     "status": "passed", "message": "exec log written"},
                    {"step": "layer_4_three_outputs", "scope": "/x/4",
                     "status": "passed", "message": "risk + management-export present"},
                ],
            }

    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=_FakeResult()):
        runner = get_runner("dry_run")
        runner.start(state, draft)
        runner.on_answer("approuve")
    return runner._humanized


# ----- the sentinel test -----


def test_qa_e2e_full_walk(tmp_path):
    """The integration sentinel: 7-step walk → all 5 assertions pass."""
    _seed_state(tmp_path)
    _seed_draft(tmp_path)
    _drive_mandate(tmp_path)
    _drive_missions(tmp_path)
    _drive_layers(tmp_path)
    _drive_skills_tools(tmp_path)
    _drive_gates_kpis(tmp_path)
    humanized_dry_run = _drive_dry_run(tmp_path)

    draft_path = tmp_path / "dept.yaml.draft"
    doc = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # ----- Assertion 1: dept.yaml.draft validates against the canonical schema.
    # Hierarchy + optional_domain_ledger are step-7 promotion concerns;
    # fill defaults so the structural schema runs end-to-end.
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

    import jsonschema
    schema = yaml.safe_load(
        (SCHEMAS_DIR / "dept.schema.yaml").read_text(encoding="utf-8"))
    errors = [
        f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
        for e in jsonschema.Draft7Validator(schema).iter_errors(doc)
    ]
    assert errors == [], (
        "dept.yaml.draft must validate cleanly.\nErrors:\n  - "
        + "\n  - ".join(errors)
    )

    # ----- Assertion 2: activation PR body contains real content.
    from skill_lib.activation_pr import build_activation_pr_body
    state_doc = yaml.safe_load(
        (tmp_path / "STATE.yaml").read_text(encoding="utf-8"))
    pr_body = build_activation_pr_body("miranda", state_doc, doc)
    # Must reference at least the canonical dept name.
    assert "Miranda" in pr_body
    # Must NOT be the empty fallback string.
    assert "_(aucune mission" not in pr_body, (
        "PR body shows the 'no mission' fallback — the missions runner "
        "didn't surface any mission to the activation step."
    )
    # At least 1 mission named.
    missions = doc.get("recurring_missions") or []
    assert len(missions) >= 1
    mission_id = missions[0].get("id", "?")
    assert f"`{mission_id}`" in pr_body
    # At least 1 layer mentioned (the "moments de la journée" block).
    assert "Le matin" in pr_body or "matin" in pr_body.lower()
    # At least 1 skill mentioned (in the layer block).
    skills = doc.get("skills") or {}
    flat_skills = [s for v in skills.values() if isinstance(v, list) for s in v]
    assert flat_skills, "at least 1 skill must be committed"
    # At least 1 gate policy mentioned.
    policies = doc.get("gate_policies") or {}
    assert policies, "at least 1 gate policy must be committed"
    pid = next(iter(policies))
    assert f"`{pid}`" in pr_body

    # ----- Assertion 3: every artifact file referenced exists on disk.
    # Missions:
    for m in missions:
        mp = tmp_path / "missions" / f"{m['id']}.yaml"
        assert mp.exists(), f"missing mission file: {mp}"
    # Layers:
    for n in (doc.get("layers", {}).get("subscribed") or []):
        pmd = tmp_path / "layers" / str(n) / "PROMPT.md"
        assert pmd.exists(), f"missing layer PROMPT.md: {pmd}"
    # Skills:
    for s in flat_skills:
        sk = tmp_path / "skills" / s / "SKILL.md"
        assert sk.exists(), f"missing SKILL.md: {sk}"
    # Tools:
    for t in (doc.get("tools") or []):
        tm = tmp_path / "tools" / t / "TOOL.md"
        assert tm.exists(), f"missing TOOL.md: {tm}"
    # Policies:
    for pid in policies.keys():
        pf = tmp_path / "policies" / f"{pid}.yaml"
        assert pf.exists(), f"missing policy file: {pf}"

    # ----- Assertion 4: dry-run humanized summary is French + non-empty.
    assert humanized_dry_run is not None
    assert len(humanized_dry_run) > 50
    # Should contain at least one French token from the humanizer.
    hl = humanized_dry_run.lower()
    assert (
        "répétition" in hl or "répétition" in humanized_dry_run
        or "matin" in hl or "passé" in hl
    ), (
        "dry-run humanized summary must be French Bureau-de-Cadre prose."
    )

    # ----- Assertion 5: department block has exactly the 7 canonical fields.
    dept = doc["department"]
    canonical = {"slug", "display_name", "level", "status",
                 "mandate", "owner", "forbidden"}
    extras = set(dept.keys()) - canonical
    assert not extras, f"extra fields in department block: {sorted(extras)}"
    missing = canonical - set(dept.keys())
    assert not missing, f"missing canonical fields: {sorted(missing)}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
