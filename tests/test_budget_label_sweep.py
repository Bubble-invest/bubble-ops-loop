"""test_budget_label_sweep.py — board #569.

Covers tools/kanban/check_budget_label_sweep.py against FIXTURES (no live `gh`
calls that mutate the board in this test file — the live report run is pasted in
the PR body instead, per the card's evaluation criteria).

Fixtures mirror the live-board shapes found during #569:
  - a card with a real budget:$10 label            -> NOT missing
  - a card with the budget:unset marker            -> NOT missing (marker counts)
  - a card with NO budget: label at all            -> missing (must be caught)
  - a card whose only "budget"-ish label is a red herring ("over-budget",
    which does not start with "budget:") -> missing (prefix, not substring)

Also locks the two safety properties the card requires:
  - default (no --apply) mutates NOTHING: no gh write subprocess is ever spawned.
  - --apply adds budget:unset ONLY to the missing cards, creating the label first.

Run: python3 -m pytest tests/test_budget_label_sweep.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "tools" / "kanban" / "check_budget_label_sweep.py"

_spec = importlib.util.spec_from_file_location("check_budget_label_sweep", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

cards_missing_budget = _mod.cards_missing_budget
_has_budget_label = _mod._has_budget_label
main = _mod.main
UNSET_LABEL = _mod.UNSET_LABEL


def _lbl(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


FIXTURE_ISSUES = [
    {"number": 100, "title": "has a real budget", "labels": _lbl("dept:rnd", "budget:$10")},
    {"number": 101, "title": "already marked unset", "labels": _lbl("dept:ben", "budget:unset")},
    {"number": 102, "title": "no budget at all", "labels": _lbl("dept:maya", "risk:low")},
    {"number": 103, "title": "red-herring label only", "labels": _lbl("over-budget")},
    {"number": 104, "title": "no labels at all", "labels": []},
]


# ---- detection ------------------------------------------------------------

def test_real_budget_label_counts():
    assert _has_budget_label(["dept:rnd", "budget:$10"]) is True


def test_unset_marker_counts_as_budget():
    # so the sweep is idempotent — a card already flagged is not re-reported
    assert _has_budget_label(["budget:unset"]) is True


def test_case_insensitive_prefix():
    assert _has_budget_label(["Budget:$5"]) is True


def test_red_herring_does_not_count():
    # "over-budget" contains "budget" but does not START with "budget:"
    assert _has_budget_label(["over-budget"]) is False


def test_no_labels_is_missing():
    assert _has_budget_label([]) is False


def test_cards_missing_budget_selects_only_the_unbudgeted():
    missing = cards_missing_budget(FIXTURE_ISSUES)
    assert sorted(m["number"] for m in missing) == [102, 103, 104]


# ---- default is report-only: mutates NOTHING ------------------------------

def test_default_report_never_spawns_a_write(monkeypatch, capsys):
    """The card's core safety property: with no --apply, the sweep must not
    edit the board. We stub fetch to fixtures and assert apply_unset_label /
    ensure_unset_label are NEVER called."""
    monkeypatch.setattr(_mod, "fetch_open_issues", lambda repo: FIXTURE_ISSUES)

    def _boom(*a, **k):  # pragma: no cover - only fires on a bug
        raise AssertionError("report-only mode must not write to the board")

    monkeypatch.setattr(_mod, "apply_unset_label", _boom)
    monkeypatch.setattr(_mod, "ensure_unset_label", _boom)

    rc = main(["--repo", "fake/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 missing a budget: label" in out
    assert "#102" in out and "#103" in out and "#104" in out


def test_json_report_shape(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "fetch_open_issues", lambda repo: FIXTURE_ISSUES)
    monkeypatch.setattr(_mod, "apply_unset_label",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")))
    rc = main(["--repo", "fake/repo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing_count"] == 3
    assert payload["apply"] is False
    assert payload["applied"] == []
    assert sorted(m["number"] for m in payload["missing"]) == [102, 103, 104]


# ---- --apply adds the marker to exactly the missing cards ------------------

def test_apply_labels_only_missing_cards(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "fetch_open_issues", lambda repo: FIXTURE_ISSUES)

    ensured = {"count": 0}
    monkeypatch.setattr(_mod, "ensure_unset_label",
                        lambda repo: ensured.__setitem__("count", ensured["count"] + 1))

    labeled: list[int] = []
    monkeypatch.setattr(_mod, "apply_unset_label",
                        lambda repo, number: labeled.append(number))

    rc = main(["--repo", "fake/repo", "--apply", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert ensured["count"] == 1                      # label ensured exactly once
    assert sorted(labeled) == [102, 103, 104]         # only the unbudgeted cards
    assert payload["apply"] is True
    assert sorted(payload["applied"]) == [102, 103, 104]
    assert payload["apply_errors"] == []


def test_apply_reports_write_failures_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "fetch_open_issues", lambda repo: FIXTURE_ISSUES)
    monkeypatch.setattr(_mod, "ensure_unset_label", lambda repo: None)

    def _fail_on_103(repo, number):
        if number == 103:
            raise RuntimeError("boom on 103")

    monkeypatch.setattr(_mod, "apply_unset_label", _fail_on_103)
    rc = main(["--repo", "fake/repo", "--apply", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["applied"]) == [102, 104]
    assert len(payload["apply_errors"]) == 1


def test_read_failure_exits_1(monkeypatch, capsys):
    def _raise(repo):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(_mod, "fetch_open_issues", _raise)
    rc = main(["--repo", "fake/repo", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload
