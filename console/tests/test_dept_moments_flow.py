"""
test_dept_moments_flow.py — card #731 (cockpit rendering, Miranda's page).

Two rendering fixes on /dept/<slug>:
  1. The "Ses 4 moments" section opens with a legible flow strip
     (L1 → L2 → L3 → L4 ↺) whose nodes anchor to the detailed moment cards.
     For Miranda (dept `content`) the couches follow her canonical state map
     « Miranda — Reconstruction (état désiré) » (miranda-rebuild):
     Observe / Orient + Draft / Act = Publish / Debrief.
  2. Miranda's « voice-change-map » (files/skills touched by a voice change)
     renders natively inside her page (partials/_voice_change_map.html) —
     not as a loose HTML file in her outputs. Content-dept only.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _add_content_dept(root: Path) -> None:
    """Drop a minimal LIVE bubble-ops-content (Miranda) repo into the disk
    root so /dept/content renders. Mirrors the fixture-dept shape in
    conftest.fixture_root; dept_registry re-scans the root on each request."""
    repo = root / "bubble-ops-content"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "content", "display_name": "Miranda",
                           "level": "ops", "host": "local",
                           "mandate": "Run Bubble Invest's content publishing"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "content", "display_name": "Miranda",
            "owner": "operator", "created_at": "2026-07-01T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-07-01T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "outputs").mkdir()


# ─── 1. Flow strip — every dept ──────────────────────────────────────────

def test_dept_page_has_moments_flow_strip(client):
    """The 4-moments section opens with the L1→L4 flow strip; each node
    anchors to its detailed moment card below."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert 'class="moments-flow"' in body, "flow strip must render"
    for n in (1, 2, 3, 4):
        assert f'href="#moment-{n}"' in body, f"node L{n} must anchor down"
        assert f'id="moment-{n}"' in body, f"moment card {n} must be anchorable"
    # the cycle note makes the loop-back (L4 ↺ L1) explicit
    assert "la boucle repart" in body


def test_generic_dept_keeps_fleet_moment_names(client):
    """A non-content dept keeps the generic fleet couche names."""
    body = client.get("/dept/fixture").text
    assert "Le matin" in body and "Le débrief du soir" in body
    # and does NOT get Miranda's canonical couches
    assert "Act = Publish" not in body


def test_generic_dept_has_no_voice_map(client):
    """The voice-change-map is Miranda's — it must not leak onto other
    dept pages."""
    body = client.get("/dept/fixture").text
    assert "carte des changements" not in body.lower()
    assert 'class="vcm"' not in body


# ─── 2. Miranda (dept content) — canonical couches + voice map ───────────

def test_content_dept_uses_canonical_couches(fixture_root, client):
    """Miranda's flow strip + moment cards follow the canonical
    « Miranda — Reconstruction (état désiré) » couches."""
    _add_content_dept(fixture_root)
    r = client.get("/dept/content")
    assert r.status_code == 200
    body = r.text
    for name in ("Observe", "Orient + Draft", "Act = Publish", "Debrief"):
        assert name in body, f"canonical couche '{name}' must render"
    # generic fleet names are replaced on HER page. (NOT "Le matin" — the
    # sidebar's home link in base.html is "☀ Le matin" on every page.)
    assert "Le débrief du soir" not in body


def test_content_dept_renders_voice_map_natively(fixture_root, client):
    """The voice-change-map renders inside Miranda's page: section heading,
    the truth/modify/keep legend, and the source-of-truth file."""
    _add_content_dept(fixture_root)
    body = client.get("/dept/content").text
    assert "La voix — carte des changements" in body
    assert 'class="vcm"' in body, "native partial must be included"
    assert "Source de vérité" in body
    assert "shared/BRAND.md" in body
    # self-contained: the partial ships no external script/CDN reference
    assert "cdn." not in body.split('class="vcm"')[1].lower()
