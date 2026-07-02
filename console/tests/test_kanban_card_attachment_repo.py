"""
test_kanban_card_attachment_repo.py — board #429 regression guard.

The kanban card DETAIL page (GET /kanban/card/<n>) hardcoded every inline
attachment image URL to `repo=Bubble-invest/bubble-ops-board`, so a dept-repo
visual (e.g. a dept:content card's `outputs/.../attachments/...` image, which
lives in bubble-ops-content, not the board repo) resolved against the WRONG
checkout -> the attachment route 404s -> the image is silently absent. The
gate-card surface already handled repo-relative attachments correctly by
resolving per-dept; this pins the same behaviour on the kanban card surface:
the rendered <img>/<a> `repo=` query param must be the CARD'S OWNING dept
repo (derived from its dept: label), not always the board repo.

All GitHub I/O is monkeypatched (`_fetch_single_issue`) so no real network
call happens.
"""
from __future__ import annotations


def _kanban_module():
    from console.routes import kanban as _kanban
    return _kanban


def _issue_with_attachment(number: int, *, labels: list[str]) -> dict:
    """A raw GitHub REST issue dict with one Visual Attachments entry."""
    body = (
        "## Job\nCheck the visual.\n\n"
        "## Visual Attachments\n"
        "- outputs/2026-07-01/attachments/post-visual.png\n"
    )
    return {
        "number": number,
        "title": f"Carte test #{number}",
        "body": body,
        "labels": [{"name": lbl} for lbl in labels],
        "html_url": f"https://github.com/Bubble-invest/bubble-ops-board/issues/{number}",
        "url": "",
        "updated_at": "2026-07-02T10:00:00Z",
        "updatedAt": "2026-07-02T10:00:00Z",
        "created_at": "2026-07-01T09:00:00Z",
        "comments_list": [],
    }


def test_dept_content_card_attachment_resolves_against_content_repo(client, monkeypatch):
    """A dept:content card's inline attachment image must point at
    repo=Bubble-invest/bubble-ops-content — NOT the board repo (board #429)."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue_with_attachment(n, labels=["dept:content", "status:in-progress"]), None),
    )
    r = client.get("/kanban/card/900")
    assert r.status_code == 200
    assert "repo=Bubble-invest/bubble-ops-content" in r.text
    assert "repo=Bubble-invest/bubble-ops-board" not in r.text


def test_board_native_card_attachment_still_resolves_against_board_repo(client, monkeypatch):
    """A card with no dept: label (board-native) keeps resolving its attachment
    against the board repo itself — unchanged prior behaviour."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue_with_attachment(n, labels=["status:in-progress"]), None),
    )
    r = client.get("/kanban/card/901")
    assert r.status_code == 200
    assert "repo=Bubble-invest/bubble-ops-board" in r.text
