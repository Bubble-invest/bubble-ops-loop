"""Tests for #1571 — Tony's `dept_kpi_watch` (L2, daily 10:30) and
`directive_review` (L3, daily 14:00) never being selected by
`due_missions.py wake-prompt` / `select_due_missions`.

ROOT CAUSE: both are legacy layer-shim missions (no dedicated
`missions/<id>/PROMPT.md`) declared on queue-signal-gated layers —
`_layer_eligible_from_signals` requires L2: `time>=12:00 AND has_research`
and L3: `time>=07:00 AND has_decisions`. Tony (a management dept) has no
research queue and no inbox/decisions queue at all, so `has_research` and
`has_decisions` are permanently False — L2/L3 are NEVER independently
eligible and these two scheduled missions never ran (last real run
2026-09-05, 23 days stale — board card #1571).

FIX: `_due_scheduled_catchup_layer` (the #428 "Mac asleep at the scheduled
slot" safeguard) is broadened, for layers 2 and 3 ONLY, to also catch
shim-resolved (no dedicated prompt) scheduled producer missions — see that
function's docstring in dispatch_helpers.py for the full Chesterton's-fence
reasoning (why L1/L4 deliberately keep the original dedicated-prompt-only
scope, and why #1085's L3-before-L4 sequencing guarantee is preserved).

These tests exercise the SAME entry points `due_missions.py wake-prompt`
uses (`build_dispatch_ctx(..., materialize=False)` + `select_due_missions`),
against a Tony-shaped `dept.yaml` fixture (morning_brief L1, dept_kpi_watch
L2, directive_review L3, ceo_debrief + session_handoff L4 — same ids/times/
layers as the live `/srv/agents/tony/dept.yaml`, per the read-only dry check
described in the PR).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    commit_dispatch,
    select_due_missions,
    write_last_run,
)

# Monday 2026-09-28, Paris is still CEST (DST ends last Sunday of October) —
# UTC+2. This mirrors the live verification timestamp from the card comment
# (2026-09-28 12:37 Paris).
_PARIS_UTC_OFFSET = timedelta(hours=2)


def _at_paris_time(hh: int, mm: int) -> datetime:
    """Build a tz-aware UTC datetime for 2026-09-28 at the given Paris-local
    wall-clock time (CEST, UTC+2)."""
    return datetime(2026, 9, 28, hh, mm, tzinfo=timezone.utc) - _PARIS_UTC_OFFSET


_TONY_MISSIONS = [
    {
        "id": "morning_brief",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "output_queue": "queues/management/",
        "creates": ["morning_brief"],
    },
    {
        "id": "dept_kpi_watch",
        "layer": 2,
        "cadence": "daily",
        "time": "10:30",
        "output_queue": "queues/management/",
        "creates": ["dept_kpi_analysis", "directive_draft"],
    },
    {
        "id": "directive_review",
        "layer": 3,
        "cadence": "daily",
        "time": "14:00",
        "output_queue": "queues/management/",
        "creates": ["directive", "directive_gate"],
        "gate_policy_id": "directive_emit",
    },
    {
        "id": "ceo_debrief",
        "layer": 4,
        "cadence": "daily",
        "time": "20:30",
        "output_queue": "queues/management/",
        "creates": ["ceo_debrief", "risk_brief", "management_export"],
    },
    {
        "id": "session_handoff",
        "layer": 4,
        "cadence": "daily",
        "time": "21:30",
        "output_queue": "outputs/",
        "creates": ["session_handoff"],
    },
]


def _mk_tony_repo(tmp_path: Path) -> Path:
    """A Tony-shaped repo: recurring_missions from dept.yaml, empty research
    and inbox/decisions queues (management dept — no such queue exists at
    all, matching the live dept)."""
    repo = tmp_path / "tony"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": _TONY_MISSIONS}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return repo


def _mark_l1_already_fired(repo: Path, now: datetime) -> None:
    """Stamp the L1 layer marker with an earlier-this-morning timestamp so L1
    is not independently eligible at the test's `now` (mirrors Tony's real
    morning_brief having already run at 07:00)."""
    today = now.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "1", when=now.replace(hour=5, minute=5))


def _ids(due: list[dict]) -> set:
    return {m.get("id") for m in due}


# ===========================================================================
# (a) empty research queue at 12:37 -> dept_kpi_watch is due; not due again
#     the same day after completion.
# ===========================================================================

def test_a_dept_kpi_watch_due_at_1237_with_empty_research_queue(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(12, 37)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    assert ctx["has_research_items"] is False
    assert ctx["has_inbox_decisions"] is False

    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "dept_kpi_watch" in _ids(due), (
        "dept_kpi_watch (L2, daily 10:30) must be due at 12:37 even though "
        "Tony's research queue is (and always will be) empty"
    )
    assert all(m["layer"] == 2 for m in due), (
        "only L2's due mission(s) should be returned this tick"
    )


def test_a_dept_kpi_watch_not_due_again_same_day_after_completion(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(12, 37)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "dept_kpi_watch" in _ids(due)

    mission = next(m for m in _TONY_MISSIONS if m["id"] == "dept_kpi_watch")
    dispatched_at = now
    completed_at = now + timedelta(minutes=5)
    committed = commit_dispatch(
        repo, mission, dispatched_at=dispatched_at, completed_at=completed_at,
        artifacts=["queues/management/dept_kpi_analysis-1.yaml"],
    )
    assert committed is True

    # A later tick the SAME day (e.g. the next 5-minute heartbeat).
    later = now + timedelta(minutes=10)
    ctx2 = build_dispatch_ctx(repo, now_utc=later, materialize=False)
    due2 = select_due_missions(ctx2, _TONY_MISSIONS)
    assert "dept_kpi_watch" not in _ids(due2), (
        "dept_kpi_watch must not be re-selected the same day once it has completed"
    )


# ===========================================================================
# (b) before 10:30 it is not due.
# ===========================================================================

def test_b_dept_kpi_watch_not_due_before_1030(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(8, 15)  # after L1's 07:00 floor, before L2's 10:30 slot
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "dept_kpi_watch" not in _ids(due), (
        "dept_kpi_watch's own scheduled time (10:30) has not been reached yet"
    )


# ===========================================================================
# (c) a dept with no scheduled missions and empty queues still gets [] —
#     the broadened catch-up must never manufacture new firing.
# ===========================================================================

def test_c_no_scheduled_missions_empty_queues_stays_empty(tmp_path: Path):
    repo = tmp_path / "quiet_dept"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": []}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    now = _at_paris_time(12, 37)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, [])
    assert due == [], (
        "a dept with no recurring missions at all must stay a true heartbeat tick"
    )


def test_c_only_event_or_consumer_missions_empty_queues_stays_empty(tmp_path: Path):
    """A dept whose ONLY missions are queue-signal consumers (input_queue set,
    no explicit time) must still return [] on an empty queue — the broadened
    catch-up never fires a consumer, and there is no scheduled producer to
    catch up on."""
    repo = tmp_path / "consumer_only_dept"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    missions = [
        {
            "id": "drafting",
            "layer": 2,
            "cadence": "daily",
            "time": "10:30",
            "input_queue": "queues/research/",
            "output_queue": "queues/gates/",
            "creates": [],
        },
    ]
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    now = _at_paris_time(12, 37)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, missions)
    assert due == [], (
        "a consumer mission (input_queue set) must never be caught up — it is "
        "gated by its queue, not the clock, even though it has cadence+time"
    )


# ===========================================================================
# (d) the L3 directive_review equivalent.
# ===========================================================================

def test_d_directive_review_due_at_1405_with_empty_decisions_queue(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(14, 5)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    assert ctx["has_inbox_decisions"] is False

    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "directive_review" in _ids(due), (
        "directive_review (L3, daily 14:00) must be due at 14:05 even though "
        "Tony's inbox/decisions queue is (and always will be) empty"
    )
    assert all(m["layer"] == 3 for m in due)


def test_d_directive_review_not_due_before_1400(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(13, 30)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "directive_review" not in _ids(due)


def test_d_directive_review_not_due_again_same_day_after_completion(tmp_path: Path):
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(14, 5)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "directive_review" in _ids(due)

    mission = next(m for m in _TONY_MISSIONS if m["id"] == "directive_review")
    committed = commit_dispatch(
        repo, mission, dispatched_at=now, completed_at=now + timedelta(minutes=5),
        artifacts=[],
    )
    assert committed is True

    later = now + timedelta(minutes=10)
    ctx2 = build_dispatch_ctx(repo, now_utc=later, materialize=False)
    due2 = select_due_missions(ctx2, _TONY_MISSIONS)
    assert "directive_review" not in _ids(due2)


# ===========================================================================
# L4 must NOT be starved by, nor steal, the L2/L3 catch-up — and #1085's
# L3-before-L4 sequencing must still hold for a genuinely blocked L3.
# ===========================================================================

def test_l4_scheduled_missions_untouched_by_l2_l3_catchup(tmp_path: Path):
    """At 12:37 (dept_kpi_watch's catch-up tick), Tony's L4 missions (not yet
    at their 20:30/21:30 slots) must not appear in the due list."""
    repo = _mk_tony_repo(tmp_path)
    now = _at_paris_time(12, 37)
    _mark_l1_already_fired(repo, now)

    ctx = build_dispatch_ctx(repo, now_utc=now, materialize=False)
    due = select_due_missions(ctx, _TONY_MISSIONS)
    assert "ceo_debrief" not in _ids(due)
    assert "session_handoff" not in _ids(due)
