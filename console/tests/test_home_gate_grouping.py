"""
test_home_gate_grouping.py — Item E1 polish.

Polish from the Bureau-de-Cadre smoke ({{OPERATOR}} flag, msg 2709):
"Pourquoi elles sont pas ensemble dans « 2 research decisions »? C'est la
même catégorie".

The home (/) page must group pending gates by (dept_slug, kind) tuple. Each
group renders as a single décision-card with:
  - title containing the count + humanized kind label (in French)
  - subtitle/body containing the humanized kind label
  - a CTA "Voir les [N] →" pointing to /dept/<slug>

Single-item groups continue to render as before (one card per gate, with
the "Ouvrir →" link pointing directly at the gate page).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_root(tmp_path: Path) -> Path:
    """Bare multi-dept root. Caller adds depts + gates."""
    root = tmp_path / "depts"
    root.mkdir()
    return root


def _make_live_dept(root: Path, slug: str, display: str) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops", "mandate": f"do {slug}"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": display,
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "queues" / "gates").mkdir(parents=True)
    return repo


def _write_gate(repo: Path, gate_id: str, kind: str, risk: str = "low") -> None:
    (repo / "queues" / "gates" / f"{gate_id}.yaml").write_text(
        yaml.safe_dump({
            "id": gate_id, "kind": kind, "source_layer": 2,
            "target_layer": 3, "risk_level": risk, "requires_human": True,
            "current_mode": "manual_required",
            "gate_policy_id": kind,
            "actions": ["approve", "reject", "modify", "defer"],
        }, sort_keys=False),
        encoding="utf-8",
    )


def _build_client(monkeypatch, root: Path):
    """Rebuild app + client from a custom on-disk root."""
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


# ──────────────────────────────────────────────────────────────────
# CASE 1 — same dept, same kind → 1 grouped card with count=2
# ──────────────────────────────────────────────────────────────────
def test_two_gates_same_dept_same_kind_render_as_single_card(monkeypatch, tmp_path):
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "gate-notif-test-002", "research_decision")
    _write_gate(repo, "gate-roundtrip-test-001", "research_decision")

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text

    # Exactly one card title for the group, containing the count and the
    # humanized kind. Format: "[N] décisions à prendre — [Nom]"
    assert "2 décisions à prendre" in body, \
        "expected grouped header '2 décisions à prendre' " \
        f"in body, got: {body[:2000]}"
    # The two gate ids must no longer leak into the home card body
    assert "gate-notif-test-002" not in body
    assert "gate-roundtrip-test-001" not in body
    # The CTA must offer to view all N
    assert "Voir les 2" in body, "expected CTA 'Voir les 2 →'"
    # The CTA must deep-link straight to the batch triage view for this
    # group, matching the dept page's own group-card link (#449) — not the
    # dept page, which would make the operator re-find the decisions.
    assert 'href="/gate/fixture/kind/research_decision"' in body, \
        "grouped card must deep-link to /gate/<slug>/kind/<kind>"


# ──────────────────────────────────────────────────────────────────
# CASE 2 — same dept, different kinds → 2 cards
# ──────────────────────────────────────────────────────────────────
def test_two_gates_same_dept_different_kinds_render_two_cards(monkeypatch, tmp_path):
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "g-a", "research_decision")
    _write_gate(repo, "g-b", "trade_order")

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    body = r.text

    # Two distinct kinds — no grouping merge, so no "2 décisions" header
    # (each group has size 1 and renders as a single-item card).
    assert "2 décisions à prendre" not in body
    # Both gate ids visible on home (single-item case keeps the direct link)
    assert "g-a" in body
    assert "g-b" in body


# ──────────────────────────────────────────────────────────────────
# CASE 3 — different depts, same kind → 2 cards (dept boundary)
# ──────────────────────────────────────────────────────────────────
def test_two_gates_different_depts_same_kind_render_two_cards(monkeypatch, tmp_path):
    root = _make_root(tmp_path)
    repo_a = _make_live_dept(root, "alpha", "Alpha")
    repo_b = _make_live_dept(root, "beta", "Beta")
    _write_gate(repo_a, "g-a", "research_decision")
    _write_gate(repo_b, "g-b", "research_decision")

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    body = r.text

    # Grouping must respect dept boundary
    assert "2 décisions à prendre" not in body
    # Each gate id rendered (single-item links survive)
    assert "g-a" in body
    assert "g-b" in body


# ──────────────────────────────────────────────────────────────────
# CASE 4 — single gate → backward-compat single-item card
# ──────────────────────────────────────────────────────────────────
def test_single_gate_renders_backward_compat_single_card(monkeypatch, tmp_path):
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "echo-1", "echo_action")

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    body = r.text

    # The single gate id is still rendered (direct link preserved)
    assert "echo-1" in body
    # No grouped-card header (count would be 1, which is the single-card shape)
    assert "1 décisions à prendre" not in body, \
        "single-gate must NOT render group-style header"


# ──────────────────────────────────────────────────────────────────
# CASE 5 — humanized French kind label, not the raw enum slug
# ──────────────────────────────────────────────────────────────────
def test_group_card_uses_humanized_french_kind_label(monkeypatch, tmp_path):
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "p1", "prospect_dm")
    _write_gate(repo, "p2", "prospect_dm")

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    body = r.text

    # The humanized French label for 'prospect_dm' is 'DM à approuver'
    assert "DM à approuver" in body, \
        "humanized French label 'DM à approuver' missing for prospect_dm"
    # The raw enum slug must NOT appear in the group card's VISIBLE prose
    # (title/body text) — the home card is operator-prose, no enum slugs.
    # It legitimately appears inside the card's href now (#449: deep-links
    # to /gate/<slug>/kind/<kind> instead of /dept/<slug>), so we isolate
    # the title+body text rather than grepping the whole page body.
    import re
    title_match = re.search(
        r'<h3 class="decision-card-title">(.*?)</h3>', body, re.DOTALL,
    )
    body_match = re.search(
        r'<p class="decision-card-body">(.*?)</p>', body, re.DOTALL,
    )
    assert title_match and body_match, "group card title/body not found in response"
    visible_prose = title_match.group(1) + body_match.group(1)
    assert "prospect_dm" not in visible_prose, \
        "raw enum 'prospect_dm' must NOT appear in the card's visible prose"
