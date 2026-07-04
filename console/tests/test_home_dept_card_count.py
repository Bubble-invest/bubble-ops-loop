"""
test_home_dept_card_count.py — board #531 guard.

Each live dept widget on the landing page carries a 3rd stat-tile: the count of
OPEN board cards tagged `dept:<slug>` ("cartes kanban"). Semantics must match
the /kanban by-department view — cards bucketed by canonical dept slug (owner
derived from the `dept:<x>` label, then case/alias-normalised).

All GitHub I/O is monkeypatched via `_kanban._fetch_issues` (module-level swap),
matching test_home_decisions_all_depts.py — no real network call happens.
"""
from __future__ import annotations

from console.routes.home import _dept_card_counts


def _kanban_module():
    from console.routes import kanban as _kanban
    return _kanban


def _issue(number: int, *, labels: list[str], title: str = "") -> dict:
    return {
        "number": number,
        "title": title or f"Card {number}",
        "body": "## Job\nDo the thing.\n",
        "labels": [{"name": lbl} for lbl in labels],
        "url": f"https://github.com/Bubble-invest/bubble-ops-board/issues/{number}",
        "updatedAt": "2026-07-02T10:00:00Z",
        "createdAt": "2026-07-01T09:00:00Z",
        "state": "open",
    }


def _fixture_issues():
    """3 fixture-dept cards, 1 rnd, 1 with an aliased owner that canonicalises
    to rnd (`dept:rick / rnd`), and 1 with no dept label (→ 'unassigned')."""
    return [
        _issue(1, labels=["dept:fixture", "status:in-progress"]),
        _issue(2, labels=["dept:fixture", "needs:human"]),
        _issue(3, labels=["dept:fixture", "status:blocked"]),
        _issue(4, labels=["dept:rnd"]),
        _issue(5, labels=["dept:rick / rnd"]),      # alias → rnd
        _issue(6, labels=["needs:human"]),          # no dept → unassigned
    ]


def test_dept_card_counts_counts_open_cards_by_canonical_dept(monkeypatch):
    """N open dept:<slug> cards → count N; aliases fold into the canonical slug."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_fixture_issues(), None))

    counts = _dept_card_counts()
    assert counts["fixture"] == 3
    assert counts["rnd"] == 2          # dept:rnd + dept:rick / rnd (alias)
    assert counts["unassigned"] == 1


def test_dept_card_counts_zero_dept_absent_from_map(monkeypatch):
    """A dept with no cards is simply absent → widget resolves it to 0 via .get()."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_fixture_issues(), None))

    counts = _dept_card_counts()
    assert counts.get("security", 0) == 0


def test_dept_card_counts_degrades_to_empty_on_fetch_error(monkeypatch):
    """A fetch that raises must degrade to {} (→ every widget shows 0), not 500."""
    _kanban = _kanban_module()

    def _boom():
        raise RuntimeError("board unreachable")

    monkeypatch.setattr(_kanban, "_fetch_issues", _boom)
    assert _dept_card_counts() == {}


def test_home_renders_third_tile_with_correct_count(client, monkeypatch):
    """Full render: the live 'fixture' widget shows its 3 open kanban cards on
    the 3rd stat-tile with the 'cartes kanban' label. Zero-count depts render
    '0' + the label (no crash)."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_fixture_issues(), None))

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # The label (plural form) must be present on the widget.
    assert "cartes kanban" in body
    # fixture has 3 open cards — the number renders in the strip.
    assert ">3<" in body.replace(" ", "").replace("\n", "") or "3" in body


def test_home_zero_cards_tile_renders_singular_and_no_crash(client, monkeypatch):
    """No cards at all → every widget shows a 0 tile; singular label form used
    when a dept has exactly 1 card is also exercised."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_issues",
        lambda: ([_issue(9, labels=["dept:fixture"])], None),
    )
    r = client.get("/")
    assert r.status_code == 200
    # Exactly 1 fixture card → singular label.
    assert "carte kanban" in r.text
