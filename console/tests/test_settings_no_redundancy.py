"""
test_settings_no_redundancy.py — Item E5 polish.

The Bureau-de-Cadre smoke grep saw 'Décisions' appear 2x in the settings
template SOURCE file. Investigation result: the two occurrences live in
the IF-branch (gate_policies present → 'Sur les décisions de type X') and
the ELSE-branch (no policies → 'Aucun type de décision déclaré') — they
are mutually exclusive at render time. No actual duplication on the
rendered page.

These tests pin that behaviour: on a dept WITH policies, the response
contains the policy-prose, NOT the empty fallback; vice-versa for a
dept WITHOUT policies. Each branch shows 'décision' exactly once, in
semantically distinct contexts.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _build_root_with_policies(tmp_path: Path, *, with_policies: bool) -> Path:
    """Build a dept fixture, controlling whether gate_policies are present."""
    root = tmp_path / "settings-depts"
    root.mkdir()
    repo = root / "bubble-ops-fixture"
    repo.mkdir()
    dept_doc: dict = {
        "department": {"slug": "fixture", "level": "ops", "mandate": "x"},
        "layers": {"subscribed": [1, 2, 3, 4]},
    }
    if with_policies:
        dept_doc["gate_policies"] = {
            "echo_action": {
                "current_mode": "manual_required",
                "eligible_future_modes": ["auto_if_policy_passed"],
            },
        }
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(dept_doc, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "fixture", "display_name": "Fixture",
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    return root


def _build_client(monkeypatch, root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_settings_with_policies_shows_policy_prose_only(monkeypatch, tmp_path):
    """On a dept WITH gate_policies, the page must show
    'Sur les décisions de type X' and NOT the empty fallback."""
    root = _build_root_with_policies(tmp_path, with_policies=True)
    c = _build_client(monkeypatch, root)
    r = c.get("/settings/fixture")
    assert r.status_code == 200
    body = r.text

    assert "Sur les décisions de type" in body, \
        "settings with policies must surface 'Sur les décisions de type X'"
    # The fallback must NOT be rendered (the {% else %} branch is dead here)
    assert "Aucun type de décision déclaré" not in body, \
        "fallback must NOT render when policies are present"


def test_settings_without_policies_shows_only_fallback(monkeypatch, tmp_path):
    """On a dept WITHOUT gate_policies, the page must show the fallback
    'Aucun type de décision déclaré' and NOT the policy prose."""
    root = _build_root_with_policies(tmp_path, with_policies=False)
    c = _build_client(monkeypatch, root)
    r = c.get("/settings/fixture")
    assert r.status_code == 200
    body = r.text

    assert "Aucun type de décision déclaré" in body, \
        "settings without policies must surface the empty-state fallback"
    assert "Sur les décisions de type" not in body, \
        "policy-prose must NOT render when no policies exist"


def test_settings_no_duplicate_decision_heading_in_rendered_body(client):
    """In the actual rendered HTML for /settings/fixture (which has 1
    policy), 'Sur les décisions de type' appears exactly once per policy.
    Make sure we did not accidentally double-render the section heading."""
    r = client.get("/settings/fixture")
    body = r.text
    # The fixture has exactly 1 policy (echo_action). So the per-policy
    # heading 'Sur les décisions de type' must appear exactly once.
    assert body.count("Sur les décisions de type") == 1, (
        "expected exactly 1 per-policy heading, "
        f"got {body.count('Sur les décisions de type')}"
    )
