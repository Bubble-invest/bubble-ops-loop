"""
test_onboarding_pill_live.py — Bug A2.

Header pill on /agents/<slug>/onboarding wrongly reads
"À ÉCLORE — 100% prêt" when the dept is already Live. It should read
"LIVE" (matching the green pill style used on /, /agents, and /dept/<slug>).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_live_dept(fixture_root: Path, slug: str = "live-pill") -> Path:
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "live dept for A2 regression"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": "LivePill",
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    return repo


def test_onboarding_pill_says_live_for_live_dept(client, fixture_root):
    """Live dept must render the green LIVE pill, not the eclore pill."""
    _make_live_dept(fixture_root)
    r = client.get("/agents/live-pill/onboarding")
    assert r.status_code == 200
    body = r.text
    # Look for the live pill in the header area. The styling class is
    # `pill-live` (lime/green), matching home/agents/dept_detail.
    assert "pill-live" in body, \
        "expected 'pill-live' class in header pill for Live dept"
    # And the misleading 'à éclore — 100% prêt' text must NOT show up
    low = body.lower()
    assert "à éclore — 100% prêt" not in low and \
           "a eclore - 100% pret" not in low, \
        "Live dept must not show 'à éclore — 100% prêt' pill"


def test_onboarding_pill_still_shows_eclore_for_drafting_dept(client):
    """Regression guard: the existing miranda (Drafting) must still show
    the à-éclore pill with its percent."""
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    assert "pill-eclore" in body
    assert "à éclore" in body.lower() or "a eclore" in body.lower()
