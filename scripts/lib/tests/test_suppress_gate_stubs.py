"""
test_suppress_gate_stubs.py — unit tests for fix #302.

Verifies that ``materialize_due_missions_for_tick`` never writes a bare
descriptor stub into ``queues/gates/`` (which would create phantom ghost
decision cards), while still:

  1. Stamping the mission's ``.last-run`` so the mission doesn't fire-spin.
  2. Writing the descriptor file normally for any non-gates output_queue.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    materialize_due_missions_for_tick,
    read_last_run,
)

# ---------------------------------------------------------------------------
# Time anchor — L2-eligible (Paris CEST = UTC+2; 12:30 Paris = 10:30 UTC)
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 6, 26, 10, 30, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _make_dept_yaml(tmp_path: Path, missions: list[dict]) -> None:
    """Write a minimal dept.yaml into tmp_path."""
    dept = {"recurring_missions": missions}
    (tmp_path / "dept.yaml").write_text(
        yaml.dump(dept, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _today_dir(repo: Path) -> Path:
    td = repo / "outputs" / _TODAY
    td.mkdir(parents=True, exist_ok=True)
    return td


# ---------------------------------------------------------------------------
# Test 1: gates output_queue → no file written, .last-run IS stamped
# ---------------------------------------------------------------------------

def test_gates_output_queue_produces_no_file(tmp_path: Path):
    """A due mission with output_queue queues/gates/ must not create a stub file."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "investment_case",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/gates/",
            "creates": ["investment_case_gate"],
        }
    ])

    today_dir = _today_dir(repo)
    # Ensure gates queue dir does NOT pre-exist (so mkdir side-effect is fine,
    # but we'll check it's empty after the call).
    gates_dir = repo / "queues" / "gates"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    # No items returned in `created`.
    assert created == [], (
        "materialize_due_missions_for_tick must return [] for a gates mission "
        "(no bare stub should be created)"
    )

    # No YAML files written into queues/gates/.
    if gates_dir.exists():
        yaml_files = [f for f in gates_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == [], (
            f"Expected no stub files in queues/gates/ but found: {yaml_files}"
        )

    # .last-run IS stamped (prevents fire-spin on next tick).
    last_run = read_last_run(today_dir / "missions" / "investment_case")
    assert last_run is not None, (
        ".last-run must be stamped even when the gate stub is suppressed "
        "(otherwise the mission fire-spins every tick)"
    )


# Variant: output_queue without trailing slash must also be suppressed.
def test_gates_output_queue_no_trailing_slash(tmp_path: Path):
    """queues/gates (no trailing slash) is also suppressed."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "prospect_dm_gate",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/gates",   # no trailing slash
            "creates": ["dm_approval"],
        }
    ])

    today_dir = _today_dir(repo)
    gates_dir = repo / "queues" / "gates"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    assert created == []
    if gates_dir.exists():
        yaml_files = [f for f in gates_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == []

    last_run = read_last_run(today_dir / "missions" / "prospect_dm_gate")
    assert last_run is not None, ".last-run must be stamped for no-trailing-slash variant"


# ---------------------------------------------------------------------------
# Test 2: regression — non-gates output_queue still materialises normally
# ---------------------------------------------------------------------------

def test_non_gates_output_queue_still_materialises(tmp_path: Path):
    """A due mission with output_queue queues/research/ must still produce a file."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "market_research",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/research/",
            "creates": ["market_brief"],
        }
    ])

    today_dir = _today_dir(repo)
    research_dir = repo / "queues" / "research"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    # One item returned.
    assert len(created) == 1, (
        "A non-gates mission must produce exactly one descriptor in `created`"
    )
    assert created[0]["mission_id"] == "market_research"
    assert created[0]["output_queue"] == "queues/research/"
    assert created[0]["kind"] == "market_brief"

    # YAML file written into queues/research/.
    yaml_files = [f for f in research_dir.iterdir()
                  if f.is_file() and not f.name.startswith(".")]
    assert len(yaml_files) == 1, (
        f"Expected exactly 1 stub file in queues/research/, got: {yaml_files}"
    )

    # File content has expected fields (bare descriptor shape).
    data = yaml.safe_load(yaml_files[0].read_text(encoding="utf-8")) or {}
    assert data.get("mission_id") == "market_research"
    assert data.get("kind") == "market_brief"
    assert data.get("created_by") == "materialize_due_missions"

    # .last-run is also stamped.
    last_run = read_last_run(today_dir / "missions" / "market_research")
    assert last_run is not None


# ---------------------------------------------------------------------------
# Test 3: mixed missions — gates suppressed, research materialised, same tick
# ---------------------------------------------------------------------------

def test_mixed_missions_gates_suppressed_research_materialised(tmp_path: Path):
    """Two missions: one gates, one research — gates suppressed, research created."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "signal_gate",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/gates/",
            "creates": ["signal_approval"],
        },
        {
            "id": "prospect_research",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/research/",
            "creates": ["prospect_brief"],
        },
    ])

    today_dir = _today_dir(repo)
    gates_dir = repo / "queues" / "gates"
    research_dir = repo / "queues" / "research"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    # Only the research mission appears in `created`.
    mission_ids = {c["mission_id"] for c in created}
    assert "signal_gate" not in mission_ids, (
        "signal_gate (queues/gates/) must not appear in created[]"
    )
    assert "prospect_research" in mission_ids, (
        "prospect_research (queues/research/) must appear in created[]"
    )

    # No files in gates_dir.
    if gates_dir.exists():
        yaml_files = [f for f in gates_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == []

    # One file in research_dir.
    yaml_files = [f for f in research_dir.iterdir()
                  if f.is_file() and not f.name.startswith(".")]
    assert len(yaml_files) == 1

    # Both missions have .last-run stamped.
    assert read_last_run(today_dir / "missions" / "signal_gate") is not None
    assert read_last_run(today_dir / "missions" / "prospect_research") is not None
