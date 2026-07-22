"""
test_dept_moments_flow.py — card #731 (cockpit rendering, Miranda's page),
extended by card #730/#731 follow-up (render/layout fix, 2026-07-22).

Rendering fixes on /dept/<slug> (shared dept_detail.html, fleet-wide):
  1. The "Ses 4 moments" section opens with a legible flow strip
     (L1 → L2 → L3 → L4 ↺) whose nodes anchor to the detailed moment cards.
     For Miranda (dept `content`) the couches follow her canonical state map
     « Miranda — Reconstruction (état désiré) » (miranda-rebuild):
     Observe / Orient + Draft / Act = Publish / Debrief.
  2. [#730/#731 follow-up] `.moments-flow` never overflows the viewport —
     bounded + horizontally scrollable strip.
  3. [#730/#731 follow-up] The detailed per-moment view (`.moments-list`)
     is wrapped in a fit-to-width frame with a "Plein écran" toggle.
  4. [#730/#731 follow-up] Miranda's « voice-change-map » section — unused
     (Jade: not useful) — is REMOVED (was: rendered natively inside her
     page via partials/_voice_change_map.html; that partial is deleted).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def test_moments_flow_has_overflow_safeguard():
    """Card #730/#731 follow-up: the flow strip must never push the page
    wider than the viewport — it is bounded to max-width: 100% AND clips/
    scrolls horizontally within its own box, so a narrow screen scrolls
    the strip, never the page."""
    css = Path(__file__).resolve().parents[1] / "static" / "style.css"
    text = css.read_text(encoding="utf-8")
    assert ".moments-flow" in text
    # isolate JUST the .moments-flow rule block (up to its closing brace),
    # not the sibling .mflow-* rules that follow it — a pre-existing
    # min-width:0 on .mflow-step must not make this test pass by accident.
    after = text.split(".moments-flow", 1)[1]
    rule_block = after[: after.index("}") + 1]
    assert "max-width: 100%" in rule_block
    assert "overflow-x: auto" in rule_block


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


def test_voice_change_map_removed():
    """Card #730/#731 (3): 'La voix — carte des changements' is unused
    (Jade: not useful) — removed from the shared dept_detail.html and its
    partial deleted outright."""
    tmpl = Path(__file__).resolve().parents[1] / "templates" / "dept_detail.html"
    text = tmpl.read_text(encoding="utf-8")
    assert "_voice_change_map.html" not in text
    assert "La voix — carte des changements" not in text
    partial = Path(__file__).resolve().parents[1] / "templates" / "partials" / "_voice_change_map.html"
    assert not partial.exists()


def test_moments_detail_has_fitwidth_frame_and_fullscreen_toggle(client):
    """Card #730/#731: the detailed per-moment view (`.moments-list`) is
    wrapped in a bounded fit-to-width frame with a 'Plein écran' toggle,
    so it never overflows and can be expanded on demand. Every dept
    (shared dept_detail.html), checked via the fixture dept — 200, no
    auth needed (see test_dept_page_has_moments_flow_strip above)."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    html = r.text
    assert 'class="moments-frame"' in html
    assert 'data-moments-fullscreen' in html            # the toggle button
    assert 'Plein écran' in html


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


# ─── 2. Miranda (dept content) — canonical couches ───────────────────────

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


def test_content_dept_has_no_voice_map(fixture_root, client):
    """[#730/#731 follow-up] The voice-change-map section is removed
    fleet-wide, INCLUDING on Miranda's own page (was: content-dept-only
    native render; Jade: not useful — see test_voice_change_map_removed
    for the template-level guard)."""
    _add_content_dept(fixture_root)
    body = client.get("/dept/content").text
    assert "carte des changements" not in body.lower()
    assert 'class="vcm"' not in body
