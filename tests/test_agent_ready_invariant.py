"""test_agent_ready_invariant.py — board #639.

Covers tools/kanban/check_agent_ready_invariant.py against FIXTURES (no live
`gh` calls in this test file — the live-board run is pasted in the PR body
instead, per the card's evaluation criteria).

Fixtures:
  - The 6 cards Rick found+fixed this session (#316, #245, #364, #557, #476,
    #272), captured in their ORIGINAL (violating) label/body shape — before
    the fix. All 6 must be caught.
  - #639 itself (this card's own tracking issue) — its body QUOTES another
    card's "(to be scoped..." stub inside a markdown table as a worked
    example, but its OWN "## Allowed" section is fully scoped. This is the
    regression case for a naive substring match: the check must NOT flag it.
  - A synthetic Bubble-Shield-shaped card — `agent:ready` + no risk: label +
    a real unfilled "## Allowed" stub. Must be caught (matches the shape of
    the live #385/#386/#396/#398).
  - A compliant card (risk:low + agent:ready + fully scoped) — must NOT be
    flagged.

Run: python3 -m pytest tests/test_agent_ready_invariant.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "tools" / "kanban" / "check_agent_ready_invariant.py"

_spec = importlib.util.spec_from_file_location("check_agent_ready_invariant", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

check_issue = _mod.check_issue
check_issues = _mod.check_issues
fetch_open_issues = _mod.fetch_open_issues
main = _mod.main


def _lbl(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


SCOPED_BODY = """## Job
Do the thing.

## Inputs
(n/a)

## Allowed
- Read the config file.
- Write a report.

## Forbidden
- Do not touch prod.

## Output
A PR.

## Evaluation
Tests pass.
"""

STUB_BODY = """## Job
Some thin card.

## Inputs
(n/a)

## Allowed
(to be scoped by the Manager on triage)

## Forbidden
(to be scoped by the Manager on triage)

## Output
(to be scoped by the Manager on triage)

## Evaluation
(to be scoped by the Manager on triage)
"""

# #639's own body shape: quotes another card's stub INSIDE a markdown table
# (as a worked example of what a bad card looks like), but #639's own
# "## Allowed" section is real, filled-in content.
CARD_639_SHAPED_BODY = """## The finding
Six open cards were sitting in the dispatchable queue wearing `agent:ready`.

| Card | Was | Why it must not auto-route |
|---|---|---|
| **#364** | *no risk label* + `agent:ready` | **Thin stub** — `(to be scoped by the Manager on triage)` in Allowed/Forbidden/Output/Evaluation. |

## Inputs
- Board: Bubble-invest/bubble-ops-board.

## Allowed
- A check that asserts the invariant across all open issues.
- Wire it where it'll be seen.

## Forbidden
- Do NOT auto-remove agent:ready.
- No new standalone cron.

## Evaluation
Run against today's board.
"""


# ── The 6 originally-violating cards (captured in their BEFORE-fix shape) ──

FIXTURE_6_VIOLATING = [
    {
        "number": 316, "title": "Wire GEMINI_API_KEY into Miranda's session",
        "labels": _lbl("risk:medium", "agent:ready", "dept:content"),
        "body": SCOPED_BODY,
    },
    {
        "number": 245, "title": "Some content bug",
        "labels": _lbl("risk:medium", "agent:ready", "dept:content"),
        "body": SCOPED_BODY,
    },
    {
        "number": 364, "title": "Cockpit NAV chart",
        "labels": _lbl("agent:ready", "dept:ben"),  # no risk: label at all
        "body": STUB_BODY,
    },
    {
        "number": 557, "title": "Client-dev bug",
        "labels": _lbl("risk:medium", "agent:ready", "dept:rnd"),
        "body": SCOPED_BODY,
    },
    {
        "number": 476, "title": "Prospection feature",
        "labels": _lbl("risk:medium", "agent:ready", "dept:content"),
        "body": SCOPED_BODY,
    },
    {
        "number": 272, "title": "Infra bug",
        "labels": _lbl("risk:medium", "agent:ready", "dept:rnd"),
        "body": SCOPED_BODY,
    },
]


def test_catches_all_6_previously_fixed_cards():
    violations = check_issues(FIXTURE_6_VIOLATING)
    caught_numbers = {v["number"] for v in violations}
    assert caught_numbers == {316, 245, 364, 557, 476, 272}


def test_316_reason_is_risk_medium_not_low():
    reasons = check_issue(FIXTURE_6_VIOLATING[0])
    assert any("risk:medium" in r for r in reasons)


def test_364_reason_is_no_risk_label_and_stub():
    reasons = check_issue(FIXTURE_6_VIOLATING[2])
    assert any("no risk: label" in r for r in reasons)
    assert any("Allowed" in r for r in reasons)


# ── Regression: #639 quotes another card's stub in a table but is itself
#    fully scoped — must NOT be flagged (naive substring match would flag it) ──

def test_639_shaped_card_is_not_a_false_positive():
    issue = {
        "number": 639,
        "title": "agent:ready invariant check",
        "labels": _lbl("risk:low", "agent:ready", "dept:rnd"),
        "body": CARD_639_SHAPED_BODY,
    }
    assert check_issue(issue) == []


def test_body_substring_alone_does_not_trip_the_check():
    """Direct unit check on the narrower helper: the stub marker appearing
    ANYWHERE in the body (e.g. quoted in prose or a table) must not be
    sufficient — only the card's OWN '## Allowed' section starting with it."""
    assert _mod._allowed_section_is_stub(CARD_639_SHAPED_BODY) is False
    assert _mod._allowed_section_is_stub(STUB_BODY) is True


# ── True positive: Bubble-Shield-shaped stub card (#385/#386/#396/#398 shape) ──

def test_shield_shaped_stub_card_is_caught():
    issue = {
        "number": 385,
        "title": "Bubble Shield pre-release check",
        "labels": _lbl("agent:ready", "type:chore"),  # no risk: label, real board shape
        "body": STUB_BODY,
    }
    reasons = check_issue(issue)
    assert any("no risk: label" in r for r in reasons)
    assert any("Allowed" in r for r in reasons)


# ── Compliant card must never be flagged ──

def test_compliant_card_is_not_flagged():
    issue = {
        "number": 999,
        "title": "A properly scoped low-risk card",
        "labels": _lbl("risk:low", "agent:ready", "dept:rnd"),
        "body": SCOPED_BODY,
    }
    assert check_issue(issue) == []


def test_non_agent_ready_card_is_never_flagged_regardless_of_risk_or_body():
    """The invariant only applies to agent:ready cards — a risk:high card
    with a stub body that ISN'T agent:ready is not a routing hazard."""
    issue = {
        "number": 1,
        "title": "High risk, not agent:ready",
        "labels": _lbl("risk:high", "dept:ben"),
        "body": STUB_BODY,
    }
    assert check_issue(issue) == []


# ── Reports, never mutates ──

def test_check_issue_and_check_issues_take_no_mutating_action(monkeypatch):
    """Explicit assertion that the check module performs no board WRITE calls.
    We patch subprocess.run to explode if invoked with anything other than a
    read-only `gh issue list` — check_issue/check_issues never call subprocess
    at all (they operate on already-fetched dicts), so this also documents
    that the pure check functions have zero I/O surface."""
    def _explode(*args, **kwargs):
        raise AssertionError(
            f"check_issue/check_issues must not perform any subprocess call; "
            f"got: {args}, {kwargs}"
        )
    monkeypatch.setattr(subprocess, "run", _explode)
    check_issues(FIXTURE_6_VIOLATING)  # would raise if it tried any I/O
    for issue in FIXTURE_6_VIOLATING:
        check_issue(issue)


def test_fetch_open_issues_only_ever_calls_gh_issue_list_state_open(monkeypatch):
    """fetch_open_issues is the ONLY function in the module that shells out.
    Assert the exact argv it uses: `gh issue list --repo ... --state open
    ... --json number,title,labels,body` — no create/edit/label/close verb
    anywhere, and --state is explicitly 'open' (never 'all', which could
    include state-changing side context)."""
    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    fetch_open_issues("Bubble-invest/bubble-ops-board")

    argv = captured["argv"]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--state" in argv and argv[argv.index("--state") + 1] == "open"
    # No mutating gh verb anywhere in the invocation.
    forbidden_verbs = {"create", "edit", "close", "delete", "reopen", "comment"}
    assert not (forbidden_verbs & set(argv)), f"mutating verb found in argv: {argv}"


def test_main_json_mode_never_shells_out_to_a_mutating_gh_call(monkeypatch, capsys):
    """End-to-end: main() with --json, subprocess.run stubbed to a read-only
    fixture response, asserts only ONE subprocess call happens (the list) and
    the output is the expected JSON shape."""
    calls = []

    class _FakeProc:
        returncode = 0
        stderr = ""
        stdout = _issues_to_json(FIXTURE_6_VIOLATING)

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    rc = main(["--json"])
    assert rc == 0
    assert len(calls) == 1, f"expected exactly one subprocess call, got {len(calls)}: {calls}"
    out = capsys.readouterr().out
    assert '"violation_count": 6' in out


def _issues_to_json(issues: list[dict]) -> str:
    import json
    return json.dumps(issues)
