"""
Phase G3 — real-time HTMX-polled onboarding console.

Tests:
  - GET /agents/<slug>/onboarding/timeline returns 200 + an HTML fragment
    (not a full page) with all 7 step bullets;
  - the fragment reflects edits to STATE.yaml on disk;
  - the artifacts fragment lists files present in the dept dir;
  - the "dernier signe de vie" line shows the latest commit message;
  - the fragment uses HTMX-friendly markup (no <html>, no <body>).
"""
from __future__ import annotations

from pathlib import Path

import re
import subprocess
import yaml


def _is_html_fragment(text: str) -> bool:
    """A fragment must NOT contain <html> or <body> tags."""
    low = text.lower()
    return "<html" not in low and "<body" not in low


# ---------------------------------------------------------------------------
# Timeline fragment
# ---------------------------------------------------------------------------

def test_timeline_fragment_returns_200_and_is_fragment(client):
    r = client.get("/agents/miranda/onboarding/timeline")
    assert r.status_code == 200, r.text
    assert _is_html_fragment(r.text), \
        "timeline fragment must NOT include <html>/<body>"


def test_timeline_fragment_contains_all_7_step_bullets(client):
    r = client.get("/agents/miranda/onboarding/timeline")
    body = r.text
    # All 7 step_ids must appear as data-step="..." attributes
    for step_id in ("mandate", "missions", "layers", "skills_tools",
                    "gates_kpis", "dry_run", "activation"):
        assert f'data-step="{step_id}"' in body, \
            f"timeline fragment missing data-step='{step_id}'"


def test_timeline_fragment_reflects_state_yaml_changes(
    client, fixture_root: Path
):
    """If we edit miranda's STATE.yaml to add a new validated step, a fresh
    GET to the fragment reflects it (no caching surprises)."""
    state_p = fixture_root / "bubble-ops-miranda" / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_p.read_text(encoding="utf-8"))
    # Originally miranda has 3 validated (mandate/missions/layers). Add #4.
    doc["validated_steps"].append("skills_tools")
    state_p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    r = client.get("/agents/miranda/onboarding/timeline")
    body = r.text
    # Tolerate whitespace/newlines between attributes (Jinja indentation).
    assert re.search(
        r'data-step="skills_tools"\s+data-step-num="4"\s+data-status="validated"',
        body,
    ), f"after editing STATE.yaml, the fragment should mark skills_tools as validated; got:\n{body[:2000]}"


# ---------------------------------------------------------------------------
# Artifacts fragment
# ---------------------------------------------------------------------------

def test_artifacts_fragment_returns_200_and_is_fragment(client):
    r = client.get("/agents/miranda/onboarding/artifacts-fragment")
    assert r.status_code == 200, r.text
    assert _is_html_fragment(r.text), \
        "artifacts fragment must NOT include <html>/<body>"


def test_artifacts_fragment_lists_real_files(client, fixture_root: Path):
    """The fragment must mention the dept.yaml.draft and any extra files
    actually present in the miranda dir."""
    # Drop a new MANDATE.md file into the miranda repo.
    (fixture_root / "bubble-ops-miranda" / "MANDATE.md").write_text(
        "test mandate\n", encoding="utf-8")
    r = client.get("/agents/miranda/onboarding/artifacts-fragment")
    body = r.text.lower()
    assert "dept.yaml.draft" in body or "dept.yaml" in body, \
        "artifacts fragment must mention the dept.yaml(.draft) file"
    # The new MANDATE.md should appear somewhere.
    assert "mandate" in body, \
        "artifacts fragment must list newly-added MANDATE.md"


# ---------------------------------------------------------------------------
# "Dernier signe de vie" — latest commit message line
# ---------------------------------------------------------------------------

def test_heartbeat_fragment_includes_latest_commit_message(
    client, fixture_root: Path
):
    """The fragment must include the latest commit message of the dept repo
    when the repo is a real git repo."""
    # Init miranda's repo as a real git repo with one commit so git log works.
    miranda = fixture_root / "bubble-ops-miranda"
    subprocess.run(["git", "init", "-q"], cwd=str(miranda), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"],
                   cwd=str(miranda), check=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(miranda), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(miranda), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "mandate: validated by joris"],
        cwd=str(miranda), check=True,
    )
    r = client.get("/agents/miranda/onboarding/heartbeat-fragment")
    assert r.status_code == 200
    body = r.text
    assert _is_html_fragment(body)
    assert "mandate: validated by joris" in body, \
        "heartbeat fragment must include the latest commit message"
    # Must include some relative-time wording
    assert "signe de vie" in body.lower() or "il y a" in body.lower() or \
           "à l'instant" in body.lower(), \
        "heartbeat fragment must include a 'dernier signe de vie' style line"


# ---------------------------------------------------------------------------
# Full onboarding page wires HTMX polling into the timeline
# ---------------------------------------------------------------------------

def test_onboarding_page_wires_htmx_polling_for_timeline(client):
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # The timeline container must declare hx-get + hx-trigger="every Xs"
    assert 'hx-get="/agents/miranda/onboarding/timeline"' in body, \
        "onboarding page must wire hx-get to the timeline fragment"
    assert re.search(r'hx-trigger="[^"]*every \d+s', body), \
        "onboarding page must wire hx-trigger='every Ns' for live polling"
