"""
test_merge_ready_reader.py — merge-ready PR surface (board #469).

Covers the service (marker detection, explanation extraction, chips parsing,
token-missing → [] + warning, single-flight cache) and the home render (section
appears with a stubbed list, hidden when empty). NO real GitHub calls — the HTTP
layer (`_get_json`) and token reader are monkeypatched.
"""
from __future__ import annotations

import threading
import time

import pytest

from console.services import merge_ready_reader as mrr


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _reset_cache():
    mrr._cache_data = []
    mrr._cache_ts = 0.0


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with a cold module cache."""
    _reset_cache()
    yield
    _reset_cache()


def _stub_api(monkeypatch, pulls, comments_by_number):
    """Monkeypatch the token + _get_json so no real GitHub call happens.

    `pulls` is the list returned for the /pulls endpoint; `comments_by_number`
    maps a PR number → its issue-comments list.
    """
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    def fake_get_json(url, token):
        if "/pulls" in url:
            return pulls
        # comments URL: .../issues/<n>/comments
        num = int(url.rsplit("/comments", 1)[0].rsplit("/", 1)[1])
        return comments_by_number.get(num, [])

    monkeypatch.setattr(mrr, "_get_json", fake_get_json)


# ── Marker detection + card shape ─────────────────────────────────────────────

def test_marker_comment_detected_builds_card(monkeypatch):
    pulls = [{
        "number": 42,
        "title": "fix(console): corrige le rendu des cartes",
        "html_url": "https://github.com/Bubble-invest/bubble-ops-loop/pull/42",
        "created_at": "2026-07-02T09:00:00Z",
        "body": "RÉSUMÉ: Les cartes s'affichent de nouveau correctement.",
    }]
    comments = {42: [
        {"body": "un commentaire quelconque"},
        {"body": "Revue indépendante: PASS. 42 passed. Merge-ready for Joris"},
    ]}
    _stub_api(monkeypatch, pulls, comments)

    cards = mrr.list_merge_ready()
    assert len(cards) == 1
    card = cards[0]
    assert card["number"] == 42
    assert card["repo"] == "bubble-ops-loop"
    assert card["html_url"].endswith("/pull/42")
    # RÉSUMÉ line wins as the explanation
    assert card["explanation"] == "Les cartes s'affichent de nouveau correctement."


def test_pr_without_marker_is_excluded(monkeypatch):
    pulls = [{
        "number": 7, "title": "feat: nouveau truc", "html_url": "u",
        "created_at": "2026-07-02T09:00:00Z", "body": "",
    }]
    comments = {7: [{"body": "en cours de revue, pas encore prêt"}]}
    _stub_api(monkeypatch, pulls, comments)
    assert mrr.list_merge_ready() == []


def test_marker_is_case_insensitive(monkeypatch):
    pulls = [{"number": 5, "title": "chore: x", "html_url": "u",
              "created_at": "2026-07-02T09:00:00Z", "body": ""}]
    comments = {5: [{"body": "MERGE-READY FOR JORIS ✅"}]}
    _stub_api(monkeypatch, pulls, comments)
    assert len(mrr.list_merge_ready()) == 1


# ── Explanation extraction ────────────────────────────────────────────────────

def test_explanation_prefers_resume_line():
    body = "Some intro\n**RÉSUMÉ:** Le cockpit affiche les PRs prêtes.\nmore text"
    assert mrr._explanation("fix(x): title", body) == "Le cockpit affiche les PRs prêtes."


def test_explanation_falls_back_to_title_prefix_stripped():
    assert mrr._explanation("fix(console): corrige le bug", "") == "corrige le bug"
    assert mrr._explanation("feat: ajoute une section", "") == "ajoute une section"
    # No conventional-commit prefix → title unchanged
    assert mrr._explanation("Simple titre sans préfixe", "") == "Simple titre sans préfixe"


# ── Chips parsing ─────────────────────────────────────────────────────────────

def test_chips_parse_tests_and_mutation():
    chips = mrr._chips("All green: 128 passed. mutation testing done. Merge-ready for Joris")
    assert "128 tests ✓" in chips
    assert "mutation-testé" in chips
    assert "revue indépendante ✓" in chips


def test_chips_always_include_independent_review():
    chips = mrr._chips("Merge-ready for Joris")
    assert chips == ["revue indépendante ✓"]


# ── Age helper ────────────────────────────────────────────────────────────────

def test_age_human_hours():
    from datetime import datetime, timezone
    now = datetime(2026, 7, 2, 11, 0, 0, tzinfo=timezone.utc)
    assert mrr._age_human("2026-07-02T09:00:00Z", now=now) == "il y a 2 h"


def test_age_human_bad_input_empty():
    assert mrr._age_human("") == ""
    assert mrr._age_human("not-a-date") == ""


# ── Fail-safe: token missing → [] + warning ──────────────────────────────────

def test_token_missing_returns_empty_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(mrr, "_read_board_token", lambda: None)
    # If it tried the network we'd know — but it must short-circuit first.
    with caplog.at_level("WARNING"):
        assert mrr.list_merge_ready() == []
    assert any("token" in r.message.lower() for r in caplog.records)


def test_api_error_returns_empty_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    def boom(url, token):
        raise OSError("network down")

    monkeypatch.setattr(mrr, "_get_json", boom)
    with caplog.at_level("WARNING"):
        assert mrr.list_merge_ready() == []
    assert any("empty" in r.message.lower() for r in caplog.records)


# ── Single-flight cache (mirror test_fetch_issues_single_flight pattern) ──────

def test_cache_single_flight(monkeypatch):
    """Concurrent callers within the TTL share ONE underlying fetch."""
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")
    call_count = {"n": 0}
    lock = threading.Lock()

    def slow_get_json(url, token):
        if "/pulls" in url:
            with lock:
                call_count["n"] += 1
            time.sleep(0.05)  # widen the window so threads pile up
            return []  # no PRs → no comment fetches
        return []

    monkeypatch.setattr(mrr, "_get_json", slow_get_json)

    threads = [threading.Thread(target=mrr.list_merge_ready) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The single-flight lock + re-check means only ONE thread hit the API.
    assert call_count["n"] == 1


def test_cache_returns_hit_without_refetch(monkeypatch):
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")
    calls = {"n": 0}

    def counting_get_json(url, token):
        if "/pulls" in url:
            calls["n"] += 1
        return []

    monkeypatch.setattr(mrr, "_get_json", counting_get_json)
    mrr.list_merge_ready()
    mrr.list_merge_ready()  # served from cache
    assert calls["n"] == 1


# ── Home render (section shows / hides) ───────────────────────────────────────

def test_home_renders_merge_ready_section(client, monkeypatch):
    from console.routes import home
    stub = [{
        "repo": "bubble-ops-loop", "number": 99,
        "title": "fix: x", "html_url": "https://github.com/o/r/pull/99",
        "created_at": "2026-07-02T09:00:00Z", "age": "il y a 2 h",
        "explanation": "Le cockpit affiche les PRs prêtes à merger.",
        "chips": ["42 tests ✓", "revue indépendante ✓"],
    }]
    monkeypatch.setattr(home.merge_ready_reader, "list_merge_ready", lambda *a, **k: stub)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Prêt à merger" in body
    assert "Le cockpit affiche les PRs prêtes à merger." in body
    assert "bubble-ops-loop·#99" in body
    assert "42 tests ✓" in body
    assert 'href="https://github.com/o/r/pull/99"' in body
    assert 'target="_blank"' in body


def test_home_hides_section_when_empty(client, monkeypatch):
    from console.routes import home
    monkeypatch.setattr(home.merge_ready_reader, "list_merge_ready", lambda *a, **k: [])
    r = client.get("/")
    assert r.status_code == 200
    assert "Prêt à merger" not in r.text
