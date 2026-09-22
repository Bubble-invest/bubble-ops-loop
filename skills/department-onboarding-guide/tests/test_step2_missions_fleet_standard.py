"""
test_step2_missions_fleet_standard.py — card #1441.

Pins that the Step-2 missions runner (`MissionsRunner`) ALWAYS appends the
fleet-standard `session_handoff` Layer-4 daily mission (board #1195) to a
newly onboarded dept when the missions step finalizes ("closing" — the
operator says they don't want to add more missions) — even when the
operator never asked for a session_handoff mission themselves. Mirrors
isolation_scaffold.FLEET_STANDARD_AGENTS's "every dept gets this by
default" pattern (tested in test_isolation_scaffold.py), but for a
recurring MISSION rather than a static agent/skill copy.
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.missions import FLEET_STANDARD_MISSIONS


# ----- helpers (mirror test_step2_missions_runner.py) -----


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


def _close_step_with_one_mission(tmp_path: Path, topic: str = "signal scan"):
    """Run the full happy-path conversation for ONE operator-chosen
    mission, then close the step (operator says "non more"). Returns the
    runner post-closing."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer(topic)
    runner.next_prompt()  # renders the mission-proposal card
    action = runner.on_answer("approuve")
    assert action == Action.APPROVE_SUBSTEP
    runner.next_prompt()  # renders "you want more?"
    action = runner.on_answer("non, on passe")
    assert action == Action.DONE
    return runner, state, draft


# ----- the fleet-standard auto-append -----


def test_closing_step_auto_appends_session_handoff_mission(tmp_path):
    """The operator asked for exactly ONE mission of their own
    ("signal scan") and never mentioned session_handoff — the step must
    still carry a session_handoff mission after closing."""
    runner, state, draft = _close_step_with_one_mission(tmp_path)

    dept_root = draft.parent
    mission_path = dept_root / "missions" / "session_handoff.yaml"
    assert mission_path.exists()
    body = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    assert body["id"] == "session_handoff"
    assert body["layer"] == 4
    assert body["cadence"] == "daily"
    for f in ("id", "layer", "cadence", "description", "output_queue", "creates"):
        assert f in body

    # is_done() must be True (>= 1 validated + operator_closed), and both
    # the operator's own mission AND the fleet-standard one must count.
    assert runner.is_done() is True
    ids = {e["id"] for e in runner._sub_validated}
    assert "signal_scan_task" in ids or any("signal" in i for i in ids)
    assert "session_handoff" in ids


def test_session_handoff_mission_id_reflected_in_dept_yaml_draft(tmp_path):
    """dept.yaml.draft::recurring_missions (the top-level sibling of
    `department:`, per the v3 schema layout) must mirror session_handoff
    too — that's how the mission actually gets dispatched (the loader
    reads dept.yaml, not missions/*.yaml directly)."""
    _runner, _state, draft = _close_step_with_one_mission(tmp_path)
    draft_doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    recurring = draft_doc.get("recurring_missions") or []
    ids = {m["id"] for m in recurring}
    assert "session_handoff" in ids
    handoff = next(m for m in recurring if m["id"] == "session_handoff")
    assert handoff["layer"] == 4
    assert handoff["cadence"] == "daily"


def test_session_handoff_prompt_md_scaffolded(tmp_path):
    """The mission-core piece (missions/session_handoff/PROMPT.md) must be
    written with the real fleet-standard prompt body, not a generic
    auto-render — so the cockpit piece view + the Layer-4 subagent get the
    canonical instructions (mirrors agents/ben/missions/session_handoff/
    PROMPT.md, the first hand-added instance)."""
    _runner, _state, draft = _close_step_with_one_mission(tmp_path)
    dept_root = draft.parent
    prompt_path = dept_root / "missions" / "session_handoff" / "PROMPT.md"
    assert prompt_path.exists()
    body = prompt_path.read_text(encoding="utf-8")
    assert "session_handoff" in body
    assert "HANDOFF.md" in body
    assert "stateless" in body.lower()


def test_idempotent_does_not_double_add_on_re_close(tmp_path):
    """Closing twice (e.g. a resumed session that re-answers "non" after a
    crash) must not create a duplicate session_handoff entry."""
    runner, state, draft = _close_step_with_one_mission(tmp_path)
    before = list(runner._sub_validated)
    # Re-invoke the guard directly — simulates a resumed runner re-hitting
    # the closing transition (e.g. on_answer("non") again after a crash
    # recovery, since is_done() short-circuits the public on_answer path
    # once truly closed).
    runner._ensure_fleet_standard_missions()
    after = list(runner._sub_validated)
    assert len([e for e in after if e["id"] == "session_handoff"]) == 1
    assert len(before) == len(after)

    dept_root = draft.parent
    mission_files = list((dept_root / "missions").glob("session_handoff*.yaml"))
    assert len(mission_files) == 1


def test_idempotent_when_session_handoff_already_hand_added(tmp_path):
    """If a prior process/operator already hand-wrote
    missions/session_handoff.yaml (e.g. an interrupted run, or a manual
    retrofit) before the step closes, the guard must adopt it rather than
    re-test/overwrite it, and still count it towards is_done()."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    dept_root = draft.parent
    hand_written = dict(FLEET_STANDARD_MISSIONS["session_handoff"])
    mission_path = dept_root / "missions" / "session_handoff.yaml"
    mission_path.parent.mkdir(parents=True, exist_ok=True)
    mission_path.write_text(yaml.safe_dump(hand_written, sort_keys=False), encoding="utf-8")
    original_mtime = mission_path.stat().st_mtime_ns

    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan")
    runner.next_prompt()
    runner.on_answer("approuve")
    runner.next_prompt()
    action = runner.on_answer("non, on passe")
    assert action == Action.DONE

    # File untouched (never clobbered), but reflected in validated set.
    assert mission_path.stat().st_mtime_ns == original_mtime
    ids = {e["id"] for e in runner._sub_validated}
    assert "session_handoff" in ids
    assert runner.is_done() is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
