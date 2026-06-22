"""
test_step2_missions_runner.py — Refonte #2 of 3, Deliverable A.

Pins the conversational behavior of `MissionsRunner` (the per-step
runner that walks the operator through Notion's 5 UX actions for
Step 2 — Missions récurrentes).

Notion v5 lines 830-846 (verbatim):
    830  ### 2. Missions récurrentes
    831  L'utilisateur décrit ce que l'agent doit surveiller ou produire
    832  régulièrement. L'agent traduit en missions déclaratives :
    ...
    841  Actions UX :
    842    + Add mission
    843    Disable mission
    844    Change cadence
    845    Change layer
    846    Test mission

The runner walks the operator through a substep A (collect topic list)
then loops over each topic as a substep B(i) (propose full mission card,
approve/edit/refine), tests each mission via the per-mission tester
before committing, and supports the 5 UX commands (add / disable /
change cadence / change layer / test) at any time.
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.missions import (
    MissionsRunner,
    SUBSTEP_TOPIC_LIST,
    SUBSTEP_MISSION_DRAFT,
)


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "owner": "operator",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "outputs": "drafts de posts, calendrier",
            "forbidden": ["publier sans validation"],
            "success_criteria": ["0 breach", "edit_rate <= 20%"],
            "status": "onboarding",
        }
    }, sort_keys=False), encoding="utf-8")
    return draft


def _start_with_topic(tmp_path: Path, topic_answer: str) -> MissionsRunner:
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer(topic_answer)
    return runner


# ----- substep A : topic list collection -----


def test_first_prompt_is_substep_a_topic_question(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    prompt = runner.next_prompt()
    assert prompt is not None
    assert "Étape 2" in prompt
    assert "missions" in prompt.lower()
    # Bureau-de-Cadre: must invite 3-5 keywords or 1-2 sentences.
    assert "mot" in prompt.lower() or "phrase" in prompt.lower()


def test_substep_a_parses_keyword_list(tmp_path):
    runner = _start_with_topic(
        tmp_path,
        "signal scan, draft posts, audit hebdo",
    )
    # 3 topics parsed → 3 missions queued (one becomes current, rest pending)
    in_flight = 1 if runner._current_mission is not None else 0
    assert (len(runner._pending_topics) + len(runner._sub_validated)
            + in_flight) >= 3


def test_substep_a_parses_short_sentence(tmp_path):
    runner = _start_with_topic(
        tmp_path,
        "Je veux que tu scannes des signaux le matin et que tu écrives un debrief le soir.",
    )
    # At least 1 topic parsed
    assert len(runner._pending_topics) >= 1


# ----- substep B : per-mission proposal -----


def test_substep_b_proposes_fully_formed_mission_card(tmp_path):
    runner = _start_with_topic(tmp_path, "signal scan")
    prompt = runner.next_prompt()
    assert prompt is not None
    # Must contain the 5 required fields of a Notion-spec mission card.
    for field in ("Cadence", "Layer", "Crée", "Outputs"):
        assert field in prompt
    # Must propose 3 options (approve / édite / refine)
    assert "Approuve" in prompt or "approuve" in prompt
    assert "dite" in prompt  # "Édite" or "édite"
    assert "Refine" in prompt or "raffine" in prompt.lower()


def test_approve_substep_b_triggers_artifact_test_and_commit(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan")
    # Get the mission proposal up
    runner.next_prompt()
    action = runner.on_answer("approuve")
    assert action == Action.APPROVE_SUBSTEP
    # Mission YAML file must exist under <root>/missions/
    mission_files = list((tmp_path / "missions").glob("*.yaml"))
    assert len(mission_files) == 1
    body = yaml.safe_load(mission_files[0].read_text(encoding="utf-8"))
    # Schema-required fields
    for f in ("id", "layer", "cadence", "description", "output_queue", "creates"):
        assert f in body
    # validated_at recorded in step_progress
    state_doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    progress = state_doc["step_progress"]["missions"]
    types_done = {e.get("type") for e in progress["sub_artifacts_validated"]}
    assert SUBSTEP_MISSION_DRAFT in types_done


def test_failing_artifact_test_blocks_substep(tmp_path, monkeypatch):
    """If the tester returns passed=False, we must NOT commit."""
    from skill_lib import artifact_tests as at_pkg
    from skill_lib.artifact_tests.base import TestResult

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan")
    runner.next_prompt()
    # Force the tester to fail.

    def _fake_test(payload, ctx):
        return TestResult(
            passed=False,
            issues=["bidon"],
            summary_md="**Refusé** — bidon.",
        )
    monkeypatch.setitem(
        at_pkg.base._REGISTRY,
        "recurring_mission",
        _fake_test,
    )
    action = runner.on_answer("approuve")
    # Must NOT advance — mission stays in current_substep, no file written.
    assert action != Action.APPROVE_SUBSTEP
    mission_files = list((tmp_path / "missions").glob("*.yaml"))
    assert len(mission_files) == 0


def test_edit_applies_operator_text_to_next_proposal(tmp_path):
    runner = _start_with_topic(tmp_path, "signal scan")
    runner.next_prompt()
    # Edit cadence inline: "édite: cadence hebdo"
    action = runner.on_answer("édite: passe la cadence à weekly")
    assert action == Action.EDIT
    prompt = runner.next_prompt()
    assert prompt is not None
    assert "weekly" in prompt or "hebdo" in prompt.lower()


def test_refine_generates_different_proposal(tmp_path):
    runner = _start_with_topic(tmp_path, "signal scan")
    p1 = runner.next_prompt()
    action = runner.on_answer("raffine")
    assert action == Action.REFINE
    p2 = runner.next_prompt()
    assert p2 is not None
    # The proposal text changed (different cadence or different description)
    assert p1 != p2


# ----- 5 UX actions: disable, change cadence, change layer -----


def test_disable_mission_command_removes_validated_mission(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan, draft posts")
    # Validate first mission
    runner.next_prompt()
    runner.on_answer("approuve")
    first_id = runner._sub_validated[0]["id"]
    # Disable it
    action = runner.on_answer(f"désactive mission {first_id}")
    # The mission file must be gone and not in sub_validated anymore
    mission_path = tmp_path / "missions" / f"{first_id}.yaml"
    assert not mission_path.exists()
    remaining_ids = {e["id"] for e in runner._sub_validated}
    assert first_id not in remaining_ids


def test_change_cadence_command_updates_mission(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan")
    runner.next_prompt()
    runner.on_answer("approuve")
    mission_id = runner._sub_validated[0]["id"]
    # Change cadence
    runner.on_answer(f"change la cadence de {mission_id} à weekly")
    # File must now have cadence: weekly
    body = yaml.safe_load(
        (tmp_path / "missions" / f"{mission_id}.yaml").read_text(encoding="utf-8")
    )
    assert body["cadence"] == "weekly"


def test_change_layer_command_updates_mission(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan")
    runner.next_prompt()
    runner.on_answer("approuve")
    mission_id = runner._sub_validated[0]["id"]
    runner.on_answer(f"change le layer de {mission_id} à 2")
    body = yaml.safe_load(
        (tmp_path / "missions" / f"{mission_id}.yaml").read_text(encoding="utf-8")
    )
    assert body["layer"] == 2


# ----- closing the step -----


def test_after_all_topics_validated_asks_for_more(tmp_path):
    runner = _start_with_topic(tmp_path, "signal scan")
    runner.next_prompt()
    runner.on_answer("approuve")
    # All proposed topics validated → next prompt should ask "more?"
    prompt = runner.next_prompt()
    assert prompt is not None
    assert ("autres" in prompt.lower() or "plus" in prompt.lower()
            or "more" in prompt.lower() or "encore" in prompt.lower())


def test_is_done_only_when_at_least_one_validated_and_no_more(tmp_path):
    runner = _start_with_topic(tmp_path, "signal scan")
    assert runner.is_done() is False
    runner.next_prompt()
    runner.on_answer("approuve")
    # Not done yet — operator hasn't said "no more".
    assert runner.is_done() is False
    # Operator says no more.
    runner.next_prompt()
    runner.on_answer("non, on passe")
    assert runner.is_done() is True


def test_idempotent_on_resume_after_partial_validation(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner1 = get_runner("missions")
    runner1.start(state, draft)
    runner1.on_answer("signal scan, draft posts")
    runner1.next_prompt()
    runner1.on_answer("approuve")
    # Resume in a fresh runner.
    runner2 = get_runner("missions")
    runner2.start(state, draft)
    # Should NOT re-prompt for topics, should resume on mission #2.
    p = runner2.next_prompt()
    assert p is not None
    # Already-validated mission is preserved.
    assert len(runner2._sub_validated) >= 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
