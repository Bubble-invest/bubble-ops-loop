"""
test_home_decisions_all_depts.py — board #505 regression guard.

The landing page's "décisions qu'on attend de toi" pile previously filtered
`_board_decision_cards()` to `dept:rnd` only (comment: "the dept gates already
cover the others") — that assumption breaks for the 8+ open needs:human cards
that carry NO host: label, so no dept gate ever surfaces them. Result: 14 of
15 open needs:human board cards were invisible to Joris on home.

Fix: `_board_decision_cards()` now returns EVERY open needs:human card
(dropping the owner == "rnd" restriction), and a new
`_group_decision_cards_by_dept()` groups them by dept — rnd first, then other
depts alphabetically, then a no-dept bucket last — for the template to render
one section per dept with the SAME decide buttons/UI as before.

All GitHub I/O is monkeypatched via `_kanban._fetch_issues` (module-level
function swap), matching the pattern used by
test_kanban_card_attachment_repo.py — no real network call happens.
"""
from __future__ import annotations

from console.routes.home import _board_decision_cards, _group_decision_cards_by_dept


def _kanban_module():
    from console.routes import kanban as _kanban
    return _kanban


def _issue(number: int, *, labels: list[str], title: str = "") -> dict:
    """A raw GitHub REST issue dict shaped like _fetch_issues's normalized output."""
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


def _three_dept_issues():
    """One dept:rnd, one dept:content, one with NO dept label at all — all
    needs:human. Mirrors the real board: cards with no host: label are the
    ones the old dept-gate assumption silently dropped."""
    return [
        _issue(501, labels=["needs:human", "dept:rnd"], title="RnD decision"),
        _issue(502, labels=["needs:human", "dept:content"], title="Content decision"),
        _issue(503, labels=["needs:human"], title="No-dept decision"),
        # A needs:human-free card must NOT show up (control).
        _issue(504, labels=["dept:rnd", "status:in-progress"], title="Not a decision"),
    ]


def test_board_decision_cards_returns_all_depts_not_just_rnd(monkeypatch):
    """Board #505: dropping the owner == 'rnd' filter — all 3 needs:human
    cards come back regardless of dept, the 4th (no needs:human) does not."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_three_dept_issues(), None))

    cards = _board_decision_cards()
    ids = {c["id"] for c in cards}
    assert ids == {"501", "502", "503"}, \
        f"expected all 3 needs:human cards regardless of dept, got {ids}"


def test_group_decision_cards_by_dept_orders_rnd_first_then_alpha_then_nodept(monkeypatch):
    """rnd group first, other depts alphabetically, no-dept bucket last."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_three_dept_issues(), None))

    cards = _board_decision_cards()
    groups = _group_decision_cards_by_dept(cards)

    owners_in_order = [g["owner"] for g in groups]
    assert owners_in_order == ["rnd", "content", ""], \
        f"expected rnd first, then alpha depts, then no-dept last, got {owners_in_order}"

    # Each group carries exactly the card(s) for its owner.
    by_owner = {g["owner"]: g["cards"] for g in groups}
    assert [c["id"] for c in by_owner["rnd"]] == ["501"]
    assert [c["id"] for c in by_owner["content"]] == ["502"]
    assert [c["id"] for c in by_owner[""]] == ["503"]


def test_group_decision_cards_by_dept_omits_empty_groups():
    """No dept:content cards in the input -> no 'content' group emitted."""
    cards = [
        {"id": "1", "owner": "rnd", "due": ""},
        {"id": "2", "owner": "", "due": ""},
    ]
    groups = _group_decision_cards_by_dept(cards)
    owners = [g["owner"] for g in groups]
    assert owners == ["rnd", ""]
    assert "content" not in owners


def test_home_page_renders_all_dept_cards_and_total_count(client, monkeypatch):
    """Full render: home() must show a count of 3 (not 1), and every card —
    rnd, content, and no-dept — must be reachable via a compact click-through
    to its card detail page (/kanban/card/<id>). Per the mockup-4 redesign
    (#524) the decide form is NOT inlined on this overview — it lives on the
    detail page the link opens; the click-through IS the real-function guard."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_fetch_issues", lambda: (_three_dept_issues(), None))

    r = client.get("/")
    assert r.status_code == 200
    body = r.text

    # All three needs:human cards reachable on the page.
    assert "RnD decision" in body
    assert "Content decision" in body
    assert "No-dept decision" in body
    # The excluded (non needs:human) card must not appear as a decision card.
    assert "Not a decision" not in body

    # Each card links to its detail page (where the decide form lives).
    assert 'href="/kanban/card/501"' in body
    assert 'href="/kanban/card/502"' in body
    assert 'href="/kanban/card/503"' in body

    # The badge/count reflects the TOTAL across all depts, not just rnd's 1.
    assert "rnd_decision_count" not in body  # sanity: no raw var name leaked
