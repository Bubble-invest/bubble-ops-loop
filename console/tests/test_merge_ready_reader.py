"""
test_merge_ready_reader.py — merge-ready PR surface (board #469).

Covers the service (marker detection incl. hardening, explanation extraction,
chips parsing, token-missing → [] + warning, conditional comment-fetch memo,
stale-while-revalidate cache) and the home render (section appears with a
stubbed list, hidden when empty). NO real GitHub calls — the HTTP layer
(`_get_json`) and token reader are monkeypatched.
"""
from __future__ import annotations

import threading
import time

import pytest

from console.services import merge_ready_reader as mrr


# ── Cache / memo reset ────────────────────────────────────────────────────────

def _reset_state():
    with mrr._cache_lock:
        mrr._cache_data = []
        mrr._cache_ts = 0.0
    with mrr._memo_lock:
        mrr._pr_memo.clear()


@pytest.fixture(autouse=True)
def _clear_state():
    """Every test starts with a cold module cache + empty memo."""
    _reset_state()
    yield
    _drain_refresh()
    _reset_state()


def _stub_api(monkeypatch, pulls, comments_by_number):
    """Monkeypatch the token + _get_json so no real GitHub call happens.

    `pulls` is the list returned for the /pulls endpoint; `comments_by_number`
    maps a PR number → its issue-comments list.
    """
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    def fake_get_json(url, token):
        if "/pulls" in url:
            return pulls
        num = int(url.rsplit("/comments", 1)[0].rsplit("/", 1)[1])
        return comments_by_number.get(num, [])

    monkeypatch.setattr(mrr, "_get_json", fake_get_json)


def _drain_refresh():
    """Wait for any in-flight background refresh thread to finish."""
    for t in threading.enumerate():
        if t.name == "merge-ready-refresh":
            t.join(timeout=5)


# ── Marker detection + card shape (drive the synchronous compute path) ────────

def test_marker_comment_detected_builds_card(monkeypatch):
    pulls = [{
        "number": 42,
        "title": "fix(console): corrige le rendu des cartes",
        "html_url": "https://github.com/Bubble-invest/bubble-ops-loop/pull/42",
        "created_at": "2026-07-02T09:00:00Z",
        "updated_at": "2026-07-02T10:00:00Z",
        "body": "RÉSUMÉ: Les cartes s'affichent de nouveau correctement.",
    }]
    comments = {42: [
        {"body": "un commentaire quelconque"},
        {"body": "Revue indépendante: PASS. 42 passed.\nMerge-ready for Joris"},
    ]}
    _stub_api(monkeypatch, pulls, comments)

    cards = mrr._compute_merge_ready(mrr._DEFAULT_REPOS)
    assert len(cards) == 1
    card = cards[0]
    assert card["number"] == 42
    assert card["repo"] == "bubble-ops-loop"
    assert card["html_url"].endswith("/pull/42")
    assert card["explanation"] == "Les cartes s'affichent de nouveau correctement."


def test_pr_without_marker_is_excluded(monkeypatch):
    pulls = [{
        "number": 7, "title": "feat: nouveau truc", "html_url": "u",
        "created_at": "2026-07-02T09:00:00Z", "updated_at": "2026-07-02T09:00:00Z",
        "body": "",
    }]
    comments = {7: [{"body": "en cours de revue, pas encore prêt"}]}
    _stub_api(monkeypatch, pulls, comments)
    assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []


def test_marker_is_case_insensitive(monkeypatch):
    pulls = [{"number": 5, "title": "chore: x", "html_url": "u",
              "created_at": "2026-07-02T09:00:00Z",
              "updated_at": "2026-07-02T09:00:00Z", "body": ""}]
    comments = {5: [{"body": "MERGE-READY FOR JORIS ✅"}]}
    _stub_api(monkeypatch, pulls, comments)
    assert len(mrr._compute_merge_ready(mrr._DEFAULT_REPOS)) == 1


# ── Marker hardening: quoted-in-FAIL not shown; genuine verdict shown ─────────

def test_fail_comment_quoting_marker_is_not_shown(monkeypatch):
    """A later FAIL verdict that QUOTES the merge-ready phrase must NOT re-flag."""
    pulls = [{"number": 9, "title": "fix: x", "html_url": "u",
              "created_at": "2026-07-02T09:00:00Z",
              "updated_at": "2026-07-02T09:00:00Z", "body": ""}]
    comments = {9: [
        {"body": "Merge-ready for Joris"},  # earlier genuine PASS
        {"body": "Verdict: FAIL.\n> Merge-ready for Joris\n"
                 "This does not yet meet the bar."},  # later FAIL quotes it
    ]}
    _stub_api(monkeypatch, pulls, comments)
    assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []


def test_genuine_verdict_after_fail_is_shown(monkeypatch):
    """A genuine merge-ready verdict as the LAST verdict is shown."""
    pulls = [{"number": 10, "title": "fix: x", "html_url": "u",
              "created_at": "2026-07-02T09:00:00Z",
              "updated_at": "2026-07-02T09:00:00Z", "body": ""}]
    comments = {10: [
        {"body": "Verdict: FAIL. needs a test."},  # earlier FAIL
        {"body": "Fixed. Verdict: PASS.\nMerge-ready for Joris"},  # later PASS
    ]}
    _stub_api(monkeypatch, pulls, comments)
    assert len(mrr._compute_merge_ready(mrr._DEFAULT_REPOS)) == 1


def test_marker_only_inside_quote_is_not_shown(monkeypatch):
    """A single comment that only mentions the marker inside a '>' quote → not shown."""
    pulls = [{"number": 11, "title": "fix: x", "html_url": "u",
              "created_at": "2026-07-02T09:00:00Z",
              "updated_at": "2026-07-02T09:00:00Z", "body": ""}]
    comments = {11: [
        {"body": "Verdict: FAIL — the reviewer wrote:\n"
                 "> Merge-ready for Joris\nbut I disagree."},
    ]}
    _stub_api(monkeypatch, pulls, comments)
    assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []


# ── Explanation extraction ────────────────────────────────────────────────────

def test_explanation_prefers_resume_line():
    body = "Some intro\n**RÉSUMÉ:** Le cockpit affiche les PRs prêtes.\nmore text"
    assert mrr._explanation("fix(x): title", body) == "Le cockpit affiche les PRs prêtes."


def test_explanation_falls_back_to_title_prefix_stripped():
    assert mrr._explanation("fix(console): corrige le bug", "") == "corrige le bug"
    assert mrr._explanation("feat: ajoute une section", "") == "ajoute une section"
    assert mrr._explanation("Simple titre sans préfixe", "") == "Simple titre sans préfixe"


def test_non_conventional_word_prefix_not_stripped():
    """'Bug: le rendu casse' is NOT a conventional-commit prefix → keep it whole."""
    assert mrr._explanation("Bug: le rendu casse", "") == "Bug: le rendu casse"
    assert mrr._explanation("Note: à revoir", "") == "Note: à revoir"
    assert mrr._explanation("WIP: brouillon", "") == "WIP: brouillon"
    # But genuine CC types still strip:
    assert mrr._explanation("refactor(api): nettoie", "") == "nettoie"


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


# ── Conditional comment-fetch (updated_at memo) ──────────────────────────────

def test_unchanged_updated_at_skips_comment_fetch(monkeypatch):
    """A second compute over the same PR (unchanged updated_at) fetches ZERO
    comments — the memoized verdict is reused."""
    pulls = [{
        "number": 20, "title": "fix: x", "html_url": "u",
        "created_at": "2026-07-02T09:00:00Z", "updated_at": "2026-07-02T09:00:00Z",
        "body": "",
    }]
    comments = {20: [{"body": "Merge-ready for Joris"}]}
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    comment_calls = {"n": 0}

    def counting_get_json(url, token):
        if "/pulls" in url:
            return pulls
        comment_calls["n"] += 1
        num = int(url.rsplit("/comments", 1)[0].rsplit("/", 1)[1])
        return comments.get(num, [])

    monkeypatch.setattr(mrr, "_get_json", counting_get_json)

    first = mrr._compute_merge_ready(mrr._DEFAULT_REPOS)
    assert len(first) == 1
    assert comment_calls["n"] == 1  # fetched once on the first pass

    second = mrr._compute_merge_ready(mrr._DEFAULT_REPOS)
    assert len(second) == 1          # still shown (memoized card reused)
    assert comment_calls["n"] == 1   # NO extra comment fetch — updated_at unchanged


def test_changed_updated_at_refetches_comments(monkeypatch):
    """When updated_at changes, comments ARE refetched (verdict may have flipped)."""
    state = {"updated_at": "2026-07-02T09:00:00Z",
             "comment": "Merge-ready for Joris"}

    def pulls():
        return [{
            "number": 21, "title": "fix: x", "html_url": "u",
            "created_at": "2026-07-02T09:00:00Z",
            "updated_at": state["updated_at"], "body": "",
        }]

    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")
    comment_calls = {"n": 0}

    def counting_get_json(url, token):
        if "/pulls" in url:
            return pulls()
        comment_calls["n"] += 1
        return [{"body": state["comment"]}]

    monkeypatch.setattr(mrr, "_get_json", counting_get_json)

    assert len(mrr._compute_merge_ready(mrr._DEFAULT_REPOS)) == 1
    assert comment_calls["n"] == 1

    # PR updated + verdict flipped to FAIL → refetch, now excluded.
    state["updated_at"] = "2026-07-02T11:00:00Z"
    state["comment"] = "Verdict: FAIL. needs work."
    assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []
    assert comment_calls["n"] == 2


# ── Fail-safe: token missing → [] + warning ──────────────────────────────────

def test_token_missing_returns_empty_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(mrr, "_read_board_token", lambda: None)
    with caplog.at_level("WARNING"):
        assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []
    assert any("token" in r.message.lower() for r in caplog.records)


def test_api_error_returns_empty_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    def boom(url, token):
        raise OSError("network down")

    monkeypatch.setattr(mrr, "_get_json", boom)
    with caplog.at_level("WARNING"):
        assert mrr._compute_merge_ready(mrr._DEFAULT_REPOS) == []
    assert any("empty" in r.message.lower() for r in caplog.records)


# ── Stale-while-revalidate: home never blocks on the network ─────────────────

def test_list_merge_ready_never_blocks_serves_stale(monkeypatch):
    """With a slow network stub and an EXPIRED cache, list_merge_ready returns in
    well under 100ms serving the stale list — it must not block on the refresh."""
    stale = [{"repo": "bubble-ops-loop", "number": 1, "title": "t",
              "html_url": "u", "created_at": "", "age": "", "explanation": "x",
              "chips": []}]
    with mrr._cache_lock:
        mrr._cache_data = stale
        mrr._cache_ts = time.monotonic() - (mrr._CACHE_TTL_SECONDS + 10)  # expired

    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")

    def slow_get_json(url, token):
        time.sleep(0.3)  # simulate a slow API
        return []

    monkeypatch.setattr(mrr, "_get_json", slow_get_json)

    t0 = time.monotonic()
    result = mrr.list_merge_ready()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.1, f"list_merge_ready blocked for {elapsed:.3f}s"
    assert result == stale  # served the stale list immediately
    _drain_refresh()  # cleanup the spawned thread


def test_background_refresh_updates_cache(monkeypatch):
    """The background refresh eventually replaces the cache with fresh data."""
    pulls = [{
        "number": 30, "title": "fix: x", "html_url": "u",
        "created_at": "2026-07-02T09:00:00Z", "updated_at": "2026-07-02T09:00:00Z",
        "body": "",
    }]
    comments = {30: [{"body": "Merge-ready for Joris"}]}
    _stub_api(monkeypatch, pulls, comments)

    # First-ever load: no cache → returns [] and kicks the refresh.
    first = mrr.list_merge_ready()
    assert first == []

    _drain_refresh()  # let the background refresh finish

    with mrr._cache_lock:
        assert mrr._cache_ts > 0.0
        assert len(mrr._cache_data) == 1
        assert mrr._cache_data[0]["number"] == 30

    # A subsequent load now serves the fresh cache synchronously.
    assert len(mrr.list_merge_ready()) == 1


def test_fresh_cache_served_without_refetch(monkeypatch):
    """Within the TTL, list_merge_ready serves the cache and does NOT hit the API."""
    with mrr._cache_lock:
        mrr._cache_data = [{"number": 99}]
        mrr._cache_ts = time.monotonic()  # fresh

    calls = {"n": 0}

    def counting_get_json(url, token):
        calls["n"] += 1
        return []

    monkeypatch.setattr(mrr, "_read_board_token", lambda: "fake-token")
    monkeypatch.setattr(mrr, "_get_json", counting_get_json)

    assert mrr.list_merge_ready() == [{"number": 99}]
    assert calls["n"] == 0


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
