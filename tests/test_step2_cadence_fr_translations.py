"""
test_step2_cadence_fr_translations.py — Sprint correctif Fix 5.

QA-E2E 2026-05-20 finding: operator types
  "change la cadence de signal_scan à hebdo"
and `_change_field()` writes `cadence: hebdo` which the schema (regex
`^(daily|weekly|hourly|every_\\d+h|every_\\d+m|cron:.+)$`) rejects.
The tester returns false; the operator gets no feedback.

Fix 5: translate common French cadence shorthands BEFORE validating
(hebdo -> weekly, quotidien -> daily, horaire -> hourly). If the
translated value is STILL invalid (e.g. "très souvent"), set
`_last_rejection_reason` with a Bureau-de-Cadre French explanation
(mirroring Fix 4) so the operator sees WHY their command was ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
sys.path.insert(0, str(SKILL_ROOT))

from skill_lib.step_runners import get_runner


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Configuring",
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
            "level": "ops",
            "status": "onboarding",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "owner": "joris",
            "forbidden": ["publier sans validation"],
        },
    }, sort_keys=False), encoding="utf-8")
    return draft


def _seed_mission(tmp_path: Path, mission_id: str = "signal_scan",
                  cadence: str = "daily") -> None:
    """Write a mission YAML so _change_field has something to mutate."""
    missions_dir = tmp_path / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)
    mission = {
        "id": mission_id,
        "layer": 1,
        "cadence": cadence,
        "time": "06:00",
        "description": "Scanner les signaux du jour.",
        "output_queue": "queues/research/",
        "creates": ["signal_task"],
    }
    (missions_dir / f"{mission_id}.yaml").write_text(
        yaml.safe_dump(mission, sort_keys=False), encoding="utf-8",
    )


def _make_runner_with_mission(tmp_path: Path, cadence: str = "daily"):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    _seed_mission(tmp_path, cadence=cadence)
    runner = get_runner("missions")
    runner.start(state, draft)
    # Hydrate sub_validated so the sync to dept.yaml.draft works.
    runner._sub_validated = [{
        "id": "signal_scan",
        "type": "mission_draft",
        "validated_at": "2026-05-21T08:00:01Z",
    }]
    runner._sync_recurring_missions_in_draft()
    return runner, draft


# ----- the FR translation tests -----


def test_change_cadence_hebdo_translates_to_weekly(tmp_path):
    runner, draft = _make_runner_with_mission(tmp_path)
    ok = runner._change_field("signal_scan", "cadence", "hebdo")
    assert ok is True, "hebdo must be accepted (translated to weekly)"
    mission = yaml.safe_load(
        (tmp_path / "missions" / "signal_scan.yaml").read_text(encoding="utf-8"))
    assert mission["cadence"] == "weekly"


def test_change_cadence_quotidien_translates_to_daily(tmp_path):
    runner, _ = _make_runner_with_mission(tmp_path, cadence="weekly")
    ok = runner._change_field("signal_scan", "cadence", "quotidien")
    assert ok is True
    mission = yaml.safe_load(
        (tmp_path / "missions" / "signal_scan.yaml").read_text(encoding="utf-8"))
    assert mission["cadence"] == "daily"


def test_change_cadence_horaire_translates_to_hourly(tmp_path):
    runner, _ = _make_runner_with_mission(tmp_path)
    ok = runner._change_field("signal_scan", "cadence", "horaire")
    assert ok is True
    mission = yaml.safe_load(
        (tmp_path / "missions" / "signal_scan.yaml").read_text(encoding="utf-8"))
    assert mission["cadence"] == "hourly"


def test_global_command_change_cadence_hebdo_works(tmp_path):
    """End-to-end: the imperative UX command, not just the internal helper."""
    runner, _ = _make_runner_with_mission(tmp_path)
    handled = runner._handle_global_commands(
        "change la cadence de signal_scan à hebdo"
    )
    assert handled is True
    mission = yaml.safe_load(
        (tmp_path / "missions" / "signal_scan.yaml").read_text(encoding="utf-8"))
    assert mission["cadence"] == "weekly"


def test_unrecognised_cadence_sets_visible_rejection_reason(tmp_path):
    """When the translated cadence is still invalid (e.g. 'tres souvent'),
    the runner sets `_last_rejection_reason` with a French explanation.
    """
    runner, _ = _make_runner_with_mission(tmp_path)
    ok = runner._change_field("signal_scan", "cadence", "très souvent")
    assert ok is False
    reason = getattr(runner, "_last_rejection_reason", None)
    assert reason is not None, (
        "Fix 5: runner must expose `_last_rejection_reason` after an "
        "unrecognised cadence command."
    )
    rl = reason.lower()
    # Must cite the 4 canonical cadence patterns.
    assert "daily" in rl or "weekly" in rl or "hourly" in rl
    # Must mention the rejected value.
    assert "très souvent" in reason or "souvent" in rl


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
