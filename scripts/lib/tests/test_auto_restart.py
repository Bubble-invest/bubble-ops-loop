"""test_auto_restart.py — auto-restart dead DEPARTMENTS (PR3, {{OPERATOR}}-approved).

Scope ({{OPERATOR}}):
  • ONLY departments (tony, ben, maya, accountant) are auto-restarted.
  • NEVER concierges (morty, claudette) — {{OPERATOR}} msg 4636. SAFETY INVARIANT,
    enforced as a GUARD (refusal), not just a config default.
  • Guardrail: max 3 restarts per rolling hour per dept; the 4th ESCALATES.
  • Default-on for the 4 depts; opt-out per dept; concierges hard-excluded.

Required tests (per the task): restart fires on a dead dept; the guardrail caps
at 3/hour then escalates; a concierge is NEVER restarted.

TDD: written against the module; each guard has a RED-if-removed assertion.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from scripts.lib.auto_restart import (
    ACT_ESCALATE,
    ACT_REFUSE_CONCIERGE,
    ACT_REFUSE_NOT_DEPT,
    ACT_REFUSE_OPTED_OUT,
    ACT_RESTART,
    CONCIERGE_DENYLIST,
    DEPT_ALLOWLIST,
    append_restart_event,
    decide_restart,
    format_restart_event,
    is_department,
    read_restart_events,
    restarts_in_window,
)


NOW = _dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _restart_record(slug: str, epoch: float) -> dict:
    return {"ts": _iso(epoch), "slug": slug, "action": ACT_RESTART, "reason": ""}


# ── The allowlist / concierge guard (SAFETY INVARIANT) ────────────────────


def test_the_four_depts_are_departments():
    for d in ("tony", "ben", "maya", "accountant"):
        assert is_department(d), f"{d} must be a restartable department"
    assert DEPT_ALLOWLIST == {"tony", "ben", "maya", "accountant"}


def test_concierges_are_never_departments():
    for c in ("morty", "claudette"):
        assert not is_department(c), f"{c} is a concierge — must never be a dept"
    assert CONCIERGE_DENYLIST == {"morty", "claudette"}


def test_concierge_check_wins_even_if_in_allowlist(monkeypatch):
    """The concierge guard must win even if a concierge is wrongly added to the
    allowlist — proves the refusal is a GUARD, not just set membership."""
    import scripts.lib.auto_restart as ar
    monkeypatch.setattr(
        ar, "DEPT_ALLOWLIST", frozenset(ar.DEPT_ALLOWLIST | {"morty"})
    )
    # is_department still refuses morty because the concierge check comes first.
    assert ar.is_department("morty") is False
    d = ar.decide_restart("morty", [], NOW)
    assert d["action"] == ACT_REFUSE_CONCIERGE


def test_unknown_slug_is_refused_failclosed():
    d = decide_restart("some-new-agent", [], NOW)
    assert d["action"] == ACT_REFUSE_NOT_DEPT


# ── Restart fires on a dead dept (the happy path) ─────────────────────────


def test_restart_fires_on_dead_dept_no_history():
    d = decide_restart("ben", [], NOW)
    assert d["action"] == ACT_RESTART
    assert d["count"] == 0
    assert "restart 1/3" in d["reason"]


def test_concierge_is_never_restarted():
    """The headline safety test: a dead concierge is NEVER restarted."""
    for c in ("morty", "claudette"):
        d = decide_restart(c, [], NOW)
        assert d["action"] == ACT_REFUSE_CONCIERGE
        assert "REFUSED" in d["reason"]


# ── Guardrail: caps at 3/hour, then escalates ─────────────────────────────


def test_guardrail_allows_up_to_three_in_the_hour():
    # 0, 1, 2 prior restarts in-window → still RESTART (the 1st/2nd/3rd).
    for prior in range(3):
        hist = [_restart_record("maya", NOW - 600 * (i + 1)) for i in range(prior)]
        d = decide_restart("maya", hist, NOW)
        assert d["action"] == ACT_RESTART, f"prior={prior} should still restart"
        assert d["count"] == prior


def test_guardrail_escalates_on_the_fourth():
    # 3 restarts already in the last hour → the 4th ESCALATES, no restart.
    hist = [_restart_record("maya", NOW - 600 * (i + 1)) for i in range(3)]
    d = decide_restart("maya", hist, NOW)
    assert d["action"] == ACT_ESCALATE
    assert d["count"] == 3
    assert "guardrail" in d["reason"].lower()


def test_restarts_outside_the_window_do_not_count():
    # 3 restarts but all > 1h ago → window empty → RESTART again.
    hist = [_restart_record("maya", NOW - 3600 - 60 * (i + 1)) for i in range(3)]
    d = decide_restart("maya", hist, NOW)
    assert d["action"] == ACT_RESTART
    assert d["count"] == 0


def test_window_boundary_is_inclusive_of_recent_only():
    # one restart exactly 59m59s ago counts; one 60m01s ago does not.
    hist = [
        _restart_record("ben", NOW - (3600 - 1)),   # in-window
        _restart_record("ben", NOW - (3600 + 1)),   # just out
    ]
    assert restarts_in_window(hist, "ben", NOW) == 1


def test_per_dept_isolation_of_the_budget():
    # maya's restarts must not count against ben's budget.
    hist = [_restart_record("maya", NOW - 600 * (i + 1)) for i in range(3)]
    assert decide_restart("ben", hist, NOW)["action"] == ACT_RESTART
    assert decide_restart("maya", hist, NOW)["action"] == ACT_ESCALATE


def test_custom_max_per_hour():
    hist = [_restart_record("tony", NOW - 60)]
    assert decide_restart("tony", hist, NOW, max_per_hour=1)["action"] == ACT_ESCALATE
    assert decide_restart("tony", hist, NOW, max_per_hour=2)["action"] == ACT_RESTART


# ── Opt-out (default-on, but per-dept opt-out honored) ────────────────────


def test_opted_out_dept_is_refused():
    d = decide_restart("ben", [], NOW, opted_out=True)
    assert d["action"] == ACT_REFUSE_OPTED_OUT


def test_opt_out_does_not_override_concierge_guard():
    # Even with opted_out=False, a concierge is refused as concierge (safety
    # ordering: concierge check is BEFORE the opt-out / budget logic).
    d = decide_restart("morty", [], NOW, opted_out=False)
    assert d["action"] == ACT_REFUSE_CONCIERGE


# ── State I/O (restart history JSONL) ─────────────────────────────────────


def test_append_then_read_restart_history(tmp_path):
    path = str(tmp_path / "state" / "auto-restart.jsonl")  # parent created on write
    append_restart_event(path, format_restart_event("ben", ACT_RESTART, "r1",
                                                     ts="2026-06-19T11:00:00Z"))
    append_restart_event(path, format_restart_event("maya", ACT_ESCALATE, "g",
                                                    ts="2026-06-19T11:30:00Z"))
    evs = read_restart_events(path)
    assert [e["slug"] for e in evs] == ["ben", "maya"]
    assert evs[0]["action"] == ACT_RESTART


def test_read_restart_history_missing_file_is_empty():
    assert read_restart_events("/no/such/restart.jsonl") == []


def test_read_restart_history_skips_garbage(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        '{"ts":"t1","slug":"ben","action":"restart"}\n'
        "\n"
        "not json\n"
        "[1,2]\n"
        '{"ts":"t2","slug":"maya","action":"restart"}\n',
        encoding="utf-8",
    )
    evs = read_restart_events(str(p))
    assert [e["slug"] for e in evs] == ["ben", "maya"]


def test_end_to_end_budget_consumed_from_written_history(tmp_path):
    """Write 3 restart records via the public API, then the decision reads them
    back and escalates — proves the count comes from real persisted state."""
    path = str(tmp_path / "auto-restart.jsonl")
    for i in range(3):
        append_restart_event(
            path,
            format_restart_event("ben", ACT_RESTART, ts=_iso(NOW - 600 * (i + 1))),
        )
    hist = read_restart_events(path)
    d = decide_restart("ben", hist, NOW)
    assert d["action"] == ACT_ESCALATE and d["count"] == 3


def test_only_restart_actions_consume_budget(tmp_path):
    """An escalate/refuse record in history must NOT count toward the budget."""
    hist = [
        {"ts": _iso(NOW - 100), "slug": "ben", "action": ACT_ESCALATE},
        {"ts": _iso(NOW - 200), "slug": "ben", "action": ACT_REFUSE_CONCIERGE},
        _restart_record("ben", NOW - 300),  # only this one counts
    ]
    assert restarts_in_window(hist, "ben", NOW) == 1
