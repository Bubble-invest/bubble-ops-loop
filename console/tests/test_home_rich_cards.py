"""
test_home_rich_cards.py — board #533 guard.

The home "Cartes riches" section used to render a HARDCODED illustrative
INDA/EEM/SPY performance chart (static SVG polylines + a fake "+17.8%" note).
Joris confirmed it should show REAL pending gate-proposals instead.

`_rich_cards(columns)` builds a small, high-signal set of featured cards from
the gates ALREADY fetched onto `columns` (no redundant network scan) — each
carries a rendered-markdown thesis excerpt + a /gate/<slug>/<id> link. Empty →
the template shows a quiet placeholder, never the fake chart.
"""
from __future__ import annotations

from types import SimpleNamespace

from console.routes.home import _rich_cards


def _dept(slug: str, name: str):
    return SimpleNamespace(slug=slug, display_name=name)


def _col(dept, gates):
    return {"dept": dept, "gates": gates}


def _gate(gid, *, kind="trade_proposal", risk="medium", title=None, summary=None):
    g = {"id": gid, "kind": kind, "risk_level": risk}
    if title is not None:
        g["title"] = title
    if summary is not None:
        g["summary"] = summary
    return g


def test_rich_cards_built_from_real_pending_gates():
    """Each featured card carries the gate's real content + a /gate link — no
    hardcoded chart data. The rendered thesis excerpt comes from summary."""
    ben = _dept("ben", "Ben")
    cols = [
        _col(ben, [
            _gate(
                "g-001",
                kind="trade_proposal",
                risk="high",
                title="Basculer vers INDA",
                summary="**Thèse** : l'Inde surperforme les émergents larges.",
            ),
        ]),
        _col(_dept("empty", "Empty"), []),
    ]
    cards = _rich_cards(cols)
    assert len(cards) == 1
    c = cards[0]
    assert c["slug"] == "ben"
    assert c["id"] == "g-001"
    assert c["title"] == "Basculer vers INDA"
    assert c["href"] == "/gate/ben/g-001"
    # thesis is rendered (sanitized) markdown — the **bold** became <strong>.
    assert c["thesis"] is not None
    assert "<strong>" in str(c["thesis"])
    assert "Thèse" in str(c["thesis"])
    # no fake INDA numbers leak from anywhere
    assert "+17.8%" not in str(c["thesis"])


def test_rich_cards_ranks_higher_risk_first_and_caps_the_set():
    """Higher-risk proposals surface first; the set is capped small (<=3)."""
    d = _dept("d", "D")
    gates = [
        _gate("low1", risk="low", title="Low A"),
        _gate("crit", risk="critical", title="Critical"),
        _gate("high", risk="high", title="High"),
        _gate("med", risk="medium", title="Medium"),
        _gate("low2", risk="low", title="Low B"),
    ]
    cards = _rich_cards([_col(d, gates)], limit=3)
    assert len(cards) == 3
    # critical, then high, then medium (low ones drop off the capped set)
    assert [c["title"] for c in cards] == ["Critical", "High", "Medium"]


def test_rich_cards_skips_malformed_and_id_less_gates():
    """Malformed placeholder gates and gates with no id are skipped."""
    d = _dept("d", "D")
    gates = [
        {"id": "bad", "_malformed": True, "kind": "malformed_gate"},
        {"kind": "trade_proposal", "risk_level": "high"},  # no id
        _gate("ok", risk="medium", title="Real one"),
    ]
    cards = _rich_cards([_col(d, gates)])
    assert [c["id"] for c in cards] == ["ok"]


def test_rich_cards_structured_summary_yields_no_thesis_no_crash():
    """A dict summary (some content-dept kinds) → thesis None, card still built."""
    d = _dept("miranda", "Miranda")
    g = _gate("post-1", kind="social_post", risk="low", title="Post")
    g["summary"] = {"hook": "x", "theme": "y"}  # structured, not a string
    cards = _rich_cards([_col(d, [g])])
    assert len(cards) == 1
    assert cards[0]["thesis"] is None


def test_rich_cards_empty_when_no_gates():
    """No pending gates anywhere → empty list (template shows the placeholder,
    NOT the fake chart)."""
    cols = [_col(_dept("a", "A"), []), _col(_dept("b", "B"), [])]
    assert _rich_cards(cols) == []


# ── Full-page render guards ──────────────────────────────────────────────


def _build_client_empty(monkeypatch, tmp_path):
    """A TestClient over an empty disk root — no depts, so no pending gates."""
    root = tmp_path / "depts"
    root.mkdir()
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


def test_home_has_no_fake_inda_chart_markup(monkeypatch, tmp_path):
    """The hardcoded illustrative INDA/EEM/SPY chart is GONE from home —
    no fake "+17.8%" note, no INDA/EEM/SPY legend swatches."""
    c = _build_client_empty(monkeypatch, tmp_path)
    body = c.get("/").text
    assert "+17.8%" not in body
    assert "INDA" not in body
    assert "Performance rebasée" not in body


def test_home_empty_state_placeholder_when_no_gates(monkeypatch, tmp_path):
    """No pending gates → the Cartes riches section shows the quiet
    placeholder, and the section + the legit flowchart card still render."""
    c = _build_client_empty(monkeypatch, tmp_path)
    body = c.get("/").text
    assert "Aucune proposition riche en attente" in body
    # The legit explainer card (gate lifecycle) is kept — not fake data.
    assert "Cycle d'un gate" in body
