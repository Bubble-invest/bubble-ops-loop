"""Tests for `console.services.pr_detail_reader._guard_status` (board #1461).

Board #1461 finding: the /pr/{owner}/{repo}/{number} page showed the
structural-merge-guard's status as "inconnu" (unknown) even though the check
had run. Root cause — `_guard_status` filtered `check-runs` by the WORKFLOW
name `structural-merge-guard`, but GitHub reports the check-run under the
JOB id, which is `guard` (the workflow's single job has no explicit `name:`,
see `.github/workflows/structural-merge-guard.yml`). These tests exercise the
name-matching fix and the "pick the latest run" fix (a head SHA can carry more
than one run of the same check — a failure on `pull_request` followed by a
success on `pull_request_review` once the App approval lands).

No real GitHub call happens here — `_get_json` is monkeypatched, mirroring
`test_pr_approver.py`'s and `test_pr.py`'s pattern of not hitting the network.
"""
from __future__ import annotations

from console.services import pr_detail_reader

_OWNER = "Bubble-invest"
_REPO = "bubble-ops-loop"
_SHA = "a" * 40


def _check_runs(runs: list[dict]) -> dict:
    return {"check_runs": runs, "total_count": len(runs)}


def _patch_check_runs(monkeypatch, runs: list[dict]) -> None:
    monkeypatch.setattr(
        pr_detail_reader, "_get_json",
        lambda url, token: _check_runs(runs),
    )


def test_matches_job_name_guard_not_just_workflow_name(monkeypatch):
    """The real-world shape: GitHub names the check-run `guard` (the job id),
    not `structural-merge-guard` (the workflow name) — this must be picked up
    as pass/fail, not fall through to "unknown"."""
    _patch_check_runs(monkeypatch, [
        {"name": "guard", "status": "completed", "conclusion": "success",
         "completed_at": "2026-09-23T10:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "pass"


def test_still_matches_legacy_workflow_name_spelling(monkeypatch):
    """Back-compat: keep accepting a check-run literally named
    `structural-merge-guard` (e.g. a differently-configured job)."""
    _patch_check_runs(monkeypatch, [
        {"name": "structural-merge-guard", "status": "completed",
         "conclusion": "failure", "completed_at": "2026-09-23T10:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "fail"


def test_no_matching_check_run_is_unknown(monkeypatch):
    _patch_check_runs(monkeypatch, [
        {"name": "some-other-check", "status": "completed",
         "conclusion": "success", "completed_at": "2026-09-23T10:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "unknown"


def test_in_progress_run_is_pending(monkeypatch):
    _patch_check_runs(monkeypatch, [
        {"name": "guard", "status": "in_progress", "conclusion": None,
         "started_at": "2026-09-23T10:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "pending"


def test_picks_latest_run_by_completed_at_not_list_order(monkeypatch):
    """Board #1461: a head SHA can carry several `guard` runs — a failing run
    triggered by `pull_request`, then a passing re-run triggered by
    `pull_request_review` once the App approval lands. The LATEST one must
    win, regardless of the order GitHub returns them in."""
    _patch_check_runs(monkeypatch, [
        # Newer, passing run listed FIRST...
        {"name": "guard", "status": "completed", "conclusion": "success",
         "completed_at": "2026-09-23T12:00:00Z"},
        # ...older, failing run listed second. If the code trusted list
        # order (index 0) this would already pass; put the newest run
        # SECOND in another case to prove it's not just luck of order.
        {"name": "guard", "status": "completed", "conclusion": "failure",
         "completed_at": "2026-09-23T09:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "pass"


def test_picks_latest_run_even_when_listed_first_is_older(monkeypatch):
    _patch_check_runs(monkeypatch, [
        # Older, failing run listed FIRST.
        {"name": "guard", "status": "completed", "conclusion": "failure",
         "completed_at": "2026-09-23T09:00:00Z"},
        # Newer, passing run listed SECOND — must still win.
        {"name": "guard", "status": "completed", "conclusion": "success",
         "completed_at": "2026-09-23T12:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "pass"


def test_in_progress_run_uses_started_at_for_recency(monkeypatch):
    """A run still in progress has no `completed_at` — recency must fall back
    to `started_at` so it can still be compared against completed runs."""
    _patch_check_runs(monkeypatch, [
        {"name": "guard", "status": "completed", "conclusion": "success",
         "completed_at": "2026-09-23T09:00:00Z"},
        {"name": "guard", "status": "in_progress", "conclusion": None,
         "started_at": "2026-09-23T12:00:00Z"},
    ])
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "pending"


def test_check_runs_fetch_error_is_unknown(monkeypatch):
    def _boom(url, token):
        raise RuntimeError("network blip")
    monkeypatch.setattr(pr_detail_reader, "_get_json", _boom)
    assert pr_detail_reader._guard_status(_OWNER, _REPO, _SHA, "tok") == "unknown"


def test_name_matching_is_case_insensitive():
    assert pr_detail_reader._is_guard_check_name("Guard") is True
    assert pr_detail_reader._is_guard_check_name("Structural-Merge-Guard") is True
    assert pr_detail_reader._is_guard_check_name("unrelated") is False
    assert pr_detail_reader._is_guard_check_name(None) is False
