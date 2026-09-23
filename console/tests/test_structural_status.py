"""Tests for the App-posted `structural-approval` commit status (board #1462).

Every test monkeypatches `pr_approver._get` / `pr_approver._mint_token` /
`pr_approver._post` — NOT any name on `structural_status` itself —because
`structural_status.py` deliberately calls those through the `pr_approver`
module object (`pr_approver.xxx(...)`), never via a `from ... import xxx`
copy (see its own module docstring). No real GitHub call happens in any of
these tests.
"""
from __future__ import annotations

from console.services import pr_approver, structural_status

_SHA = "d" * 40
_OTHER_SHA = "e" * 40


def _files(*paths: str) -> list[dict]:
    return [{"filename": p} for p in paths]


def _reviews(*, login: str = "", state: str = "", commit_id: str = "") -> list[dict]:
    if not login:
        return []
    return [{"user": {"login": login}, "state": state, "commit_id": commit_id}]


def _stub_get(monkeypatch, *, files: list[dict], reviews: list[dict] | None = None,
              head_sha: str = _SHA, status_body: dict | None = None):
    """Route pr_approver._get by URL suffix, like the real API would."""
    def fake_get(url, token):
        if url.endswith(f"/status"):
            return 200, status_body if status_body is not None else {"statuses": []}
        if "/files" in url:
            return 200, files
        if "/reviews" in url:
            return 200, reviews or []
        # the bare PR fetch (.../pulls/{n})
        return 200, {"head": {"sha": head_sha}}
    monkeypatch.setattr(pr_approver, "_get", fake_get)


def test_no_token_is_not_provisioned_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_get", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    state, msg = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 1)
    assert state == "not_provisioned"
    assert called["n"] == 0


def test_non_structural_pr_is_success(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_fake")
    _stub_get(monkeypatch, files=_files("console/routes/pr.py", "README.md"))
    posted = {}
    def fake_post(url, token, payload):
        posted["url"] = url; posted["payload"] = payload
        return 201, {}
    monkeypatch.setattr(pr_approver, "_post", fake_post)

    state, description = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 501)
    assert state == "success"
    assert description == "not structural"
    assert posted["payload"]["state"] == "success"
    assert posted["payload"]["context"] == "structural-approval"
    assert f"/statuses/{_SHA}" in posted["url"]


def test_structural_pr_with_no_approval_is_pending(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_fake")
    _stub_get(monkeypatch, files=_files(".claude/agents/rnd.md"), reviews=[])
    posted = {}
    monkeypatch.setattr(pr_approver, "_post",
                         lambda url, token, payload: (posted.update(url=url, payload=payload) or (201, {})))

    state, description = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 502)
    assert state == "pending"
    assert "needs Joris" in description
    assert posted["payload"]["state"] == "pending"


def test_structural_pr_approved_on_old_sha_is_pending(monkeypatch):
    """A review exists but is pinned to a SHA that isn't the current head —
    e.g. the PR pushed a new commit after the approval — must NOT count."""
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_fake")
    _stub_get(
        monkeypatch,
        files=_files(".claude/agents/rnd.md"),
        reviews=_reviews(login=pr_approver.APPROVER_BOT, state="APPROVED", commit_id=_OTHER_SHA),
        head_sha=_SHA,
    )
    posted = {}
    monkeypatch.setattr(pr_approver, "_post",
                         lambda url, token, payload: (posted.update(payload=payload) or (201, {})))

    state, description = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 503)
    assert state == "pending"
    assert posted["payload"]["state"] == "pending"


def test_structural_pr_approved_on_head_is_success(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_fake")
    _stub_get(
        monkeypatch,
        files=_files(".claude/agents/rnd.md"),
        reviews=_reviews(login=pr_approver.APPROVER_BOT, state="APPROVED", commit_id=_SHA),
        head_sha=_SHA,
    )
    posted = {}
    monkeypatch.setattr(pr_approver, "_post",
                         lambda url, token, payload: (posted.update(payload=payload) or (201, {})))

    state, description = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 504)
    assert state == "success"
    assert f"approved by cockpit on {_SHA[:12]}" == description
    assert posted["payload"]["state"] == "success"


def test_no_token_post_status_posts_nothing(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    ok, msg = structural_status.post_status("Bubble-invest", "bubble-ops-loop", _SHA, "success", "not structural")
    assert ok is False
    assert "not provisioned" in msg
    assert called["n"] == 0


def test_post_status_is_idempotent_when_unchanged(monkeypatch):
    """Board #1462 point 3: the periodic sweep calls this every ~2min for
    every open PR — an unchanged verdict must not re-POST."""
    calls = {"post": 0}
    monkeypatch.setattr(
        pr_approver, "_get",
        lambda url, token: (200, {
            "statuses": [{"context": "structural-approval", "state": "success",
                          "description": "not structural"}],
        }),
    )
    monkeypatch.setattr(pr_approver, "_post",
                         lambda *a, **k: calls.__setitem__("post", calls["post"] + 1) or (201, {}))

    ok, msg = structural_status.post_status(
        "Bubble-invest", "bubble-ops-loop", _SHA, "success", "not structural", token="ghs_fake")
    assert ok is True
    assert "skipped" in msg
    assert calls["post"] == 0


def test_post_status_posts_when_state_changed(monkeypatch):
    """A DIFFERENT description/state than what's already posted must still
    go out (idempotence only skips an EXACT match)."""
    monkeypatch.setattr(
        pr_approver, "_get",
        lambda url, token: (200, {
            "statuses": [{"context": "structural-approval", "state": "pending",
                          "description": "needs Joris's cockpit approval"}],
        }),
    )
    posted = {}
    monkeypatch.setattr(pr_approver, "_post",
                         lambda url, token, payload: (posted.update(payload=payload) or (201, {})))

    ok, msg = structural_status.post_status(
        "Bubble-invest", "bubble-ops-loop", _SHA, "success",
        f"approved by cockpit on {_SHA[:12]}", token="ghs_fake")
    assert ok is True
    assert posted["payload"]["state"] == "success"


def test_evaluate_pr_fetch_error_posts_nothing(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_fake")
    monkeypatch.setattr(pr_approver, "_get", lambda url, token: (404, {"message": "Not Found"}))
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    state, msg = structural_status.evaluate("Bubble-invest", "bubble-ops-loop", 999)
    assert state == "error"
    assert called["n"] == 0
