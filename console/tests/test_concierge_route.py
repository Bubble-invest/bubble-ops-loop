"""test_concierge_route.py — /concierge/<name> page + live session fragment.

Builds an app with the concierge router included and a fake agents_root +
seeded session, so the route renders without depending on live machine
state. (The router itself will be wired into main.py separately.)

TDD: written alongside the route.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parent.parent  # console/


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Seed a fake agents root + a session for morty.
    agents = tmp_path / "agents"
    (agents / "morty").mkdir(parents=True)
    (agents / "claudette").mkdir(parents=True)
    home = tmp_path / "home"
    sess_dir = home / ".claude" / "projects" / "-home-claude-agents-morty"
    sess_dir.mkdir(parents=True)
    with (sess_dir / "s.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": "2026-06-01T08:00:00Z",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text",
                                                      "text": "MORTY_IS_WORKING"}]}}) + "\n")
    monkeypatch.setenv("HOME", str(home))

    # Point the reader's default agents_root at our fake tree.
    from console.services import concierge_reader
    monkeypatch.setattr(concierge_reader, "list_concierges",
                        lambda agents_root=str(agents): _orig_list(str(agents)))
    # Simpler: monkeypatch the module-level default by wrapping get/read.
    _real_get = concierge_reader.get_concierge
    _real_read = concierge_reader.read_recent_session
    monkeypatch.setattr(concierge_reader, "get_concierge",
                        lambda name, agents_root=str(agents): _real_get(name, str(agents)))
    monkeypatch.setattr(concierge_reader, "read_recent_session",
                        lambda name, n=30, agents_root=str(agents): _real_read(name, n, str(agents)))

    app = FastAPI()
    templates = Jinja2Templates(directory=str(CONSOLE_DIR / "templates"))
    # Register sidebar_agents and humanize globals that base.html requires.
    # main.py sets these on create_app(); this minimal test app must mirror
    # that setup so base.html doesn't raise UndefinedError.
    from console.services import dept_registry
    templates.env.globals["sidebar_agents"] = dept_registry.sidebar_agents
    from console.services.humanize import (
        capitalize_fr, humanize_cadence, humanize_future_modes,
        humanize_kind, humanize_mode, humanize_risk, humanize_substep,
        shadow_autonomy_label,
    )
    templates.env.globals["humanize_kind"] = humanize_kind
    templates.env.globals["humanize_risk"] = humanize_risk
    templates.env.globals["humanize_mode"] = humanize_mode
    templates.env.globals["humanize_future_modes"] = humanize_future_modes
    templates.env.globals["humanize_substep"] = humanize_substep
    templates.env.globals["humanize_cadence"] = humanize_cadence
    templates.env.globals["shadow_autonomy_label"] = shadow_autonomy_label
    templates.env.globals["capitalize_fr"] = capitalize_fr
    app.state.templates = templates
    from console.routes import concierge as concierge_route
    app.include_router(concierge_route.router)
    return TestClient(app)


def _orig_list(agents_root):
    from console.services.concierge_reader import list_concierges as _l
    return _l(agents_root)


def test_concierge_page_renders_status_and_session(client):
    r = client.get("/concierge/morty")
    assert r.status_code == 200, r.text
    assert "Morty" in r.text
    assert "MORTY_IS_WORKING" in r.text          # live session turn rendered
    assert "Session en direct" in r.text          # the live view section


def test_concierge_page_has_htmx_autorefresh(client):
    r = client.get("/concierge/morty")
    assert 'hx-get="/concierge/morty/session"' in r.text
    assert "every 5s" in r.text                   # auto-refresh wired


def test_unknown_concierge_404(client):
    r = client.get("/concierge/nope")
    assert r.status_code == 404


def test_session_fragment_renders_turns(client):
    r = client.get("/concierge/morty/session")
    assert r.status_code == 200
    assert "MORTY_IS_WORKING" in r.text


def test_session_fragment_unknown_404(client):
    r = client.get("/concierge/nope/session")
    assert r.status_code == 404
