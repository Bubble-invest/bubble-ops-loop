"""Tests for #442 — phantom empty draft cards in the cockpit.

Board #442: two empty draft cards kept reappearing in the cockpit "décisions
à prendre" view (which reads queues/gates/*.yaml):
  - draft-research_draft-20260620-180410
  - draft-linkedin_sage_batch-20260621-170320
both kind=draft, created_by=materialize_due_missions, EMPTY.

GROUND TRUTH (verified against the LIVE content dept.yaml, 2026-07-05):
  - research_draft      → output_queue=queues/gates, SHIM-resolved
                          (NO missions/research_draft/PROMPT.md).
  - linkedin_sage_batch → output_queue=queues/gates, DEDICATED-prompt
                          (has missions/linkedin_sage_batch/PROMPT.md).

Because BOTH target queues/gates, the pre-existing #302 guard
(`oq.rstrip("/") == "queues/gates"`, which runs BEFORE the #442 guard) already
suppresses the bare stub for BOTH of them — independent of dedicated/shim. The
two board cards were written BEFORE #302 landed (2026-06-26); they were archived
to queues/gates/.processed/ and are also excluded from the view now (console
.processed exclusion, tested in console/tests/test_dept_queue_view.py). So the
root mechanism for the board-#442 cards is already closed by #302.

What THIS PR's dispatch change adds is a DEFENSE-IN-DEPTH generalization: a
dedicated-prompt mission must never receive a bare materializer stub on ANY
queue — not just queues/gates. #302 only covers the literal queues/gates path;
a dedicated-prompt mission on some OTHER queue (queues/drafts, queues/research)
would still get a phantom stub on a dry run. No LIVE mission is in that shape
today, so it is a preventative invariant, not a repro fix.

Test coverage below:
  1. GENERAL invariant (the #442 guard's distinct effect): a dedicated-prompt
     mission on a NON-gates queue → no stub. FAILS on main (stub written),
     PASSES with the guard.
  2. Reality repro: BOTH board-#442 missions in their REAL shape (queues/gates:
     research_draft shim, linkedin_sage_batch dedicated) → no stub. Passes via
     the pre-existing #302 guard; documents that both are covered today.
  3. Regression: #302 queues/gates suppression still holds.
  4. Regression: a SHIM-resolved mission on a NON-gates queue still materializes
     its legitimate hand-off stub (that stub IS the funnel item downstream layers
     consume — e.g. content_daily_rotation's idea_item on queues/research).
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
    card itself; the materializer must never stub one out on its behalf.

    This mirrors reality ONLY for missions that actually own a
    missions/<id>/PROMPT.md on the live VPS (e.g. linkedin_sage_batch). It must
    NOT be called for shim-resolved missions like research_draft, which have no
    such directory — doing so would test a fiction.
    """
    pdir = repo / "missions" / mid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "PROMPT.md").write_text("# dedicated prompt\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — the #442 guard's DISTINCT effect: a DEDICATED-PROMPT mission on a
# NON-gates queue must not get a bare stub either. Uses linkedin_sage_batch, a
# REAL dedicated-prompt mission (it owns missions/linkedin_sage_batch/PROMPT.md
# on the live VPS), placed on queues/drafts to isolate this guard from the #302
# queues/gates guard. This is the case #302 does NOT reach.
#
# FAIL-BEFORE / PASS-AFTER: on main (no #442 guard) this writes a phantom
# draft stub to queues/drafts/; with the guard, `created == []`.
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
            "id": "linkedin_sage_batch",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/drafts/",
            "creates": ["draft"],
        }
    ])
    _add_prompt(repo, "linkedin_sage_batch")  # REAL dedicated-prompt mission
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
    marker = read_last_run(today_dir / "missions" / "linkedin_sage_batch")
    assert marker is None, (
        "dedicated-prompt mission must not be pre-stamped when its stub is "
        "suppressed — otherwise the real run would be silently skipped (#428)"
    )


# ---------------------------------------------------------------------------
# Test 2 — REALITY REPRO: both board-#442 missions in their exact live shape.
# research_draft (SHIM, queues/gates) and linkedin_sage_batch (DEDICATED,
# queues/gates) must BOTH produce no stub. research_draft is covered by the
# pre-existing #302 queues/gates guard (NOT by the dedicated-prompt #442 guard,
# which never fires for it — it has no PROMPT.md). This test pins the true fix
# path for the actual board cards.
# ---------------------------------------------------------------------------

def test_board_442_missions_real_shape_produce_no_gates_stub(tmp_path: Path):
    """Both offending board-#442 missions target queues/gates in production;
    the #302 guard suppresses their bare stub regardless of dedicated/shim."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {   # SHIM-resolved: NO PROMPT.md — covered by the #302 gates guard.
            "id": "research_draft",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/gates/",
            "creates": ["draft"],
        },
        {   # DEDICATED-prompt: has PROMPT.md — also on queues/gates.
            "id": "linkedin_sage_batch",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/gates/",
            "creates": ["draft"],
        },
    ])
    _add_prompt(repo, "linkedin_sage_batch")  # only THIS one is dedicated
    # research_draft intentionally gets NO prompt — it is shim-resolved.
    today_dir = _today_dir(repo)
    gates_dir = repo / "queues" / "gates"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    assert created == [], (
        "no phantom stub should be created for either board-#442 mission"
    )
    if gates_dir.exists():
        yaml_files = [f for f in gates_dir.iterdir()
                      if f.is_file() and not f.name.startswith(".")]
        assert yaml_files == [], (
            f"Expected no phantom stub in queues/gates/, found: {yaml_files}"
        )

    # research_draft is SHIM-resolved and covered by #302, which DOES stamp
    # .last-run (anti-fire-spin) for suppressed gate missions with no dedicated
    # prompt. linkedin_sage_batch is dedicated → NOT pre-stamped (#428).
    assert read_last_run(today_dir / "missions" / "research_draft") is not None, (
        "shim-resolved gate mission must be stamped by #302 (anti-fire-spin)"
    )
    assert read_last_run(today_dir / "missions" / "linkedin_sage_batch") is None, (
        "dedicated-prompt mission must not be pre-stamped (#428)"
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
# Test — regression — a SHIM-RESOLVED mission (no dedicated PROMPT.md) on a
# non-gates queue must still be materialised exactly as before (#302's
# original non-gates regression guard, unaffected by the #442 change). This is
# the shape of the real content_daily_rotation mission (shim, queues/research):
# its bare descriptor IS the legitimate funnel hand-off downstream layers read.
# ---------------------------------------------------------------------------

def test_shim_resolved_non_gates_mission_still_materialises(tmp_path: Path):
    """A due mission WITHOUT a dedicated prompt must still produce its bare
    descriptor stub on a non-gates queue — that stub IS the real hand-off for
    shim-resolved missions (unchanged by the #442 fix)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/research/",
            "creates": ["idea_item"],
        }
    ])
    # NO _add_prompt — shim-resolved (matches the live content_daily_rotation).
    today_dir = _today_dir(repo)
    research_dir = repo / "queues" / "research"

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    assert len(created) == 1
    assert created[0]["mission_id"] == "content_daily_rotation"
    yaml_files = [f for f in research_dir.iterdir()
                  if f.is_file() and not f.name.startswith(".")]
    assert len(yaml_files) == 1

    marker = read_last_run(today_dir / "missions" / "content_daily_rotation")
    assert marker is not None, (
        "shim-resolved mission must still be stamped — its only per-mission "
        "idempotence source"
    )
