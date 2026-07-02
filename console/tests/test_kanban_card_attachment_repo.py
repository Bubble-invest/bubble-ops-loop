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


# ── Board #491 — override map: 4 live dept: values with no bubble-ops-<x> repo ──

def test_repo_for_dept_override_map_routes_to_board_repo():
    """rnd/security/claudette/morty have no bubble-ops-<x> repo on disk (#491) —
    _repo_for_dept must route them to the board repo, NOT build a nonexistent
    'Bubble-invest/bubble-ops-<dept>' string that 404s."""
    _kanban = _kanban_module()
    for dept in ("rnd", "security", "claudette", "morty"):
        assert _kanban._repo_for_dept(dept) == _kanban._BOARD_REPO, dept


def test_repo_for_dept_real_dept_repos_still_resolve_to_bubble_ops_x():
    """The 4 depts WITH a real bubble-ops-<x> repo (#429) must be unaffected by
    the #491 override map."""
    _kanban = _kanban_module()
    for dept in ("ben", "maya", "tony", "content"):
        assert (
            _kanban._repo_for_dept(dept)
            == f"Bubble-invest/bubble-ops-{dept}"
        ), dept


def test_dept_rnd_card_attachment_resolves_against_board_repo(client, monkeypatch):
    """A dept:rnd card's inline attachment must resolve against the board repo
    (rnd's own outputs live IN this loop repo, not a nonexistent
    bubble-ops-rnd) — board #491."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue_with_attachment(n, labels=["dept:rnd", "status:in-progress"]), None),
    )
    r = client.get("/kanban/card/902")
    assert r.status_code == 200
    assert "repo=Bubble-invest/bubble-ops-board" in r.text
    assert "repo=Bubble-invest/bubble-ops-rnd" not in r.text


def test_dept_security_card_attachment_resolves_against_board_repo(client, monkeypatch):
    """A dept:security card has no dedicated bubble-ops-security repo — must
    fall back to the board repo, not 404 (board #491)."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue_with_attachment(n, labels=["dept:security", "status:in-progress"]), None),
    )
    r = client.get("/kanban/card/903")
    assert r.status_code == 200
    assert "repo=Bubble-invest/bubble-ops-board" in r.text
    assert "repo=Bubble-invest/bubble-ops-security" not in r.text


def test_dept_claudette_and_morty_card_attachments_resolve_against_board_repo(client, monkeypatch):
    """claudette/morty are concierges (console/services/dept_registry.py ::
    KNOWN_CONCIERGE_SLUGS), not ops-loop depts — no bubble-ops-<x> repo of
    their own. Both must fall back to the board repo (board #491)."""
    _kanban = _kanban_module()
    for n, dept in ((904, "claudette"), (905, "morty")):
        monkeypatch.setattr(
            _kanban, "_fetch_single_issue",
            lambda num, _dept=dept: (_issue_with_attachment(num, labels=[f"dept:{_dept}", "status:in-progress"]), None),
        )
        r = client.get(f"/kanban/card/{n}")
        assert r.status_code == 200
        assert "repo=Bubble-invest/bubble-ops-board" in r.text
        assert f"repo=Bubble-invest/bubble-ops-{dept}" not in r.text
