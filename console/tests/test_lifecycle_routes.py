"""
test_lifecycle_routes.py — Sprint Lifecycle Deliverable D.

Console additions for cancel-eclosion + retire-dept:
  - GET  /agents                                 — 'Anciens collègues' section
                                                   lists Cancelled + Retired
  - POST /agents/<slug>/cancel-eclosion          — invokes cancel_eclosion()
  - POST /agents/<slug>/retire                   — invokes retire_dept()
  - GET  /agents/<slug>/onboarding               — Cancelled banner instead
                                                   of 7-step timeline
  - GET  /dept/<slug>                            — Retired/Cancelled banner
                                                   ("ne fait plus partie...")
"""
from __future__ import annotations

from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Helpers — materialize Cancelled / Retired depts in fixture_root.
# ---------------------------------------------------------------------------

def _make_cancelled_dept(fixture_root: Path, slug: str = "cancelled-one",
                         display_name: str = "CancelledOne") -> Path:
    """A bubble-ops-<slug> with STATE.yaml::status=Cancelled."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml.draft").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "An abandoned eclosure for the test."}
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug,
            "display_name": display_name,
            "owner": "joris", "created_at": "2026-05-21T08:00:00Z",
            "status": "Cancelled",
            "validated_steps": ["mandate"],
            "last_updated_at": "2026-05-21T09:00:00Z",
            "cancelled_at": "2026-05-21T09:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    return repo


def _make_retired_dept(fixture_root: Path, slug: str = "retired-one",
                       display_name: str = "RetiredOne") -> Path:
    """A bubble-ops-<slug> with STATE.yaml::status=Retired + dept.yaml status=retired."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "status": "retired",
                           "mandate": "A retired dept for the test."},
            "layers": {"subscribed": [1]},
            "recurring_missions": [],
            "skills": {},
            "tools": [],
            "gate_policies": {},
            "hierarchy": {
                "level": "ops", "parent": "tony", "children": [],
                "visibility": {"read_outputs": [], "read_risk_kpis": False,
                               "read_risk_briefs": False,
                               "read_raw_artifacts": False,
                               "read_secrets": False},
                "directive_policy": {"can_open_priority_prs": False,
                                     "target_queue": None,
                                     "requires_human_gate_for": []},
            },
            "optional_domain_ledger": None,
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug,
            "display_name": display_name,
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": "Retired",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-21T11:00:00Z",
            "retired_at": "2026-05-21T11:00:00Z",
            "retired_reason": "Mission accomplished",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    return repo


def _make_live_dept(fixture_root: Path, slug: str = "live-one",
                    display_name: str = "LiveOne") -> Path:
    """A bubble-ops-<slug> in Live status (for testing retire route)."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "status": "live",
                           "mandate": "A live dept for the retire route test."},
            "layers": {"subscribed": [1]},
            "recurring_missions": [],
            "skills": {},
            "tools": [],
            "gate_policies": {},
            "hierarchy": {
                "level": "ops", "parent": "tony", "children": [],
                "visibility": {"read_outputs": [], "read_risk_kpis": False,
                               "read_risk_briefs": False,
                               "read_raw_artifacts": False,
                               "read_secrets": False},
                "directive_policy": {"can_open_priority_prs": False,
                                     "target_queue": None,
                                     "requires_human_gate_for": []},
            },
            "optional_domain_ledger": None,
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug,
            "display_name": display_name,
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-21T11:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# 10 tests.
# ---------------------------------------------------------------------------

def test_agents_page_shows_anciens_collegues_section(client, fixture_root):
    """When at least one Cancelled/Retired dept exists, /agents shows the
    'Anciens collègues' section."""
    _make_cancelled_dept(fixture_root)
    _make_retired_dept(fixture_root)
    r = client.get("/agents")
    assert r.status_code == 200, r.text
    assert "Anciens collègues" in r.text, r.text
    # The two depts are listed in the section.
    assert "CancelledOne" in r.text
    assert "RetiredOne" in r.text


def test_cancel_eclosion_route_returns_fragment(client, fixture_root,
                                                  monkeypatch):
    """POST /agents/<slug>/cancel-eclosion on a non-Live dept returns 200
    and a fragment confirming cancellation."""
    # fixture_root in conftest has 'miranda' in Drafting — perfect.
    # We must monkeypatch subprocess.run so the script doesn't try to SSH.
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/miranda/cancel-eclosion")
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    low = r.text
    # The fragment should mention cancellation in French Bureau-de-Cadre voice.
    assert ("annulée" in low.lower() or "annulé" in low.lower()
            or "Éclosion annulée" in low or "Cancelled" in low), low


def test_cancel_eclosion_on_live_dept_returns_409(client, fixture_root,
                                                    monkeypatch):
    """POST /agents/<slug>/cancel-eclosion on a Live dept returns 409."""
    _make_live_dept(fixture_root)
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/live-one/cancel-eclosion")
    assert r.status_code == 409, f"got {r.status_code}: {r.text}"
    assert "retire-dept" in r.text.lower(), r.text


def test_retire_route_returns_fragment(client, fixture_root, monkeypatch):
    """POST /agents/<slug>/retire on a Live dept returns 200 + fragment."""
    _make_live_dept(fixture_root)
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/live-one/retire")
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    # The fragment should show the farewell preview.
    assert "Merci" in r.text or "retraite" in r.text.lower(), r.text


def test_retire_route_on_non_live_dept_returns_409(client, fixture_root,
                                                     monkeypatch):
    """POST /agents/<slug>/retire on a Drafting dept returns 409."""
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/miranda/retire")
    assert r.status_code == 409, f"got {r.status_code}: {r.text}"
    assert "cancel-eclosion" in r.text.lower(), r.text


def test_cancel_fragment_includes_botfather_instructions(client, fixture_root,
                                                           monkeypatch):
    """The HTMX cancellation response includes the BotFather operator
    instructions (the manual step we can't automate)."""
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/miranda/cancel-eclosion")
    assert r.status_code == 200, r.text
    assert "BotFather" in r.text, r.text


def test_retire_fragment_includes_final_telegram_preview(client, fixture_root,
                                                          monkeypatch):
    """The HTMX retirement response includes the farewell Telegram message
    preview (so {{OPERATOR}} sees what got sent)."""
    _make_live_dept(fixture_root)
    _patch_lifecycle_subprocess(monkeypatch)

    r = client.post("/agents/live-one/retire")
    assert r.status_code == 200, r.text
    # The farewell message includes display name + 'Merci' + 'retraite'.
    assert "LiveOne" in r.text, r.text
    assert "Merci" in r.text, r.text
    assert "retraite" in r.text.lower(), r.text


def test_anciens_collegues_uses_muted_variant(client, fixture_root):
    """The 'Anciens collègues' section uses a muted color variant — assert
    via a marker class on the rendered HTML."""
    _make_retired_dept(fixture_root)
    r = client.get("/agents")
    assert r.status_code == 200
    # The section / cards have a muted class so they're visually de-emphasized.
    assert "collegue-row--ancien" in r.text or "anciens-collegues" in r.text, (
        "Expected a muted variant class for the Anciens collègues section; "
        f"body excerpt: {r.text[:1500]}"
    )


def test_cancelled_onboarding_page_shows_cancellation_banner(
    client, fixture_root,
):
    """GET /agents/<slug>/onboarding for a Cancelled dept shows the
    'Éclosion annulée le ...' banner instead of the 7-step timeline."""
    _make_cancelled_dept(fixture_root)
    r = client.get("/agents/cancelled-one/onboarding")
    assert r.status_code == 200, r.text
    assert "Éclosion annulée" in r.text, r.text


def test_retired_dept_detail_shows_not_active_banner(client, fixture_root):
    """GET /dept/<slug> for a Retired dept shows the 'ne fait plus partie de
    l'équipe active' banner."""
    _make_retired_dept(fixture_root)
    r = client.get("/dept/retired-one")
    assert r.status_code == 200, r.text
    # The banner phrasing comes from the deliverable spec.
    assert ("ne fait plus partie" in r.text
            or "Ce collègue ne fait plus partie" in r.text), r.text


# ---------------------------------------------------------------------------
# Local subprocess patch — keeps the route from invoking real bash scripts.
# Instead, we monkeypatch the lifecycle lib functions to return a happy
# result deterministically.
# ---------------------------------------------------------------------------

def _patch_lifecycle_subprocess(monkeypatch):
    """Replace subprocess.run in the cancel_eclosion + retire_dept libs
    (used inside the route handlers) with a no-op happy returner."""
    import sys as _sys
    from pathlib import Path as _Path
    # Make scripts/lib importable.
    proj = _Path(__file__).resolve().parent.parent.parent
    libdir = proj / "scripts" / "lib"
    if str(libdir) not in _sys.path:
        _sys.path.insert(0, str(libdir))
    import cancel_eclosion  # type: ignore
    import retire_dept  # type: ignore

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake(*args, **kwargs):
        return _OK()

    monkeypatch.setattr(cancel_eclosion.subprocess, "run", _fake)
    monkeypatch.setattr(retire_dept.subprocess, "run", _fake)
