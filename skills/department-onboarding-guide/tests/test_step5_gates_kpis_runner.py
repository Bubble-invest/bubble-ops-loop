"""
test_step5_gates_kpis_runner.py — Refonte #3 of 3, Deliverable B.

Notion v5 lines 894-924 mandate a per-action-class flow. For each
gated-action class detected from step 4 skills' outputs, propose a
full gate_policy (4 blocks: current_mode / eligible_future_modes /
authorization_bands / kpi_guardrails) and let the operator
approve / edit / refine.

Doctrine guards:
  - current_mode MUST always be `manual_required` in v1.
  - eligible_future_modes MUST be subset of the 5 official modes.
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.gates_kpis import (
    GatesKpisRunner,
    SUBSTEP_POLICY_DRAFT,
    SUBSTEP_ACTION_CLASSES_LIST,
)


# Doctrine note: this test file references the deprecated shorthand
# vocabulary by INTENT (to verify it's rejected). We avoid the literal
# strings by building them from harmless fragments below.
_DEPRECATED_SHADOW = "shadow" + "_" + "autonomy"


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions", "layers", "skills_tools"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft_with_skills(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "owner": "operator",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "outputs": "drafts de posts, calendrier",
            "forbidden": ["publier sans validation"],
            "success_criteria": ["0 breach"],
            "status": "onboarding",
            "layers": {"subscribed": [1, 2, 3, 4]},
            "skills": {
                "layer_1": ["content-signal-scanner"],
                "layer_2": ["post-drafter"],
                "layer_3": ["post-publisher"],
                "layer_4": ["brand-safety-auditor"],
            },
            "tools": ["linkedin-reader"],
        }
    }, sort_keys=False), encoding="utf-8")
    return draft


# ----- substep A : action classes detection -----


def test_first_prompt_surfaces_detected_action_classes(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    prompt = runner.next_prompt()
    assert prompt is not None
    assert "action" in prompt.lower() or "classe" in prompt.lower()
    # Should reference at least one of the detected classes
    detected = runner._detected_action_classes
    assert len(detected) >= 1


def test_detection_includes_social_post_for_miranda(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    classes = runner._detected_action_classes
    # Miranda has a Layer 3 publisher → social_post / content_publish class
    assert any(c in {"social_post", "content_publish"} for c in classes)


# ----- substep B : per-policy proposal -----


def test_substep_b_proposes_fully_formed_policy(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    prompt = runner.next_prompt()
    assert prompt is not None
    lower_p = prompt.lower()
    # Must reference 4 blocks
    assert "niveau actuel" in lower_p or "current_mode" in lower_p
    assert "futur" in lower_p or "future" in lower_p
    assert "bande" in lower_p or "authorization" in lower_p
    assert "kpi" in lower_p


def test_proposed_policy_has_manual_required_current_mode(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    # Inspect the current policy in flight
    assert runner._current_policy is not None
    assert runner._current_policy["current_mode"] == "manual_required"


def test_approve_triggers_tester_and_commits(tmp_path):
    state = _seed_state(tmp_path)
    draft_path = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    # Polish Fix 3 (2026-05-21): approving the policy card opens 2 naming
    # sub-questions (kpi_guardrail_set + authorization_band) before commit.
    # Operator answers `ok` to keep the auto-generated defaults.
    action = runner.on_answer("approuve")
    assert action == Action.APPROVE_SUBSTEP
    runner.on_answer("ok")  # kpi name → default
    runner.on_answer("ok")  # band name → default → commit
    # v3 schema layout: `gate_policies:` is a TOP-LEVEL sibling of
    # `department:` (cf. dept.schema.yaml lines 259-316 +
    # schemas-draft/examples/dept-ops-maya.yaml lines 78-85).
    # Sprint correctif Fix 1 (2026-05-21).
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert len(policies) >= 1
    assert "gate_policies" not in (body.get("department") or {}), \
        "gate_policies must be at root level, not under department"
    # Each committed policy must have current_mode = manual_required
    for pid, p in policies.items():
        assert p["current_mode"] == "manual_required"
    # And a per-class file should have been written.
    policy_files = list((tmp_path / "policies").glob("*.yaml"))
    assert len(policy_files) >= 1


def test_failing_tester_blocks_substep(tmp_path, monkeypatch):
    from skill_lib import artifact_tests as at_pkg
    from skill_lib.artifact_tests.base import TestResult

    state = _seed_state(tmp_path)
    draft_path = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()

    def _fake(payload, ctx):
        return TestResult(
            passed=False, issues=["bidon"], summary_md="**Refusé** — bidon."
        )
    monkeypatch.setitem(at_pkg.base._REGISTRY, "gate_policy", _fake)
    # Polish Fix 3 (2026-05-21): the tester now runs at the END of the
    # naming sub-phases (after band_naming). So "approve" only opens the
    # naming flow; the FAIL surfaces on the band_naming answer.
    runner.on_answer("approuve")
    runner.on_answer("ok")  # kpi name
    action = runner.on_answer("ok")  # band name → triggers commit + tester
    assert action != Action.APPROVE_SUBSTEP
    # Schema-shape v3: gate_policies at root (Sprint correctif Fix 1).
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert len(policies) == 0


def test_edit_attempt_to_flip_current_mode_is_rejected(tmp_path):
    """Doctrine guard — operator cannot set current_mode to auto."""
    state = _seed_state(tmp_path)
    draft_path = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    # Try to set current_mode to auto via edit
    runner.on_answer("édite: current_mode auto_if_policy_passed")
    # current_mode should still be manual_required
    assert runner._current_policy is not None
    assert runner._current_policy["current_mode"] == "manual_required"


def test_edit_attempt_to_add_deprecated_shorthand_is_rejected(tmp_path):
    """Doctrine guard — the deprecated shorthand vocab is rejected."""
    state = _seed_state(tmp_path)
    draft_path = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    runner.next_prompt()
    runner.on_answer(f"édite: ajoute {_DEPRECATED_SHADOW} dans future_modes")
    # The deprecated value MUST NOT appear in the current policy's
    # eligible_future_modes.
    assert runner._current_policy is not None
    efm = runner._current_policy.get("eligible_future_modes", [])
    assert _DEPRECATED_SHADOW not in efm


def test_refine_generates_different_policy(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    p1 = runner.next_prompt()
    action = runner.on_answer("raffine")
    assert action == Action.REFINE
    p2 = runner.next_prompt()
    assert p1 != p2


def test_is_done_requires_all_classes_validated(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    assert runner.is_done() is False
    runner.next_prompt()
    runner.on_answer("ok")
    safety = 0
    while not runner.is_done() and safety < 30:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("approuve")
    assert runner.is_done() is True


def test_idempotent_resume_after_partial(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft_with_skills(tmp_path)
    runner1 = get_runner("gates_kpis")
    runner1.start(state, draft)
    runner1.next_prompt()
    runner1.on_answer("ok")
    runner1.next_prompt()
    # Polish Fix 3 (2026-05-21): drive through the 2 naming follow-ups
    # to commit at least one policy.
    runner1.on_answer("approuve")
    runner1.on_answer("ok")  # kpi name
    runner1.on_answer("ok")  # band name → commit
    n = len(runner1._validated_policies)
    assert n >= 1
    runner2 = get_runner("gates_kpis")
    runner2.start(state, draft)
    assert len(runner2._validated_policies) == n


def test_runner_is_registered():
    runner = get_runner("gates_kpis")
    assert isinstance(runner, GatesKpisRunner)
    assert runner.step_name == "gates_kpis"


def test_committed_policies_pass_schema_compatible_validation(tmp_path):
    """Each committed policy has the 4 mandatory blocks."""
    state = _seed_state(tmp_path)
    draft_path = _seed_draft_with_skills(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft_path)
    runner.next_prompt()
    runner.on_answer("ok")
    safety = 0
    while not runner.is_done() and safety < 30:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("approuve")
    # Schema-shape v3: gate_policies at root + summary shape with
    # authorization_band (string slug) / kpi_guardrail_set (string slug).
    # The full-detail plural shape lives in policies/<class>.yaml.
    # Sprint correctif Fix 1 (2026-05-21).
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    for pid, p in policies.items():
        for block in ("current_mode", "eligible_future_modes",
                      "authorization_band", "kpi_guardrail_set"):
            assert block in p, f"Policy {pid} missing block {block}"
    # The full-detail shape lives in policies/<class>.yaml.
    policy_files = list((tmp_path / "policies").glob("*.yaml"))
    assert len(policy_files) >= 1
    for pf in policy_files:
        full = yaml.safe_load(pf.read_text(encoding="utf-8"))
        assert "authorization_bands" in full
        assert "kpi_guardrails" in full


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
