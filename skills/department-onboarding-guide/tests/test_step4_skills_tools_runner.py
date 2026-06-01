"""
test_step4_skills_tools_runner.py — Refonte #3 of 3, Deliverable A.

Notion v5 lines 863-893 mandate a per-card flow: for each subscribed
layer, propose skills one card at a time, then cross-layer tools the
same way. Card shape (lines 887-893):

    content-signal-scanner
    Purpose: détecter des idées de contenu
    Inputs: wiki, LinkedIn, notes
    Outputs: content_idea_task
    Tests: missing
    Status: draft
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.skills_tools import (
    SkillsToolsRunner,
    SUBSTEP_SKILL_NEEDS,
    SUBSTEP_SKILL_DRAFT,
    SUBSTEP_TOOLS_NEEDS,
    SUBSTEP_TOOL_DRAFT,
)


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions", "layers"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path, subscribed=(1, 2, 3, 4)) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "owner": "joris",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "outputs": "drafts de posts, calendrier",
            "forbidden": ["publier sans validation"],
            "success_criteria": ["0 breach", "edit_rate <= 20%"],
            "status": "onboarding",
            "layers": {"subscribed": list(subscribed)},
        }
    }, sort_keys=False), encoding="utf-8")
    # Seed PROMPT.md files for each subscribed layer so the runner has
    # a focalisation to read.
    for n in subscribed:
        d = tmp_path / "layers" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROMPT.md").write_text(
            f"# Layer {n}\n\n## Focalisation\n- skill need A\n- skill need B\n",
            encoding="utf-8",
        )
    return draft


# ----- substep A : skill needs surfacing -----


def test_first_prompt_is_substep_a_for_first_subscribed_layer(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1, 2))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    prompt = runner.next_prompt()
    assert prompt is not None
    # First substep: identify skill needs for Layer 1
    assert "Layer 1" in prompt
    # Must mention "skill" and invite operator to confirm / adjust
    assert "skill" in prompt.lower()
    assert "ajust" in prompt.lower() or "confirme" in prompt.lower() or "modif" in prompt.lower()


def test_substep_a_surfaces_focalisation_signals(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    prompt = runner.next_prompt()
    # The agent should reference at least one of the focalisation bullets
    # OR explain what kind of skills it has identified.
    assert prompt is not None
    assert "identifié" in prompt.lower() or "besoin" in prompt.lower() or "skill" in prompt.lower()


# ----- substep B : per-skill proposal -----


def test_substep_b_proposes_fully_formed_skill_card(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    # Confirm the skill needs list — operator says "ok"
    runner.on_answer("ok, on commence")
    prompt = runner.next_prompt()
    assert prompt is not None
    # Must contain the 5 mandatory fields of a Notion-spec card
    for field in ("Purpose", "Inputs", "Outputs", "Tests", "Status"):
        assert field in prompt, f"Missing field {field} in card prompt"
    # Must propose 3 options (Approve / Edit / Refine)
    lower_p = prompt.lower()
    assert "approuve" in lower_p
    assert "édite" in lower_p or "edit" in lower_p
    assert "raffin" in lower_p or "refine" in lower_p


def test_approve_substep_b_triggers_artifact_test_and_commit(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()  # surface the skill card
    action = runner.on_answer("approuve")
    assert action == Action.APPROVE_SUBSTEP
    # A SKILL.md file must exist under <root>/skills/<skill-name>/
    skill_files = list((tmp_path / "skills").rglob("SKILL.md"))
    assert len(skill_files) >= 1
    body = skill_files[0].read_text(encoding="utf-8")
    assert "Purpose" in body
    assert "Inputs" in body
    assert "Outputs" in body


def test_failing_skill_tester_blocks_substep(tmp_path, monkeypatch):
    """If the tester returns passed=False, do NOT commit."""
    from skill_lib import artifact_tests as at_pkg
    from skill_lib.artifact_tests.base import TestResult

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()

    def _fake_test(payload, ctx):
        return TestResult(
            passed=False,
            issues=["bidon"],
            summary_md="**Refusé** — bidon.",
        )
    monkeypatch.setitem(at_pkg.base._REGISTRY, "skill", _fake_test)
    action = runner.on_answer("approuve")
    assert action != Action.APPROVE_SUBSTEP
    skill_files = list((tmp_path / "skills").rglob("SKILL.md"))
    assert len(skill_files) == 0


def test_edit_applies_operator_text(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    p1 = runner.next_prompt()
    action = runner.on_answer("édite: purpose Préparer un brief de contenu jour")
    assert action == Action.EDIT
    p2 = runner.next_prompt()
    assert p1 != p2


def test_refine_generates_different_card(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    p1 = runner.next_prompt()
    action = runner.on_answer("raffine")
    assert action == Action.REFINE
    p2 = runner.next_prompt()
    assert p1 != p2


# ----- multi-layer flow -----


def test_after_layer_skills_validated_asks_more_or_advances(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1, 2))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    # Approve all proposed skills for layer 1
    for _ in range(10):
        p = runner.next_prompt()
        if p is None:
            break
        # If asked "more for this layer?" answer "non"
        if "autres" in p.lower() and "layer" in p.lower():
            runner.on_answer("non")
            break
        runner.on_answer("approuve")
    # Should have transitioned to Layer 2 OR to tools loop
    p = runner.next_prompt()
    assert p is not None
    assert "Layer 2" in p or "tool" in p.lower()


# ----- tools loop -----


def test_tools_loop_proposes_cards_with_5_fields(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    # Burn through Layer 1 skills
    runner.next_prompt()
    runner.on_answer("ok")
    for _ in range(20):
        p = runner.next_prompt()
        if p is None:
            break
        lp = p.lower()
        if ("autres" in lp and ("layer" in lp or "skill" in lp)):
            runner.on_answer("non")
            continue
        if "tool" in lp and ("identifié" in lp or "besoin" in lp):
            # We hit the tools-needs prompt
            assert "tool" in lp
            runner.on_answer("ok")
            break
        runner.on_answer("approuve")
    # Now we should see a tool card prompt
    p = runner.next_prompt()
    assert p is not None
    if "approuve" in p.lower():
        for field in ("Purpose", "Inputs", "Outputs", "Tests", "Status"):
            assert field in p, f"Missing field {field} in tool card"


# ----- UX commands -----


def test_disable_skill_command_removes_validated_skill(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    runner.on_answer("approuve")
    # First skill validated. Get its name.
    assert len(runner._validated_skills) >= 1
    first = runner._validated_skills[0]
    name = first["name"]
    runner.on_answer(f"disable skill {name}")
    remaining = {s["name"] for s in runner._validated_skills}
    assert name not in remaining
    skill_dir = tmp_path / "skills" / name
    assert not (skill_dir / "SKILL.md").exists()


# ----- done -----


def test_is_done_requires_skills_and_tools_committed(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    assert runner.is_done() is False
    # Walk through skill + tool happy path
    runner.next_prompt()
    runner.on_answer("ok")
    safety = 0
    while not runner.is_done() and safety < 30:
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
    assert runner.is_done() is True


def test_idempotent_resume_after_partial_skills(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1, 2))
    runner1 = get_runner("skills_tools")
    runner1.start(state, draft)
    runner1.next_prompt()
    runner1.on_answer("ok")
    runner1.next_prompt()
    runner1.on_answer("approuve")
    n_validated = len(runner1._validated_skills)
    # Resume in a fresh runner
    runner2 = get_runner("skills_tools")
    runner2.start(state, draft)
    assert len(runner2._validated_skills) == n_validated


def test_dept_yaml_draft_records_skills_per_layer(tmp_path):
    state = _seed_state(tmp_path)
    draft_path = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    runner.on_answer("approuve")
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    # v3 schema layout: `skills:` is a TOP-LEVEL sibling of `department:`
    # (cf. schemas-draft/examples/dept-ops-maya.yaml and dept.schema.yaml
    # lines 230-241). Sprint correctif Fix 1 (2026-05-21).
    skills_section = body.get("skills") or {}
    assert skills_section, "skills must be at root level, not under department"
    # layer_1 list must be present and non-empty
    assert "layer_1" in skills_section
    assert len(skills_section["layer_1"]) >= 1
    # And NOT nested under department:
    assert "skills" not in (body.get("department") or {})


def test_step_progress_records_substep_types(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path, subscribed=(1,))
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    runner.on_answer("approuve")
    state_doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    progress = state_doc["step_progress"]["skills_tools"]
    types_done = {e.get("type") for e in progress.get("sub_artifacts_validated", [])}
    assert SUBSTEP_SKILL_DRAFT in types_done


def test_runner_is_registered():
    runner = get_runner("skills_tools")
    assert isinstance(runner, SkillsToolsRunner)
    assert runner.step_name == "skills_tools"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
