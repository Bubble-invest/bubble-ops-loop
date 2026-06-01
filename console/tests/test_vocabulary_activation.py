"""
test_vocabulary_activation.py — UX-refresh.

POST /agents/<slug>/activate (HTMX fragment) reads as a 'Cérémonie
d'arrivée' letter, not a 'Dry-run PR preview'. The PR body still appears
verbatim (it's the activate-dept.sh shell output rendered as markdown),
but the surrounding chrome speaks in human voice.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_ready_dept(fixture_root: Path, slug: str = "ready") -> Path:
    """Mirror the helper from test_activation_route.py."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    dept = {
        "department": {
            "slug": slug, "level": "ops",
            "mandate": "MVP ready-to-activate dept for UX-refresh test.",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [
            {"id": "echo", "layer": 1, "cadence": "every_2h",
             "active_hours": "08:00-22:00",
             "creates": ["echo_task"],
             "output_queue": "queues/research/",
             "input_sources": ["filesystem"],
             "description": "Echo-task heartbeat mission."},
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


def test_activate_preview_uses_ceremonie_arrivee_title(client, fixture_root):
    _make_ready_dept(fixture_root)
    r = client.post("/agents/ready/activate")
    assert r.status_code == 200
    body = r.text
    assert "Cérémonie d'arrivée" in body or "Cérémonie d’arrivée" in body, \
        "activation preview must be titled 'Cérémonie d'arrivée de [Nom]'"


def test_activate_preview_uses_lettre_language(client, fixture_root):
    _make_ready_dept(fixture_root)
    r = client.post("/agents/ready/activate")
    body = r.text.lower()
    assert "lettre" in body, \
        "activation preview must talk about the 'lettre' to the team"


def test_activate_preview_button_says_envoyer_la_lettre(client, fixture_root):
    _make_ready_dept(fixture_root)
    r = client.post("/agents/ready/activate")
    body = r.text
    assert "Envoyer la lettre" in body, \
        "primary button must read 'Envoyer la lettre'"
