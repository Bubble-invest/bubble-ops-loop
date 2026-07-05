"""Tests for #442 — phantom empty draft cards in the cockpit.

Board #442: two empty draft cards kept reappearing in the cockpit "décisions
à prendre" view — draft-linkedin_sage_batch-20260621-170320 and
draft-research_draft-20260620-180410 (kind: draft, created_by:
materialize_due_missions, EMPTY). They were archived to
queues/gates/.processed/ and pushed, yet the underlying mechanism that
created them in the first place was still live for any dedicated-prompt
mission targeting a queue OTHER than the literal "queues/gates" string.

Root cause: `materialize_due_missions_for_tick`'s bare-stub write (the
id/mission_id/kind/created_at/created_by descriptor with NO payload) is only
guarded against dedicated-prompt missions when `output_queue` is exactly
"queues/gates" (fix #302). `linkedin_sage_batch` and `research_draft` are
BOTH dedicated-prompt missions (they own a `missions/<id>/PROMPT.md` — see
dispatch_helpers.py's `_mission_authors_own_marker` docstring, which names
them explicitly). Their real card is always hand-authored by the subagent
that runs that prompt; the materializer's bare stub is only ever meant to be
a placeholder the subagent overwrites. When the mission's run yields no
content (the upstream funnel is dry), nothing overwrites the stub — it is
left behind as a phantom, empty "decision" card forever (until manually
archived, and even then the underlying gap meant a future dry run of the
same dedicated-prompt mission could produce another one).

Fix (this file's failing-before-passing-after test): extend the #302
"don't write a bare stub" guard to ANY dedicated-prompt mission, regardless
of its output_queue — not just the literal "queues/gates" path.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    materialize_due_missions_for_tick,
    read_last_run,
)

# L2-eligible anchor (Paris CEST = UTC+2; 12:30 Paris = 10:30 UTC).
_NOW = datetime(2026, 6, 21, 10, 30, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _make_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _today_dir(repo: Path) -> Path:
    td = repo / "outputs" / _TODAY
    td.mkdir(parents=True, exist_ok=True)
    return td


def _add_prompt(repo: Path, mid: str) -> None:
    """Give a mission a dedicated prompt — its subagent authors the real
    card itself; the materializer must never stub one out on its behalf."""
    pdir = repo / "missions" / mid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "PROMPT.md").write_text("# dedicated prompt\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 (primary #442 fix): dedicated-prompt mission targeting a NON-gates
# queue must not get a bare stub either — mirrors the real linkedin_sage_batch
# / research_draft repro (their output_queue is queues/gates in production,
# but the guard must generalise: it is the DEDICATED-PROMPT property that
# matters, not the literal queue path).
# ---------------------------------------------------------------------------

def test_dedicated_prompt_mission_non_gates_queue_produces_no_stub(tmp_path: Path):
    """A dedicated-prompt mission whose output_queue is NOT 'queues/gates'
    must still be suppressed — the #302 guard was scoped only to the literal
    'queues/gates' string, leaving every other queue unguarded for
    dedicated-prompt missions."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "research_draft",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/drafts/",
            "creates": ["draft"],
        }
    ])
    _add_prompt(repo, "research_draft")
    today_dir = _today_dir(repo)
    drafts_dir = repo / "queues" / "drafts"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    assert created == [], (
        "materialize_due_missions_for_tick must return [] for a dedicated-prompt "
        "mission — no bare/empty stub should ever be created on its behalf"
    )
    if drafts_dir.exists():
        yaml_files = [f for f in drafts_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == [], (
            f"Expected no phantom draft stub in queues/drafts/, found: {yaml_files}"
        )

    # #428 invariant: a dedicated-prompt mission must NOT be pre-stamped by the
    # materializer — only its own subagent run (STEP 0) may stamp .last-run.
    marker = read_last_run(today_dir / "missions" / "research_draft")
    assert marker is None, (
        "dedicated-prompt mission must not be pre-stamped when its stub is "
        "suppressed — otherwise the real run would be silently skipped (#428)"
    )


def test_dedicated_prompt_mission_queues_gates_still_suppressed(tmp_path: Path):
    """Regression: the original #302 queues/gates path (e.g. linkedin_sage_batch)
    must remain suppressed after generalising the guard to #442."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "linkedin_sage_batch",
            "layer": 3,
            "cadence": "weekly",
            "day": "sunday",
            "time": "18:30",
            "output_queue": "queues/gates/",
            "creates": ["draft"],
        }
    ])
    _add_prompt(repo, "linkedin_sage_batch")
    today_dir = _today_dir(repo)
    gates_dir = repo / "queues" / "gates"

    # Use a Sunday 18:30 Paris anchor so the weekly cadence is due.
    sunday = datetime(2026, 6, 21, 16, 30, 0, tzinfo=timezone.utc)  # Sun 18:30 CEST
    created = materialize_due_missions_for_tick(repo, today_dir, sunday)

    assert created == []
    if gates_dir.exists():
        yaml_files = [f for f in gates_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == []


# ---------------------------------------------------------------------------
# Test 2: regression — a SHIM-RESOLVED mission (no dedicated PROMPT.md) on a
# non-gates queue must still be materialised exactly as before (#302's
# original non-gates regression guard, unaffected by the #442 change).
# ---------------------------------------------------------------------------

def test_shim_resolved_non_gates_mission_still_materialises(tmp_path: Path):
    """A due mission WITHOUT a dedicated prompt must still produce its bare
    descriptor stub on a non-gates queue — that stub IS the real hand-off for
    shim-resolved missions (unchanged by the #442 fix)."""
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
    # NO _add_prompt — shim-resolved.
    today_dir = _today_dir(repo)
    research_dir = repo / "queues" / "research"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    assert len(created) == 1
    assert created[0]["mission_id"] == "market_research"
    yaml_files = [f for f in research_dir.iterdir()
                  if f.is_file() and not f.name.startswith(".")]
    assert len(yaml_files) == 1

    marker = read_last_run(today_dir / "missions" / "market_research")
    assert marker is not None, (
        "shim-resolved mission must still be stamped — its only per-mission "
        "idempotence source"
    )
