"""
test_concierge_agent_model_info.py — model/runtime info for concierges
(Morty, Claudette) on /concierge/<name>.

SCOPE NOTE (flagged for review, see final report): concierges are NOT
/dept/<slug> pages — they render on the separate /concierge/<name> route +
concierge_detail.html template (console/routes/concierge.py), because they
have no dept.yaml / layers / onboarding at all (see concierge_reader.py
module docstring). There is nowhere to declare a per-dept `model:` field for
them, so this module hardcodes a small, explicit map instead of reading
YAML — mirroring the ground truth the card names explicitly:

  - claudette -> no --model flag on ExecStart; settings.json + agent.md
                 both say opus[1m] -> falls back to Claude opus[1m].
  - morty     -> runs DEEPSEEK by design (run-deepseek-morty.sh via a
                 DeepSeek proxy), NOT a Claude model.

This is a SEPARATE, smaller surface from the /dept/<slug> agent_model_info
panel added in test_dept_agent_model_info.py — added here because the card
explicitly names morty/claudette's model values, even though they aren't
`/dept/<slug>` pages.
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
    agents = tmp_path / "agents"
    (agents / "morty").mkdir(parents=True)
    (agents / "claudette").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    from console.services import concierge_reader
    _real_get = concierge_reader.get_concierge
    _real_read = concierge_reader.read_recent_session
    monkeypatch.setattr(concierge_reader, "get_concierge",
                        lambda name, agents_root=str(agents): _real_get(name, str(agents)))
    monkeypatch.setattr(concierge_reader, "read_recent_session",
                        lambda name, n=30, agents_root=str(agents): _real_read(name, n, str(agents)))

    app = FastAPI()
    templates = Jinja2Templates(directory=str(CONSOLE_DIR / "templates"))
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


def test_concierge_reader_exposes_model_info_map():
    """concierge_reader must expose a lookup for each concierge's model/
    runtime deployment fact (hardcoded — concierges have no dept.yaml)."""
    from console.services import concierge_reader
    assert hasattr(concierge_reader, "concierge_model_info")


def test_claudette_model_info_falls_back_to_opus_claude():
    """Claudette: no --model flag on ExecStart; settings.json + agent.md
    both say opus[1m] -> falls back to Claude opus[1m]."""
    from console.services import concierge_reader
    info = concierge_reader.concierge_model_info("claudette")
    assert info["model"] == "opus[1m]"
    assert info["runtime"] == "claude"


def test_morty_model_info_is_deepseek():
    """Morty runs DEEPSEEK by design — NOT a Claude model."""
    from console.services import concierge_reader
    info = concierge_reader.concierge_model_info("morty")
    assert info["runtime"] == "deepseek"
    assert "deepseek" in info["model"].lower()


def test_unknown_concierge_model_info_does_not_crash():
    from console.services import concierge_reader
    info = concierge_reader.concierge_model_info("does-not-exist")
    assert isinstance(info, dict)
    assert info.get("model")


def test_concierge_page_shows_morty_deepseek_model(client):
    r = client.get("/concierge/morty")
    assert r.status_code == 200
    assert "deepseek" in r.text.lower()


def test_concierge_page_shows_claudette_opus_model(client):
    r = client.get("/concierge/claudette")
    assert r.status_code == 200
    assert "opus[1m]" in r.text
