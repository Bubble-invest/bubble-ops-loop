"""Regression test for board #1051 — an L1 gather mission references a Skill
whose SKILL.md declares `disable-model-invocation`, so a stateless L1
subagent cannot invoke it (Skill tool refuses), and the mission silently
writes an empty `signal_status: skill_blocked` artifact every tick.

`scripts.lib.mission_doctor.find_uninvokable_skill_missions` is the
framework's build/CI-time detector for the whole class: it surfaces the
mission→skill edge so a dept ships the fix (re-scope the mission, or drop
the flag from the skill) instead of failing once-per-tick at runtime.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.mission_doctor import find_uninvokable_skill_missions  # noqa: E402


def _write_dept(repo: Path, missions: list) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _write_skill(repo: Path, name: str, frontmatter: dict) -> None:
    d = repo / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False)
    (d / "SKILL.md").write_text(f"---\n{fm}---\n\n# {name}\n\nbody\n", encoding="utf-8")


def test_flags_disable_model_invocation_skill_on_l1_mission(tmp_path: Path):
    """The exact #1051 shape: gather_internal_work (L1) references
    research-internal-work whose frontmatter sets disable-model-invocation."""
    repo = tmp_path / "content"
    _write_dept(repo, [{
        "id": "gather_internal_work",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "output_queue": "queues/research/",
        "creates": ["research_item"],
        "input_sources": ["research-internal-work"],
    }])
    _write_skill(repo, "research-internal-work",
                 {"name": "research-internal-work",
                  "disable-model-invocation": True})

    hits = find_uninvokable_skill_missions(repo)
    assert len(hits) == 1, hits
    assert hits[0]["mission_id"] == "gather_internal_work"
    assert hits[0]["skill"] == "research-internal-work"
    assert hits[0]["skill_path"] == "skills/research-internal-work/SKILL.md"
    assert hits[0]["layer"] == "1"


def test_underscored_input_source_key_still_resolves(tmp_path: Path):
    """input_sources may name the skill with underscores; the detector
    hyphen-normalises to match the on-disk dashed skill dir."""
    repo = tmp_path / "content"
    _write_dept(repo, [{
        "id": "gather_internal_work", "layer": 1, "cadence": "daily",
        "time": "07:00", "output_queue": "queues/research/",
        "creates": ["research_item"],
        "input_sources": ["research_internal_work"],  # underscored
    }])
    _write_skill(repo, "research-internal-work",
                 {"disable-model-invocation": True})

    hits = find_uninvokable_skill_missions(repo)
    assert [h["skill"] for h in hits] == ["research-internal-work"]


def test_description_mentioned_skill_is_detected(tmp_path: Path):
    """A skill named only in the mission's free-text description is an edge
    too (mirrors the cockpit resolver's description-mention convention)."""
    repo = tmp_path / "content"
    _write_dept(repo, [{
        "id": "gather_internal_work", "layer": 1, "cadence": "daily",
        "time": "07:00", "output_queue": "queues/research/",
        "creates": ["research_item"],
        "description": "Pull internal work via the research-internal-work skill.",
    }])
    _write_skill(repo, "research-internal-work",
                 {"disable-model-invocation": "true"})  # string form

    hits = find_uninvokable_skill_missions(repo)
    assert [h["mission_id"] for h in hits] == ["gather_internal_work"]


def test_invocable_skill_is_not_flagged(tmp_path: Path):
    """A normally-invocable skill (no disable flag, or explicitly false) is
    clean — the detector must not raise a false positive."""
    repo = tmp_path / "content"
    _write_dept(repo, [{
        "id": "gather_x_timeline", "layer": 1, "cadence": "daily",
        "time": "07:00", "output_queue": "queues/research/",
        "creates": ["research_item"],
        "input_sources": ["gather-x-timeline"],
    }])
    _write_skill(repo, "gather-x-timeline", {"name": "gather-x-timeline"})
    assert find_uninvokable_skill_missions(repo) == []

    # And the explicit-false form is likewise clean.
    _write_skill(repo, "other-skill", {"disable-model-invocation": False})
    assert find_uninvokable_skill_missions(repo) == []


def test_unreferenced_disabled_skill_does_not_trip(tmp_path: Path):
    """A disable-model-invocation skill that NO mission references is not a
    mission defect — only an actual mission→skill edge counts."""
    repo = tmp_path / "content"
    _write_dept(repo, [{
        "id": "gather_x_timeline", "layer": 1, "cadence": "daily",
        "time": "07:00", "output_queue": "queues/research/",
        "creates": ["research_item"],
    }])
    _write_skill(repo, "research-internal-work",
                 {"disable-model-invocation": True})
    assert find_uninvokable_skill_missions(repo) == []


def test_missing_dept_or_skills_is_empty(tmp_path: Path):
    """No dept.yaml / no skills dir → nothing to lint, no crash."""
    assert find_uninvokable_skill_missions(tmp_path / "nope") == []
    repo = tmp_path / "bare"
    _write_dept(repo, [{"id": "m", "layer": 1, "cadence": "daily", "time": "07:00"}])
    assert find_uninvokable_skill_missions(repo) == []
