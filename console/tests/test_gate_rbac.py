"""Per-user/per-department authorization for gate mutations (board #1121)."""
from __future__ import annotations

import json


def _session_cookie(app, monkeypatch, tmp_path, username: str) -> str:
    import sys

    settings = sys.modules["console.settings"]
    sessions = sys.modules["console.services.sessions"]
    monkeypatch.setattr(settings, "SESSION_DB_PATH", tmp_path / "rbac-sessions.db")
    return f"{settings.SESSION_COOKIE}={sessions.create_session(username)}"


def _set_rbac(monkeypatch, value) -> None:
    import sys

    settings = sys.modules["console.settings"]
    monkeypatch.setattr(settings, "GATE_RBAC_JSON", value)


def test_named_user_can_decide_only_an_allowed_department(
    client_noauth, app, monkeypatch, tmp_path, fixture_root,
):
    _set_rbac(monkeypatch, json.dumps({"rick": ["fixture"], "jade": ["miranda"]}))
    cookie = _session_cookie(app, monkeypatch, tmp_path, "rick")

    allowed = client_noauth.post(
        "/gate/fixture/echo-1/decide",
        data={"action": "approve"},
        headers={"Cookie": cookie},
    )
    assert allowed.status_code == 200
    assert (fixture_root / "bubble-ops-fixture" / "inbox" / "decisions" /
            "echo-1.yaml").exists()

    denied = client_noauth.post(
        "/gate/miranda/anything/decide",
        data={"action": "approve"},
        headers={"Cookie": cookie},
    )
    assert denied.status_code == 403


def test_named_user_cannot_undo_an_unallowed_department(
    client_noauth, app, monkeypatch, tmp_path, fixture_root,
):
    decision = (fixture_root / "bubble-ops-fixture" / "inbox" / "decisions" /
                "echo-1.yaml")
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text("gate_id: echo-1\naction: approve\n", encoding="utf-8")
    _set_rbac(monkeypatch, json.dumps({"jade": ["miranda"]}))
    cookie = _session_cookie(app, monkeypatch, tmp_path, "jade")

    denied = client_noauth.post(
        "/gate/fixture/echo-1/undo", headers={"Cookie": cookie},
    )
    assert denied.status_code == 403
    assert decision.exists()


def test_wildcard_grant_allows_all_departments(
    client_noauth, app, monkeypatch, tmp_path,
):
    _set_rbac(monkeypatch, json.dumps({"rick": ["*"]}))
    cookie = _session_cookie(app, monkeypatch, tmp_path, "rick")
    response = client_noauth.post(
        "/gate/fixture/echo-1/decide",
        data={"action": "approve"},
        headers={"Cookie": cookie},
    )
    assert response.status_code == 200


def test_missing_or_malformed_policy_fails_closed(
    client_noauth, app, monkeypatch, tmp_path,
):
    cookie = _session_cookie(app, monkeypatch, tmp_path, "rick")
    for policy in ("", "not-json", json.dumps({"rick": "*"})):
        _set_rbac(monkeypatch, policy)
        response = client_noauth.post(
            "/gate/fixture/echo-1/decide",
            data={"action": "approve"},
            headers={"Cookie": cookie},
        )
        assert response.status_code == 403


def test_bearer_needs_its_own_explicit_grant(client, monkeypatch):
    _set_rbac(monkeypatch, json.dumps({"rick": ["*"]}))
    denied = client.post(
        "/gate/fixture/echo-1/decide", data={"action": "approve"},
    )
    assert denied.status_code == 403

    _set_rbac(monkeypatch, json.dumps({"bearer": ["fixture"]}))
    allowed = client.post(
        "/gate/fixture/echo-1/decide", data={"action": "approve"},
    )
    assert allowed.status_code == 200
