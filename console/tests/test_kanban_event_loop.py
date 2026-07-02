"""
test_kanban_event_loop.py — board #447 (kanban_board blocked the event loop
+ un-timed gh subprocess calls on gate approvals).

Two regressions covered:

1. `kanban_board` (console/routes/kanban.py) was the only `async def` route
   handler and ran `urllib.request.urlopen` (up to 5 sequential pages, 20s
   timeout each) directly on the event loop. A hung GitHub call froze the
   entire single-worker console — including unrelated concurrent requests —
   for up to ~100s. Fixed by making the handler sync `def` so FastAPI
   threadpools it.

2. `console/services/github_reader.py`'s `_gh` / `_put` / `_gh_contents_sha`
   ran `subprocess.run(["gh", ...])` with no timeout — a hung `gh` process
   tied up a threadpool slot indefinitely. Fixed by adding `timeout=30` to
   each call, with `subprocess.TimeoutExpired` surfaced through the existing
   warning-log + return-None path (never raised to the caller).
"""
from __future__ import annotations

import inspect
import subprocess
import threading
import time

import pytest


def _kanban_module():
    """Resolve the LIVE kanban route module.

    The `app` fixture deletes + re-imports every `console.*` module on each
    test, so we must resolve the module AFTER the app/client fixture exists
    (see test_kanban_card_decide.py for the same pattern) — importing at
    collection time would give us a stale copy the running app doesn't use.
    """
    from console.routes import kanban as _kanban
    return _kanban


def _github_reader_module():
    from console.services import github_reader as _gr
    return _gr


# ── 1. kanban_board must not be an async def with inline blocking I/O ────────

def test_kanban_board_handler_is_not_async(client):
    """`kanban_board` must be a sync `def` (FastAPI runs sync handlers in its
    threadpool) — NOT `async def`, which would execute `_fetch_issues()`'s
    blocking urllib calls directly on the event loop."""
    _kanban = _kanban_module()
    assert not inspect.iscoroutinefunction(_kanban.kanban_board), (
        "kanban_board must be sync `def` so FastAPI threadpools it; "
        "an async def here would run _fetch_issues()'s blocking urlopen "
        "calls on the event loop (board #447)."
    )


# NOTE (board #458): the former
# `test_kanban_board_hung_fetch_does_not_block_concurrent_request` test was
# DELETED here — it was FALSE SECURITY. It drove a hung `_fetch_issues()` through
# Starlette's TestClient and asserted a concurrent `/health` request stayed fast;
# but the TestClient runs the app via anyio's BlockingPortal, which serves the
# second request on a *different* portal thread regardless of whether the handler
# is sync or async. The test therefore PASSED even when `kanban_board` was mutated
# back to `async def` (verified by the PR #184 independent reviewer) — so it
# fenced nothing. `test_kanban_board_handler_is_not_async` above is the real
# regression fence for issue #447 (it fails the moment the handler becomes async).
# A faithful event-loop-blocking test would need a real `uvicorn.Server`; the
# static async-def guard is a simpler, reliable equivalent, so we don't add one.


# ── 2. single-flight cache: N concurrent stale-cache requests → 1 fetch ───────

def test_fetch_issues_single_flight_one_fetch_under_concurrency():
    """board #458: `kanban_board` is threadpooled (issue #447), so N concurrent
    requests arriving on a stale cache would each pass the TTL check and fire a
    duplicate 5-page GitHub fetch. The single-flight lock must collapse them to
    exactly ONE fetch: the winner refreshes the cache, the rest wait on the lock
    and reuse it.

    Drives `_fetch_issues` directly from N threads (not via TestClient, whose
    BlockingPortal would serialise the calls and mask the race). A barrier makes
    all threads hit the stale-cache check together; a counting stub for the
    HTTP-fetch body proves it ran once."""
    _kanban = _kanban_module()

    # Start from a stale (empty) cache so every thread takes the slow path.
    _kanban._cache_data = []
    _kanban._cache_ts = 0.0

    N = 12
    fetch_calls = 0
    fetch_lock = threading.Lock()
    barrier = threading.Barrier(N)

    def counting_locked_fetch():
        # Stands in for the real GitHub fetch body (called under _fetch_lock).
        nonlocal fetch_calls
        with fetch_lock:
            fetch_calls += 1
        time.sleep(0.05)  # widen the window a duplicate fetch could sneak into
        issues = [{"number": 1, "title": "x", "labels": [],
                   "url": "", "updatedAt": "", "createdAt": "", "state": "open"}]
        _kanban._cache_data = issues
        _kanban._cache_ts = time.monotonic()
        return issues, None

    # Patch the fetch body, not _fetch_issues itself, so the REAL lock +
    # double-checked cache logic under test is exercised.
    orig = _kanban._fetch_issues_locked
    _kanban._fetch_issues_locked = counting_locked_fetch
    try:
        results: list = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()  # release all threads onto the stale cache at once
            data, err = _kanban._fetch_issues()
            with results_lock:
                results.append((len(data), err))

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        _kanban._fetch_issues_locked = orig
        _kanban._cache_data = []
        _kanban._cache_ts = 0.0

    assert fetch_calls == 1, (
        f"expected exactly 1 GitHub fetch under {N} concurrent stale-cache "
        f"requests (single-flight), got {fetch_calls}"
    )
    # Every caller got the same successful, non-empty result.
    assert len(results) == N
    assert all(err is None and n == 1 for n, err in results), results


# ── 3. gh subprocess calls must carry a timeout and fail safe ────────────────

def test_gh_contents_sha_timeout_returns_none_with_warning(monkeypatch, caplog):
    """A TimeoutExpired from the `gh api ... --jq .sha` subprocess call must
    be caught and surfaced as None (existing warning-log + return-None
    path) — never raised to the caller."""
    _gr = _github_reader_module()

    def fake_run(cmd, *args, **kwargs):
        assert kwargs.get("timeout") == 30, (
            "gh subprocess call must carry timeout=30 (board #447)"
        )
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level("WARNING"):
        result = _gr._gh_contents_sha(
            "repos/Bubble-invest/bubble-ops-fixture/contents/foo.yaml",
            env={},
        )

    assert result is None
    assert any("timed out" in rec.message for rec in caplog.records)


def test_write_gate_decision_github_gh_timeout_fails_safe(monkeypatch, caplog):
    """A TimeoutExpired anywhere in the `_gh`/`_put` chain used by
    `_write_gate_decision_github` (the host=local gate-approval write path)
    must fail safe: return None + log a warning, never raise/500."""
    _gr = _github_reader_module()

    def fake_run(cmd, *args, **kwargs):
        assert kwargs.get("timeout") == 30, (
            "gh subprocess call must carry timeout=30 (board #447)"
        )
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_gr, "_read_contents_token", lambda: "fake-token")

    with caplog.at_level("WARNING"):
        result = _gr._write_gate_decision_github(
            "fixture", "echo-1", {"decision": "approve"}
        )

    assert result is None
    assert any("gh api failed" in rec.message for rec in caplog.records)


def test_gh_subprocess_calls_carry_timeout_kwarg():
    """Static guard: every `subprocess.run(["gh", ...])` call site in
    github_reader.py must pass timeout= — catches a future regression where
    a new un-timed gh call is added without a test-time monkeypatch to
    notice it."""
    import ast
    import inspect as _inspect

    _gr = _github_reader_module()
    src = _inspect.getsource(_gr)
    tree = ast.parse(src)

    offending = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            if not has_timeout:
                offending.append(node.lineno)

    assert not offending, (
        f"subprocess.run() call(s) without timeout= at line(s) {offending} "
        "in github_reader.py (board #447)"
    )
