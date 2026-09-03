"""Regression test for board #1022 — L1 gather prompts wrote a bespoke
`.gather_<id>.done` sentinel instead of the `outputs/<today>/missions/<id>/
.last-run` marker the dispatcher actually reads, so after all four gathers
completed, select_due_missions STILL returned them as due → next-tick
re-dispatch (fire-spin / wasted Opus spend).

Two halves of the fix, both framework-level and both asserted here:

  1. The SCAFFOLD contract (mission_scaffold.render_mission_prompt_md): every
     newly-scaffolded per-mission prompt now carries the explicit STEP that
     stamps the per-mission `.last-run` the dispatcher reads — aligning new
     gather-style missions with the contract by construction.

  2. The DISPATCHER truth (dispatch_helpers._mission_last_fired): a bespoke
     `.done` sentinel does NOT count as "handled", while the correct
     `outputs/<today>/missions/<id>/.last-run` does — this is WHY the prompt
     must stamp the latter, and it composes with the #1080 output-truth gate
     (the L1 marker is only trusted once the L1 layer produced real output).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.mission_scaffold import render_mission_prompt_md  # noqa: E402
from scripts.lib.dispatch_helpers import (  # noqa: E402
    _mission_authors_own_marker,
    _mission_last_fired,
    write_last_run,
)

_GATHER = {
    "id": "gather_internal_work",
    "layer": 1,
    "cadence": "daily",
    "time": "07:00",
    "output_queue": "queues/research/",
    "creates": ["research_item"],
    "input_sources": ["research-internal-work"],
}


# --- Half 1: the scaffold contract ------------------------------------------

def test_scaffolded_prompt_stamps_the_marker_the_dispatcher_reads():
    """render_mission_prompt_md must instruct the subagent to stamp
    `outputs/<today>/missions/<id>/.last-run` via write_last_run — the exact
    path dispatch_helpers._mission_handled_marker reads."""
    text = render_mission_prompt_md(_GATHER, slug="content", display_name="Miranda")
    assert "write_last_run" in text
    assert ".last-run" in text
    # The exact stamp call, on the per-mission dir the dispatcher reads —
    # NOT a layer dir and NOT a bespoke sentinel.
    assert 'write_last_run(Path("outputs/<today>/missions/gather_internal_work"))' in text


def test_dedicated_prompt_means_materializer_defers_to_the_mission(tmp_path: Path):
    """Sanity-check the premise: a mission WITH a dedicated PROMPT.md is a
    dedicated-prompt mission (_mission_authors_own_marker True), so the
    materializer will NOT stamp a marker for it — which is exactly why the
    prompt itself must stamp `.last-run` (the #1022 root cause)."""
    (tmp_path / "missions" / "gather_internal_work").mkdir(parents=True)
    (tmp_path / "missions" / "gather_internal_work" / "PROMPT.md").write_text(
        render_mission_prompt_md(_GATHER, "content", "Miranda"), encoding="utf-8"
    )
    assert _mission_authors_own_marker(tmp_path, _GATHER) is True


# --- Half 2: the dispatcher truth (composes with #1080) ---------------------

_NOW = datetime(2026, 8, 23, 6, 30, 0, tzinfo=timezone.utc)  # 08:30 Paris (CEST)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _ctx(repo: Path) -> dict:
    return {"today_dir": str(repo / "outputs" / _TODAY), "now_utc": _NOW}


def _add_l1_output_evidence(repo: Path) -> None:
    """#1080: the L1 per-mission marker is only trusted once the L1 layer has
    produced real output in outputs/<today>/1/ (satisfied live by the morning
    briefing mission running alongside the gathers)."""
    l1 = repo / "outputs" / _TODAY / "1"
    l1.mkdir(parents=True, exist_ok=True)
    (l1 / "morning_briefing.md").write_text("moved: nothing\n", encoding="utf-8")


def test_bespoke_done_sentinel_does_not_count_as_handled(tmp_path: Path):
    """THE #1022 BUG: a gather subagent that wrote only
    outputs/<today>/1/.gather_internal_work.done leaves the dispatcher's
    per-mission marker absent → _mission_last_fired returns None → the mission
    is re-selected as due (fire-spin)."""
    repo = tmp_path
    _add_l1_output_evidence(repo)
    # The OLD, wrong sentinel — under the LAYER dir, wrong name.
    (repo / "outputs" / _TODAY / "1" / ".gather_internal_work.done").write_text(
        _NOW.isoformat(), encoding="utf-8"
    )
    assert _mission_last_fired(_ctx(repo), _GATHER) is None, (
        "a bespoke .done sentinel is not the marker the dispatcher reads — the "
        "mission still looks un-run and would re-fire (#1022)"
    )


def test_correct_last_run_marker_is_honoured(tmp_path: Path):
    """THE #1022 FIX: once the prompt stamps
    outputs/<today>/missions/<id>/.last-run (as the scaffold now instructs),
    a PRIOR-tick marker is honoured and the mission is not re-selected."""
    repo = tmp_path
    _add_l1_output_evidence(repo)
    prior = _NOW - timedelta(hours=1)  # a genuine prior-tick completion
    write_last_run(repo / "outputs" / _TODAY / "missions" / "gather_internal_work",
                   when=prior)
    got = _mission_last_fired(_ctx(repo), _GATHER)
    assert got == prior, (
        "the correct per-mission .last-run marker must be recognised as "
        "'handled today' so the mission is not re-dispatched (#1022)"
    )


def test_marker_without_l1_output_is_still_gated_by_1080(tmp_path: Path):
    """Composition guard: even the correct .last-run marker must NOT be
    trusted for L1 while the layer produced NO real output yet (#1080
    output-truth gate) — this cluster's fix must not reintroduce the
    marker-without-work false positive."""
    repo = tmp_path  # deliberately NO l1 output evidence
    prior = _NOW - timedelta(hours=1)
    write_last_run(repo / "outputs" / _TODAY / "missions" / "gather_internal_work",
                   when=prior)
    assert _mission_last_fired(_ctx(repo), _GATHER) is None, (
        "#1080: an L1 per-mission marker with no outputs/<today>/1/ artifact "
        "must not count as fired — the mission stays recoverable"
    )
