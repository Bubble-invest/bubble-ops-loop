"""Board #1673: cockpit session routes expose metadata, never transcript text.

All fixtures are synthetic.  These tests intentionally seed secret-shaped prose
and tool arguments so the pre-fix routes prove the real disclosure before the
privacy-safe replacement is implemented.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates


CONSOLE_DIR = Path(__file__).resolve().parent.parent
_PRIVATE_MARKERS = (
    "PRIVATE_USER_PROSE_1673",
    "PRIVATE_ASSISTANT_PROSE_1673",
    "PRIVATE_TOOL_ARG_1673",
)


def _write_transcript(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-10-01T12:00:00Z",
            "message": {"role": "user", "content": _PRIVATE_MARKERS[0]},
        },
        {
            "timestamp": "2026-10-01T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": _PRIVATE_MARKERS[1]},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"printf {_PRIVATE_MARKERS[2]}"},
                    },
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    fixed = datetime(2026, 10, 1, 12, 34, 56, tzinfo=timezone.utc).timestamp()
    os.utime(path, (fixed, fixed))


def _assert_no_transcript_content(body: str) -> None:
    for marker in _PRIVATE_MARKERS:
        assert marker not in body


def test_dept_session_fragment_replaces_text_with_activity_and_evidence(
    client, fixture_root: Path, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    transcript = home / ".claude/projects/-home-claude-agents-bubble-ops-fixture/s.jsonl"
    _write_transcript(transcript)

    layer = fixture_root / "bubble-ops-fixture/outputs/2026-10-01/1"
    layer.mkdir(parents=True, exist_ok=True)
    (layer / ".last-run").write_text("2026-10-01T11:45:00Z\n", encoding="utf-8")
    (layer / "deliverable-1673.txt").write_text("synthetic evidence\n", encoding="utf-8")

    from console.services import agent_session

    monkeypatch.setattr(
        agent_session,
        "read_session_turns",
        lambda *args, **kwargs: pytest.fail("cockpit route opened transcript content"),
    )

    response = client.get("/dept/fixture/session")

    assert response.status_code == 200
    _assert_no_transcript_content(response.text)
    assert "Personne" in response.text
    assert ">Fixture<" in response.text
    assert "Dernière activité" in response.text
    assert "2026-10-01T12:34:56Z" in response.text
    assert "En poste" in response.text
    assert "deliverable-1673.txt" in response.text
    assert ".last-run" not in response.text
    assert "logs.jsonl" not in response.text
    assert "/dept/fixture/output?f=" in response.text
    assert "Le texte des sessions n’est pas affiché" in response.text


def test_dept_page_labels_privacy_safe_activity_not_live_session(client):
    response = client.get("/dept/fixture")

    assert response.status_code == 200
    assert "Activité et preuves" in response.text
    assert "session en direct" not in response.text.lower()
    assert 'hx-get="/dept/fixture/session"' in response.text


@pytest.fixture
def concierge_client(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    for name in ("morty", "claudette"):
        (agents / name).mkdir(parents=True)

    project = agents / "morty/workspace/projects/privacy-safe"
    project.mkdir(parents=True)
    (project / "STATUS.md").write_text(
        "# Synthetic deliverable 1673\n"
        "**État:** prêt pour revue\n"
        "Evidence: https://example.test/evidence-1673\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    transcript = home / ".claude/projects/-home-claude-agents-morty/s.jsonl"
    _write_transcript(transcript)

    from console.services import concierge_reader

    real_get = concierge_reader.get_concierge
    real_projects = concierge_reader.list_projects
    monkeypatch.setattr(
        concierge_reader,
        "get_concierge",
        lambda name, agents_root=str(agents): real_get(name, str(agents)),
    )
    monkeypatch.setattr(
        concierge_reader,
        "read_recent_session",
        lambda *args, **kwargs: pytest.fail("cockpit route opened transcript content"),
    )
    monkeypatch.setattr(
        concierge_reader,
        "list_projects",
        lambda name, agents_root=str(agents): real_projects(name, str(agents)),
    )

    app = FastAPI()
    templates = Jinja2Templates(directory=str(CONSOLE_DIR / "templates"))
    from console.tests.conftest import install_base_template_globals

    install_base_template_globals(templates)
    app.state.templates = templates
    from console.routes import concierge as concierge_route

    app.include_router(concierge_route.router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/concierge/morty", "/concierge/morty/session"])
def test_concierge_entrypoints_hide_transcript_and_keep_status_deliverables(
    concierge_client, path
):
    response = concierge_client.get(path)

    assert response.status_code == 200
    _assert_no_transcript_content(response.text)
    assert "Personne" in response.text
    assert "Morty" in response.text
    assert "Dernière activité" in response.text
    assert "2026-10-01T12:34:56Z" in response.text
    assert "Synthetic deliverable 1673" in response.text
    assert "https://example.test/evidence-1673" in response.text
    assert "Le texte des sessions n’est pas affiché" in response.text
