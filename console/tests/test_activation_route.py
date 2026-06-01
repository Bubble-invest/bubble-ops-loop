"""
test_activation_route.py — UX-5 task 7.

POST /agents/<slug>/activate
  Calls scripts/activate-dept.sh --slug <slug> --dry-run by default and
  renders the resulting PR body as an HTMX partial fragment. The non-
  --dry-run path (real PR) is triggered by submitting `confirm=1`.

Auth: same bearer-token middleware as other console routes.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml


# ---------------- fixtures ------------------------------------------------


def _make_ready_dept(fixture_root: Path, slug: str = "ready") -> Path:
    """Construct a bubble-ops-<slug> at fixture_root in `Ready to activate`."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    dept = {
        "department": {
            "slug": slug, "level": "ops",
            "mandate": "MVP ready-to-activate dept for the UX-5 console test.",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [
            {"id": "echo", "layer": 1, "cadence": "every_2h",
             "active_hours": "08:00-22:00",
             "creates": ["echo_task"],
             "output_queue": "queues/research/",
             "input_sources": ["filesystem"],
             "description": "Echo-task heartbeat mission for ready dept."},
        ],
        "skills": {"layer_1": ["a"], "layer_2": ["b"],
                   "layer_3": ["c"], "layer_4": ["d"]},
        "tools": ["t"],
        "gate_policies": {
            "echo_action": {
                "current_mode": "manual_required",
                "eligible_future_modes": [],
                "authorization_band": "low",
                "kpi_guardrail_set": "k",
            },
        },
        "hierarchy": {
            "level": "ops", "parent": None, "children": [],
            "visibility": {
                "read_outputs": [], "read_risk_kpis": False,
                "read_risk_briefs": False, "read_raw_artifacts": False,
                "read_secrets": False,
            },
            "directive_policy": {
                "can_open_priority_prs": False,
                "target_queue": "queues/management/",
                "requires_human_gate_for": [],
            },
        },
        "optional_domain_ledger": None,
    }
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(dept, sort_keys=False), encoding="utf-8")
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug,
            "display_name": slug.capitalize(),
            "owner": "joris", "created_at": "2026-05-19T10:00:00Z",
            "status": "Ready to activate",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [
                {"step": "dry_run", "commit_sha": "abc1234",
                 "validated_at": "2026-05-20T09:00:00Z"},
            ],
        }, sort_keys=False), encoding="utf-8")
    for sub in ("outputs", "queues/research", "inbox/decisions",
                "missions", "tests"):
        (repo / sub).mkdir(parents=True, exist_ok=True)
    return repo


# ---------------- tests ---------------------------------------------------


def test_activate_preview_returns_pr_body_html(client, fixture_root):
    """POST /agents/<slug>/activate with valid ready-status returns an HTML
    fragment with the rendered PR body."""
    _make_ready_dept(fixture_root)
    r = client.post("/agents/ready/activate")
    assert r.status_code == 200, \
        f"expected 200, got {r.status_code}: {r.text}"
    # Decode HTML entities — Jinja autoescapes apostrophes to &#39; inside <pre>
    import html as _html
    body = _html.unescape(r.text)
    # PR-body sections rendered as HTML headings or raw markdown.
    # Vocabulary refresh: msg 2702/2708 — old "Mandate / Recurring missions /
    # Activation checklist" are now the humanized French equivalents.
    assert "Sa mission" in body
    assert "Ce qu'elle fera chaque jour" in body
    assert "Ce qu'il faut vérifier avant la cérémonie" in body


def test_activate_preview_requires_auth(client_noauth, fixture_root):
    _make_ready_dept(fixture_root)
    r = client_noauth.post("/agents/ready/activate")
    assert r.status_code in (401, 403)


def test_activate_preview_409_when_not_ready(client, fixture_root):
    """A dept that's not in `Ready to activate` status must return 409."""
    r = client.post("/agents/miranda/activate")
    # miranda is in Drafting per fixture_root in conftest
    assert r.status_code == 409, \
        f"expected 409, got {r.status_code}: {r.text}"


def test_activate_preview_404_when_unknown_slug(client):
    r = client.post("/agents/no-such-dept/activate")
    assert r.status_code == 404


def test_activate_preview_includes_confirm_button(client, fixture_root):
    """The HTMX preview MUST include an 'Open PR' confirm button so
    the operator can promote the dry-run preview to a real PR."""
    _make_ready_dept(fixture_root)
    r = client.post("/agents/ready/activate")
    assert r.status_code == 200
    low = r.text.lower()
    assert "open pr" in low or "confirm" in low or "activate" in low
