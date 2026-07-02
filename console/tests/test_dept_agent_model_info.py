"""
test_dept_agent_model_info.py — dept.yaml `department.model` (+ runtime/
hierarchy/layers/host) surfaced on /dept/<slug> as a compact info panel.

Card: add each agent's CURRENT MODEL (and other useful deployment metadata)
to the dept UI so {{OPERATOR}} can see at a glance what an agent is actually
running on — instead of having to SSH the VPS and grep a systemd unit.

Design (Rick, 2026-07-02): `department.model` already exists in dept.schema
.yaml as "the value written into .claude/settings.json `model` by
isolation_scaffold" — i.e. it's already the ground-truth deployment fact for
which Claude model a dept's orchestrator process launches with. We reuse it
rather than inventing a parallel field. `runtime` is a NEW field (default
"claude") so a dept whose agent process isn't a Claude process at all (e.g.
a DeepSeek-backed concierge) can declare that distinctly from "no model
pinned, falls back to the platform default".

Three behaviours under test, per the card:
  (a) dept_detail() route handler passes model/metadata into the template
      context (agent_model_info key).
  (b) a dept.yaml WITH a `model:` field surfaces that value correctly.
  (c) a dept.yaml WITHOUT a `model:` field falls back gracefully to the
      platform default label — never crashes, never raises KeyError.
"""
from __future__ import annotations

import yaml


# ─── (a) route handler passes model/metadata into the template context ────


def test_dept_detail_route_context_has_agent_model_info(client, monkeypatch):
    """dept_detail() must build an `agent_model_info` dict and pass it to
    the template context — inspected directly at the route-handler level
    (not just via substring match on rendered HTML) so a future template
    refactor can't silently drop the wiring without failing this test.

    Uses the real `client`/`app` fixtures (real disk-backed 'fixture' dept
    from conftest.py) and spies on the real Jinja2Templates.TemplateResponse
    to capture the exact context dict dept_detail() builds."""
    from console.services import github_reader

    original = github_reader.load_dept_yaml

    def _patched(slug):
        doc = original(slug)
        if slug == "fixture" and isinstance(doc, dict):
            doc.setdefault("department", {})["model"] = "opus[1m]"
        return doc

    monkeypatch.setattr(github_reader, "load_dept_yaml", _patched)

    templates = client.app.state.templates
    original_response = templates.TemplateResponse
    captured = {}

    def _spy(name, context, *args, **kwargs):
        if name == "dept_detail.html":
            captured["context"] = context
        return original_response(name, context, *args, **kwargs)

    monkeypatch.setattr(templates, "TemplateResponse", _spy)

    r = client.get("/dept/fixture")
    assert r.status_code == 200

    assert "agent_model_info" in captured["context"], (
        "dept_detail() must pass an 'agent_model_info' key in the template "
        "context so dept_detail.html can render the model/runtime panel."
    )
    info = captured["context"]["agent_model_info"]
    assert info["model"] == "opus[1m]"


# ─── (b) dept.yaml WITH a model: field surfaces that value ────────────────


def test_load_agent_model_info_reads_declared_model():
    """github_reader.load_agent_model_info(slug) must read department.model
    verbatim when the dept.yaml declares it."""
    from console.services import github_reader

    dept_yaml = {
        "department": {"slug": "morty-like", "level": "ops", "model": "deepseek-v4-pro"},
        "hierarchy": {"level": "ops"},
        "layers": {"subscribed": [1, 4]},
    }
    info = github_reader.load_agent_model_info(dept_yaml)
    assert info["model"] == "deepseek-v4-pro"


def test_dept_detail_page_shows_declared_model(client, monkeypatch):
    """GET /dept/fixture with a dept.yaml declaring model: 'sonnet[1m]' must
    render that exact string somewhere on the page."""
    from console.services import github_reader

    original = github_reader.load_dept_yaml

    def _patched(slug):
        doc = original(slug)
        if slug == "fixture" and isinstance(doc, dict):
            doc.setdefault("department", {})["model"] = "sonnet[1m]"
        return doc

    monkeypatch.setattr(github_reader, "load_dept_yaml", _patched)
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "sonnet[1m]" in r.text, (
        "Declared department.model must be visible verbatim on /dept/<slug>."
    )


# ─── (c) dept.yaml WITHOUT a model: field falls back gracefully ───────────


def test_load_agent_model_info_falls_back_when_model_absent():
    """No department.model declared -> a sensible default label, never a
    KeyError / crash."""
    from console.services import github_reader

    dept_yaml = {
        "department": {"slug": "no-model-dept", "level": "ops"},
        "hierarchy": {"level": "ops"},
        "layers": {"subscribed": [1, 2, 3, 4]},
    }
    info = github_reader.load_agent_model_info(dept_yaml)
    assert info["model"], "model key must always be populated with a fallback label"
    assert info["model"] != ""


def test_load_agent_model_info_handles_none_dept_yaml():
    """dept_yaml itself missing (e.g. mid-onboarding, no file yet) must not
    raise — the panel just shows the fallback/default state."""
    from console.services import github_reader

    info = github_reader.load_agent_model_info(None)
    assert isinstance(info, dict)
    assert info.get("model")


def test_dept_detail_page_falls_back_when_model_not_declared(client):
    """The 'fixture' test dept.yaml (see conftest.py fixture_root) does NOT
    declare department.model. The page must still render 200 (no crash) and
    show SOME model label (the fallback), not blow up with KeyError."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    # graceful: page renders, and the fallback default is shown somewhere.
    assert "opus[1m]" in r.text or "non déclaré" in r.text.lower() or \
        "not declared" in r.text.lower()


# ─── Extra metadata: runtime / hierarchy level / layers / host ────────────


def test_load_agent_model_info_includes_runtime_and_hierarchy():
    """The info dict should also surface runtime (claude/deepseek/...),
    hierarchy level, and subscribed layers when available — cheap
    additions since dept.yaml already tracks them."""
    from console.services import github_reader

    dept_yaml = {
        "department": {"slug": "ben", "level": "ops", "model": "opus[1m]",
                        "runtime": "claude"},
        "hierarchy": {"level": "ops"},
        "layers": {"subscribed": [1, 2, 3, 4]},
    }
    info = github_reader.load_agent_model_info(dept_yaml)
    assert info["runtime"] == "claude"
    assert info["hierarchy_level"] == "ops"
    assert info["layers"] == [1, 2, 3, 4]


def test_load_agent_model_info_runtime_defaults_to_claude_when_absent():
    """No runtime declared -> defaults to 'claude' (the platform default —
    every dept.yaml-driven agent is a Claude Code process unless it opts
    into a different runtime explicitly)."""
    from console.services import github_reader

    dept_yaml = {"department": {"slug": "x", "level": "ops"}}
    info = github_reader.load_agent_model_info(dept_yaml)
    assert info["runtime"] == "claude"


def test_dept_detail_page_renders_model_panel_with_metadata(client, monkeypatch):
    """End-to-end: a dept.yaml with model+runtime+hierarchy+layers renders
    all of it into the page (the compact info panel)."""
    from console.services import github_reader

    original = github_reader.load_dept_yaml

    def _patched(slug):
        doc = original(slug)
        if slug == "fixture" and isinstance(doc, dict):
            doc["department"]["model"] = "opus[1m]"
            doc["department"]["runtime"] = "claude"
            doc["hierarchy"] = {"level": "ops"}
            doc["layers"] = {"subscribed": [1, 2, 3, 4]}
        return doc

    monkeypatch.setattr(github_reader, "load_dept_yaml", _patched)
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "opus[1m]" in body
    assert "claude" in body.lower()
