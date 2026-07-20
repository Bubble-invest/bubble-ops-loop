"""test_hatch_io_failure_guard.py — card #707.

Guards the LIVE conversational dept-hatch path (`MissionsRunner.
_commit_current_mission`) against a half-created dept when
`mission_scaffold.scaffold_mission_pieces` raises mid-emission.

Failure shape (pre-fix): `_commit_current_mission` writes
`missions/<id>.yaml` (mission DECLARED), then calls
`scaffold_mission_pieces(...)` UNGUARDED. If that raises (OSError /
PermissionError / disk-full), the exception propagates straight out of
`on_answer()` — violating step_runners/base.py's own design rule
("on_answer() never raises ... returns Action.CONTINUE") — and nothing
tells the operator what broke. The mission is left DECLARED but
UNFURNISHED (no PROMPT.md / skill stub / etc.) with zero visible
signal.

This test pins the fixed behavior:
  1. The operator SEES a clear, named error (mission id + failing step)
     via the same `_last_rejection_reason` -> next_prompt() channel the
     codebase already uses for other refusals (see `_change_field`'s
     "Sprint correctif Fix 5" comments).
  2. `on_answer()` itself never raises (preserves the StepRunner
     contract).
  3. State is recoverable: the mission yaml written in step 1 is NOT
     deleted (that would lose declared work), and a re-run with a
     working `scaffold_mission_pieces` completes the hatch (mission
     moves into `_sub_validated`, pieces get emitted).
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.missions import MissionsRunner
from skill_lib.step_runners import missions as missions_mod


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


def test_scaffold_io_failure_is_surfaced_not_silent_and_state_recovers(tmp_path, monkeypatch):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)

    calls = {"n": 0}
    real_scaffold = missions_mod.mission_scaffold.scaffold_mission_pieces

    def _flaky_scaffold(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated) while emitting mission pieces")
        return real_scaffold(*args, **kwargs)

    monkeypatch.setattr(
        missions_mod.mission_scaffold, "scaffold_mission_pieces", _flaky_scaffold,
    )

    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal scan, draft posts")

    # Mission #1 commits cleanly (1st scaffold call succeeds).
    runner.next_prompt()
    action1 = runner.on_answer("approuve")
    assert action1 == Action.APPROVE_SUBSTEP
    assert len(runner._sub_validated) == 1
    first_id = runner._sub_validated[0]["id"]
    assert (tmp_path / "missions" / f"{first_id}.yaml").exists()

    # Mission #2's commit hits the flaky scaffold (2nd call) and must
    # NOT raise out of on_answer() (StepRunner contract).
    runner.next_prompt()
    second_mission_id = runner._current_mission["id"]
    try:
        action2 = runner.on_answer("approuve")
    except OSError:
        pytest.fail(
            "on_answer() must never raise (step_runners/base.py contract); "
            "the I/O failure must be caught and surfaced instead."
        )

    # Must NOT silently look like success: it must not have advanced as
    # a normal APPROVE_SUBSTEP (mission #2 isn't actually furnished).
    assert action2 != Action.APPROVE_SUBSTEP
    assert second_mission_id not in {e["id"] for e in runner._sub_validated}

    # (1) Operator SEES a clear, named error mentioning the mission and
    # the failure -- not a silent pass.
    prompt = runner.next_prompt()
    assert prompt is not None
    assert second_mission_id in prompt, (
        "surfaced error must name the mission that failed to hatch"
    )
    lowered = prompt.lower()
    assert any(word in lowered for word in ("erreur", "échoué", "echoue", "échec", "echec")), (
        "surfaced error must clearly say something broke, not just show "
        "the normal mission-proposal card"
    )

    # (2) State is recoverable: the DECLARED mission yaml from step 1 of
    # _commit_current_mission (write-before-scaffold) is still on disk --
    # not deleted, so a retry doesn't lose the already-done work.
    declared_path = tmp_path / "missions" / f"{second_mission_id}.yaml"
    assert declared_path.exists(), (
        "the declared mission yaml must survive a scaffold I/O failure "
        "so a re-run can recover instead of losing work"
    )

    # (3) Re-run with a working scaffold completes the hatch.
    monkeypatch.setattr(
        missions_mod.mission_scaffold, "scaffold_mission_pieces", real_scaffold,
    )
    action_retry = runner.on_answer("approuve")
    assert action_retry == Action.APPROVE_SUBSTEP
    assert second_mission_id in {e["id"] for e in runner._sub_validated}
    # Pieces actually got emitted this time.
    assert (tmp_path / "missions" / second_mission_id / "PROMPT.md").exists()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
