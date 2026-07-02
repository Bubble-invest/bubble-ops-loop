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


def test_kanban_board_hung_fetch_does_not_block_concurrent_request(
    client, monkeypatch
):
    """Simulate a hung `_fetch_issues()` (e.g. a stalled GitHub call) and
    verify a second, unrelated concurrent request is served immediately
    instead of queueing behind it — proving the handler runs off the event
    loop (board #447 acceptance criterion: 'a simulated hung fetch no
    longer blocks a second concurrent request')."""
    _kanban = _kanban_module()

    release = threading.Event()
    entered = threading.Event()

    def hung_fetch():
        entered.set()
        release.wait(timeout=5)
        return [], None

    monkeypatch.setattr(_kanban, "_fetch_issues", hung_fetch)

    results: dict[str, float] = {}

    def call_kanban():
        start = time.monotonic()
        r = client.get("/kanban")
        results["kanban_status"] = r.status_code
        results["kanban_elapsed"] = time.monotonic() - start

    t = threading.Thread(target=call_kanban)
    t.start()

    # Wait until the kanban request is actually inside the hung fetch.
    assert entered.wait(timeout=5), "kanban request never reached _fetch_issues"

    # A concurrent, unrelated request must be served promptly — it must not
    # queue behind the stuck /kanban request on a single worker thread.
    # /health is used (not /) because home() also reads the shared
    # _fetch_issues() cache (see home.py:_kanban_queue_counts) and would
    # legitimately hang too — /health never touches the board cache.
    start = time.monotonic()
    r2 = client.get("/health")
    elapsed = time.monotonic() - start
    assert r2.status_code == 200
    assert elapsed < 2.0, (
        f"concurrent request took {elapsed:.2f}s — kanban_board appears to "
        "be blocking the event loop / worker (board #447 regression)"
    )

    release.set()
    t.join(timeout=5)
    assert results.get("kanban_status") == 200


# ── 2. gh subprocess calls must carry a timeout and fail safe ────────────────

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
