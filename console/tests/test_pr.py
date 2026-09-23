"""Tests for the cockpit PR routes (board #1432): the deep-link detail page
(GET /pr/{owner}/{repo}/{number}) and the App-signed approval endpoint
(POST /pr/{owner}/{repo}/{number}/approve).

Both `console.services.pr_approver` and `console.services.pr_detail_reader`
are monkeypatched — no real GitHub call happens in these tests (mirrors
`test_pr_approver.py`, which already unit-tests the approval service alone).

The `app` fixture (conftest.py) force-reimports every `console.*` module per
test, so `console.routes.pr` must be looked up FRESH inside each test (after
the `client`/`app` fixture has run) rather than imported once at module
scope — a stale module-level reference would monkeypatch an object no route
handler still points at. Mirrors the `_kanban_module()` helper pattern in
test_home_dept_card_count.py.
"""
from __future__ import annotations

_SHA = "a" * 40


def _pr_route():
    from console.routes import pr as pr_route
    return pr_route


def _sample_detail(**overrides) -> dict:
    base = {
        "owner": "Bubble-invest",
        "repo": "bubble-ops-loop",
        "number": 501,
        "title": "feat(#1432): example structural change",
        "html_url": "https://github.com/Bubble-invest/bubble-ops-loop/pull/501",
        "state": "open",
        "head_sha": _SHA,
        "base_ref": "main",
        "files": [
            {"path": ".claude/agents/rnd.md", "structural": True, "status": "modified"},
            {"path": "console/routes/pr.py", "structural": False, "status": "modified"},
        ],
        "files_error": "",
        "structural": True,
        "guard_status": "pending",
    }
    base.update(overrides)
    return base


# ── GET /pr/{owner}/{repo}/{number} ──────────────────────────────────────

def test_pr_detail_renders(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(pr_route.pr_detail_reader, "fetch_pr_detail",
                         lambda owner, repo, number: _sample_detail())
    resp = client.get("/pr/Bubble-invest/bubble-ops-loop/501")
    assert resp.status_code == 200
    assert "feat(#1432): example structural change" in resp.text
    assert ".claude/agents/rnd.md" in resp.text
    # The Approve button is always present on the detail page (RBAC is
    # enforced server-side on the POST, not by hiding the button).
    assert "bubbleApprovePR" in resp.text
    # The head SHA shown here must be the exact value threaded into the
    # Approve button (board #1432 review: TOCTOU pin) — not just displayed.
    assert f"'{_SHA}'" in resp.text


def test_pr_detail_not_found_is_404(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(pr_route.pr_detail_reader, "fetch_pr_detail",
                         lambda owner, repo, number: None)
    resp = client.get("/pr/Bubble-invest/bubble-ops-loop/999")
    assert resp.status_code == 404


def test_pr_detail_degrades_on_files_error(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(
        pr_route.pr_detail_reader, "fetch_pr_detail",
        lambda owner, repo, number: _sample_detail(
            files=[], files_error="Liste des fichiers indisponible.",
            guard_status="unknown",
        ),
    )
    resp = client.get("/pr/Bubble-invest/bubble-ops-loop/501")
    assert resp.status_code == 200
    assert "indisponible" in resp.text


def test_pr_detail_disallowed_repo_is_404_without_hitting_reader(client, monkeypatch):
    """Board #1432 review: owner/repo is allowlisted to Bubble-invest/bubble-ops-*
    BEFORE any GitHub read — a bogus owner/repo must never reach
    pr_detail_reader (closes the repo-existence probe the reviewer flagged)."""
    pr_route = _pr_route()
    called = {"n": 0}
    monkeypatch.setattr(pr_route.pr_detail_reader, "fetch_pr_detail",
                         lambda owner, repo, number: called.__setitem__("n", called["n"] + 1))
    resp = client.get("/pr/some-other-org/some-repo/1")
    assert resp.status_code == 404
    assert called["n"] == 0


# ── POST /pr/{owner}/{repo}/{number}/approve ─────────────────────────────

def test_approve_success(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda owner, repo, number, actor, head_sha: (
            "approved", f"Approved {owner}/{repo}#{number}"),
    )
    resp = client.post(f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "approved"
    assert "detail" in body


def test_approve_not_provisioned_is_503_with_clean_message(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda owner, repo, number, actor, head_sha: (
            "not_provisioned",
            "cockpit-approver App key not provisioned yet — no approval submitted.",
        ),
    )
    resp = client.post(f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}")
    assert resp.status_code == 503
    assert "not provisioned" in resp.json()["detail"]


def test_approve_github_error_is_502(client, monkeypatch):
    pr_route = _pr_route()
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda owner, repo, number, actor, head_sha: ("error", "GitHub 422: nope"),
    )
    resp = client.post(f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}")
    assert resp.status_code == 502
    assert "nope" in resp.json()["detail"]


def test_approve_stale_head_is_409(client, monkeypatch):
    """Board #1432 review: the PR moved since the operator loaded the page —
    must be a distinct, non-2xx status the UI can show clearly, never a
    silent success."""
    pr_route = _pr_route()
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda owner, repo, number, actor, head_sha: (
            "stale", "PR changed since you loaded this page — reload and re-review before approving.",
        ),
    )
    resp = client.post(f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}")
    assert resp.status_code == 409
    assert "reload" in resp.json()["detail"].lower()


def test_approve_missing_head_sha_is_400(client, monkeypatch):
    pr_route = _pr_route()
    called = {"n": 0}
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    resp = client.post("/pr/Bubble-invest/bubble-ops-loop/501/approve")
    assert resp.status_code == 400
    assert called["n"] == 0


def test_approve_malformed_head_sha_is_400(client, monkeypatch):
    pr_route = _pr_route()
    called = {"n": 0}
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    resp = client.post("/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha=not-a-sha")
    assert resp.status_code == 400
    assert called["n"] == 0


def test_approve_disallowed_repo_is_404(client, monkeypatch):
    pr_route = _pr_route()
    called = {"n": 0}
    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    resp = client.post(f"/pr/some-other-org/some-repo/1/approve?head_sha={_SHA}")
    assert resp.status_code == 404
    assert called["n"] == 0


def test_approve_unauthorized_principal_is_403(client_noauth, monkeypatch):
    # client_noauth carries no credential at all -> the auth middleware itself
    # rejects it before pr.py's own RBAC check ever runs (API/JSON request,
    # no `accept: text/html`, no htmx header -> 401 JSON, per main.py).
    resp = client_noauth.post(f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}")
    assert resp.status_code == 401


def test_approve_narrow_rbac_denies_unlisted_principal(client_noauth, app, monkeypatch, tmp_path):
    """A principal granted decide-rights for a DIFFERENT department only
    (not "rnd" and not "*") must be refused — mirrors test_gate_rbac.py's
    narrow-grant + session-cookie pattern exactly."""
    import sys

    pr_route = _pr_route()
    settings = sys.modules["console.settings"]
    sessions = sys.modules["console.services.sessions"]
    monkeypatch.setattr(settings, "GATE_RBAC_JSON", '{"jade": ["content"]}')
    monkeypatch.setattr(settings, "SESSION_DB_PATH", tmp_path / "rbac-sessions.db")
    cookie = f"{settings.SESSION_COOKIE}={sessions.create_session('jade')}"

    monkeypatch.setattr(
        pr_route.pr_approver, "submit_structural_pr_approval",
        lambda owner, repo, number, actor, head_sha: ("approved", "should not be reached"),
    )
    resp = client_noauth.post(
        f"/pr/Bubble-invest/bubble-ops-loop/501/approve?head_sha={_SHA}",
        headers={"Cookie": cookie},
    )
    assert resp.status_code == 403
