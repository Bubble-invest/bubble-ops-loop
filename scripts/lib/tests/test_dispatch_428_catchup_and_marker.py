"""Tests for #428 — newsletter silent-skip fix (ported onto canonical / #375-v3).

Two-part fix in dispatch_helpers.py:

  Fix 1 (materialize_due_missions_for_tick): a mission that authors its OWN
  per-mission `.last-run` marker at STEP 0 (any mission with a dedicated
  `missions/<id>/PROMPT.md`) must NOT be pre-stamped by the materializer at
  DECISION time. Pre-stamping makes the idempotence guard believe the mission
  already ran, so the real subagent run is never dispatched — silently skipping
  weekly/daily dedicated-prompt producers (newsletter Tue/Fri, sage Sun, …).
  Shim-resolved missions (no dedicated PROMPT.md) KEEP the stamp — it is their
  only per-mission idempotence source (anti fire-spin #261/#277).

  Fix 2 (_due_scheduled_catchup_layer + select_due_missions fallback): a narrow
  catch-up safeguard so a scheduled producer whose slot passed while the Mac was
  asleep is not silently skipped for the week. It only acts as a fallback when no
  layer is eligible the normal way — it can never out-rank a real-work layer.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    materialize_due_missions_for_tick,
    select_due_missions,
    _due_scheduled_catchup_layer,
    read_last_run,
    write_last_run,
)

# ---------------------------------------------------------------------------
# Time anchors (June = Paris CEST = UTC+2)
#   L1>=07:00, L2>=12:00, L3>=16:00, L4>=19:00 Paris.
# ---------------------------------------------------------------------------
# A Friday at 18:30 Paris (16:30 UTC) — past a Tue/Fri 18:03 newsletter slot,
# and past the L2 floor, but BEFORE any layer is forced eligible (no research).
_FRI = datetime(2026, 6, 26, 16, 30, 0, tzinfo=timezone.utc)   # Friday 18:30 Paris
_TODAY = _FRI.strftime("%Y-%m-%d")


def _make_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _today_dir(repo: Path, now: datetime = _FRI) -> Path:
    td = repo / "outputs" / now.strftime("%Y-%m-%d")
    td.mkdir(parents=True, exist_ok=True)
    return td


def _add_prompt(repo: Path, mid: str) -> None:
    """Give a mission a dedicated prompt → it authors its own marker at STEP 0."""
    pdir = repo / "missions" / mid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "PROMPT.md").write_text("# dedicated prompt\n", encoding="utf-8")


def _bare_ctx(repo: Path, now: datetime, **overrides) -> dict:
    """Minimal ctx for select_due_missions as a pure function (no materialize)."""
    base = {
        "now_utc": now,
        "today": now.strftime("%Y-%m-%d"),
        "today_dir": str(repo / "outputs" / now.strftime("%Y-%m-%d")),
        "_repo_dir": str(repo),
        "has_research_items": False,
        "has_inbox_decisions": False,
        "has_unconsumed_mgmt_notes": False,
        "layer_1_last_run_today": None,
        "layer_2_last_run_today": None,
        "layer_3_last_run_today": None,
        "layer_4_last_run_today": None,
        "round_counter": {},
        "layer_1_baseline_counter": {},
        "fire_after_rounds": 1,
    }
    base.update(overrides)
    return base


# ===========================================================================
# Fix 1 — materializer must NOT pre-stamp dedicated-prompt missions
# ===========================================================================

def test_dedicated_prompt_mission_not_prestamped(tmp_path: Path):
    """A weekly producer WITH missions/<id>/PROMPT.md must NOT have its
    per-mission .last-run pre-stamped by the materializer (the marker stays
    absent until the real subagent run stamps it at STEP 0)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": ["tuesday", "friday"],
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        }
    ])
    _add_prompt(repo, "newsletter_redaction")
    today_dir = _today_dir(repo)

    materialize_due_missions_for_tick(repo, today_dir, _FRI)

    marker = read_last_run(today_dir / "missions" / "newsletter_redaction")
    assert marker is None, (
        "dedicated-prompt mission must NOT be pre-stamped by the materializer "
        "(otherwise the idempotence guard silently skips the real run)"
    )


def test_shim_resolved_mission_is_still_stamped(tmp_path: Path):
    """A producer WITHOUT a dedicated PROMPT.md (shim-resolved) MUST still be
    stamped by the materializer — its only per-mission idempotence source
    (anti fire-spin #261/#277)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": [],   # pure-report shim → no queue item, stamp via top loop
        }
    ])
    # NO _add_prompt — shim-resolved.
    today_dir = _today_dir(repo)

    materialize_due_missions_for_tick(repo, today_dir, _FRI)

    marker = read_last_run(today_dir / "missions" / "content_daily_rotation")
    assert marker is not None, (
        "shim-resolved mission MUST keep its materializer stamp (anti fire-spin)"
    )


def test_dedicated_prompt_gate_mission_not_prestamped(tmp_path: Path):
    """A dedicated-prompt mission whose output_queue is queues/gates/ (the
    suppressed-stub path) must also NOT be pre-stamped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "linkedin_sage_batch",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:30",
            "day": "friday",
            "output_queue": "queues/gates/",
            "creates": ["sage_gate"],
        }
    ])
    _add_prompt(repo, "linkedin_sage_batch")
    today_dir = _today_dir(repo)

    materialize_due_missions_for_tick(repo, today_dir, _FRI)

    marker = read_last_run(today_dir / "missions" / "linkedin_sage_batch")
    assert marker is None, (
        "dedicated-prompt gate mission must NOT be pre-stamped (the gate-stub "
        "path is also a #428 silent-skip site)"
    )


def test_shim_gate_mission_still_stamped(tmp_path: Path):
    """A gate mission WITHOUT a dedicated prompt is still stamped (suppressed
    stub path keeps anti fire-spin for shim missions)."""
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
        }
    ])
    today_dir = _today_dir(repo)

    materialize_due_missions_for_tick(repo, today_dir, _FRI)

    marker = read_last_run(today_dir / "missions" / "signal_gate")
    assert marker is not None, (
        "shim gate mission must keep its stamp (no dedicated PROMPT.md)"
    )


# ===========================================================================
# Fix 2 — _due_scheduled_catchup_layer + select_due_missions fallback
# ===========================================================================

def test_catchup_returns_layer_for_missed_weekly_producer(tmp_path: Path):
    """A weekly dedicated-prompt producer whose slot passed today and has not
    run → _due_scheduled_catchup_layer returns its layer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": ["tuesday", "friday"],
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        }
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_redaction")
    _today_dir(repo)
    ctx = _bare_ctx(repo, _FRI)

    layer = _due_scheduled_catchup_layer(ctx, missions)
    assert layer == 2, (
        "missed weekly dedicated-prompt producer must wake its own layer (2)"
    )


def test_catchup_none_when_already_ran(tmp_path: Path):
    """If the producer already ran today (a real prior-tick marker), the
    catch-up returns None — it self-terminates."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": ["tuesday", "friday"],
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        }
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_redaction")
    today_dir = _today_dir(repo)
    # A real marker from EARLIER this tick's day (prior tick → < now_utc).
    write_last_run(today_dir / "missions" / "newsletter_redaction",
                   _FRI - timedelta(hours=1))
    ctx = _bare_ctx(repo, _FRI)

    assert _due_scheduled_catchup_layer(ctx, missions) is None, (
        "catch-up must self-terminate once the mission's real run stamped its marker"
    )


def test_catchup_none_for_shim_mission(tmp_path: Path):
    """A producer WITHOUT a dedicated prompt is never caught up (its behaviour
    is left exactly as before — the legacy layer-shim primaries)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "research_draft",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": "friday",
            "output_queue": "queues/research/",
            "creates": [],
        }
    ]
    _make_dept_yaml(repo, missions)
    # NO _add_prompt.
    _today_dir(repo)
    ctx = _bare_ctx(repo, _FRI)

    assert _due_scheduled_catchup_layer(ctx, missions) is None, (
        "catch-up only applies to dedicated-prompt missions"
    )


def test_catchup_none_for_consumer_mission(tmp_path: Path):
    """A consumer mission (has input_queue) is never caught up — it is gated by
    its input queue, not the clock."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_consumer",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": "friday",
            "input_queue": "queues/research/",
            "output_queue": "queues/gates/",
            "creates": [],
        }
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_consumer")
    _today_dir(repo)
    ctx = _bare_ctx(repo, _FRI)

    assert _due_scheduled_catchup_layer(ctx, missions) is None, (
        "consumer missions are gated by their input queue, not catch-up"
    )


def test_catchup_none_before_scheduled_slot(tmp_path: Path):
    """Before the scheduled time, the slot has not passed → no catch-up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": "friday",
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        }
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_redaction")
    # 17:00 Paris = 15:00 UTC — before the 18:03 slot.
    before = datetime(2026, 6, 26, 15, 0, 0, tzinfo=timezone.utc)
    (repo / "outputs" / before.strftime("%Y-%m-%d")).mkdir(parents=True, exist_ok=True)
    ctx = _bare_ctx(repo, before)

    assert _due_scheduled_catchup_layer(ctx, missions) is None, (
        "is_mission_due enforces 'never before the scheduled time'"
    )


def test_select_falls_back_to_catchup_when_no_layer_eligible(tmp_path: Path):
    """select_due_missions returns the missed scheduled producer ONLY via the
    catch-up fallback when no layer is eligible the normal way (heartbeat tick,
    empty research queue)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": ["tuesday", "friday"],
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        }
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_redaction")
    _today_dir(repo)
    # Heartbeat tick: L1 already fired today, no research/decisions/mgmt-notes,
    # and the L1 cycle gate is not met (empty round_counter) → NO layer is
    # eligible the normal way. Only then does the catch-up fallback apply.
    ctx = _bare_ctx(
        repo, _FRI,
        layer_1_last_run_today=_FRI - timedelta(hours=10),
    )

    # Sanity: the normal eligibility path yields no layer for this tick.
    from scripts.lib.dispatch_helpers import _highest_eligible_layer_from_signals, _to_paris
    assert _highest_eligible_layer_from_signals(
        _to_paris(_FRI).time(),
        has_research=False, has_decisions=False, has_mgmt_notes=False,
        l1_fired=True, l2_fired=False, l3_fired=False,
        counts={}, baseline={}, fire_after_rounds=1,
    ) is None, "test premise: no layer normally eligible this tick"

    due = select_due_missions(ctx, missions)
    ids = {m["id"] for m in due}
    assert "newsletter_redaction" in ids, (
        "with no normally-eligible layer, the missed weekly producer must be "
        "selected via the catch-up fallback"
    )


def test_catchup_never_outranks_real_work_layer(tmp_path: Path):
    """The catch-up must never out-rank a higher-priority layer that has real
    work. With has_inbox_decisions=True (L3 eligible), select picks L3 and the
    L2 newsletter catch-up does NOT steal the tick."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "newsletter_redaction",
            "layer": 2,
            "cadence": "weekly",
            "time": "18:03",
            "day": ["tuesday", "friday"],
            "output_queue": "queues/research/",
            "creates": ["newsletter_draft"],
        },
        {
            "id": "decision_executor",
            "layer": 3,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": [],
        },
    ]
    _make_dept_yaml(repo, missions)
    _add_prompt(repo, "newsletter_redaction")
    _today_dir(repo)
    # L3 eligible (07:00 floor reached + decisions present).
    ctx = _bare_ctx(repo, _FRI, has_inbox_decisions=True)

    due = select_due_missions(ctx, missions)
    layers = {int(m["layer"]) for m in due}
    assert layers == {3}, (
        "a real-work L3 layer must win; the L2 catch-up must not steal the tick"
    )
    assert "newsletter_redaction" not in {m["id"] for m in due}
