"""
test_home_excludes_anciens.py — home (/) must not list retired/cancelled
depts as active or surface their pending gates as outstanding decisions.

Regression: 2026-05-24 (Joris msg 3041) — after retiring fixture, the home
page still showed:
  - "9 décisions qu'on attend de toi" (fixture's 9 stale gates)
  - "1 collègue en éclosion" + Fixture rendered under "L'équipe" with
    the "En cours d'éclosion" pill

Root cause: home.py used `dept_registry.list_departments()` (ALL depts)
and the eclore branch was `not c["dept"].is_live`, which falsely included
anciens. The /agents page already had a separate `anciens` section
(handled correctly via `anciens_collegues()`); home / never did.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "depts"
    root.mkdir()
    return root


def _make_dept(root: Path, slug: str, display: str, status: str,
                with_gate: bool = False) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops", "mandate": "x"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": display,
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": status,
            "validated_steps": ["mandate", "missions", "layers"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "queues" / "gates").mkdir(parents=True)
    if with_gate:
        (repo / "queues" / "gates" / "stale-gate.yaml").write_text(
            yaml.safe_dump({
                "id": "stale-gate", "kind": "echo_action",
                "source_layer": 2, "target_layer": 3,
                "risk_level": "low", "requires_human": True,
                "current_mode": "manual_required",
                "gate_policy_id": "echo_action",
                "actions": ["approve", "reject"],
            }, sort_keys=False),
            encoding="utf-8",
        )
    return repo


def _build_client(monkeypatch, root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_retired_dept_gates_not_counted_in_total(monkeypatch, tmp_path):
    """A Retired dept with a stale gate must NOT contribute to
    `total_gates` on the home page."""
    root = _make_root(tmp_path)
    _make_dept(root, "ben", "Ben", status="Live", with_gate=False)
    _make_dept(root, "fixture", "Fixture", status="Retired", with_gate=True)
    c = _build_client(monkeypatch, root)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    # Should NOT mention "1 décision" or "stale-gate" anywhere — Ben has
    # none, Fixture is retired.
    assert "stale-gate" not in body, (
        "Retired dept's gates leaked into home decisions list"
    )
    # Body should NOT say "1 décision" in the hero counter
    assert "1 décision" not in body and "9 décision" not in body, (
        "Home hero counter should ignore retired-dept gates"
    )


def test_retired_dept_not_in_eclore_count(monkeypatch, tmp_path):
    """A Retired dept must NOT appear as `eclore` (in éclosion) on the
    home page hero counter."""
    root = _make_root(tmp_path)
    _make_dept(root, "ben", "Ben", status="Live", with_gate=False)
    _make_dept(root, "fixture", "Fixture", status="Retired", with_gate=False)
    c = _build_client(monkeypatch, root)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    # Hero should say "1 collègue en poste" (Ben) and zero éclore
    # (fixture is retired, not éclore).
    assert "0 collègue\xa0en éclosion" in body or "0 collègue en éclosion" in body \
        or ">0<" in body.replace(" ", ""), (
        f"Expected eclore_count=0 (fixture is Retired). Body excerpt: "
        f"{body[body.find('hero'):body.find('hero')+800]}"
    )


def test_retired_dept_not_in_team_section(monkeypatch, tmp_path):
    """A Retired dept must NOT appear under «L'équipe» on home — that
    section is for live + éclore only. Anciens are exposed on /agents."""
    root = _make_root(tmp_path)
    _make_dept(root, "ben", "Ben", status="Live", with_gate=False)
    _make_dept(root, "fixture", "Fixture", status="Retired", with_gate=False)
    c = _build_client(monkeypatch, root)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    # Fixture should not be linked from a "Continuer son éclosion" CTA on /
    # because it's retired.
    assert 'href="/agents/fixture/onboarding"' not in body, (
        "Retired Fixture should not have a 'continuer éclosion' link on home"
    )
