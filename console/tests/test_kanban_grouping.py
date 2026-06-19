"""Tests for kanban grouping helpers.

Standalone: run with `python3 console/tests/test_kanban_grouping.py` or pytest.
Imports pure functions from the route module — no FastAPI app needed.
"""
import sys
import os

# Ensure the project root is on the path so the route module can be imported
# without starting the full FastAPI application.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from console.routes.kanban import (
    derive_project,
    group_by_department,
    group_by_project,
    group_accent,
    build_accent_map,
    _flatten_columns,
)

# ── Fixture: a mini /api/inbox-shaped response ────────────────────────────────

SAMPLE_COLUMNS = {
    "needs_attention": [
        {
            "id": "1",
            "column": "needs_attention",
            "owner": "rnd",
            "priority": "high",
            "kanban_type": "incident",
            "title": "Cockpit dashboard crash",
            "body": "The kanban page throws a 500 on startup",
            "ts_display": "2026-06-19T10:00:00",
            "context_url": "",
        },
        {
            "id": "2",
            "column": "needs_attention",
            "owner": "ben",
            "priority": "medium",
            "kanban_type": "task",
            "title": "Alpaca trade failed",
            "body": "Saxo order rejected due to insufficient margin",
            "ts_display": "2026-06-19T09:30:00",
            "context_url": "",
        },
    ],
    "investigating": [
        {
            "id": "3",
            "column": "investigating",
            "owner": "maya",
            "priority": "low",
            "kanban_type": "findings",
            "title": "LinkedIn prospect scoring",
            "body": "Maya needs to investigate a linkedin prospect pipeline issue",
            "ts_display": "2026-06-18T14:00:00",
            "context_url": "https://example.com",
        },
        {
            "id": "4",
            "column": "investigating",
            "owner": "",  # should fall back to 'unassigned'
            "priority": "",
            "kanban_type": "task",
            "title": "Whisper audio listener broken",
            "body": "voice recognition mic not working",
            "ts_display": "2026-06-17T08:00:00",
            "context_url": "",
        },
    ],
    "waiting": [],
    "done": [
        {
            "id": "5",
            "column": "done",
            "owner": "rnd",
            "priority": "high",
            "kanban_type": "task",
            "title": "Wiki memory compile",
            "body": "synthesis and compile of the shared wiki pages",
            "ts_display": "2026-06-16T12:00:00",
            "context_url": "",
        },
    ],
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_derive_project_cockpit():
    card = {"title": "Cockpit dashboard crash", "body": "kanban page 500"}
    assert derive_project(card) == "Cockpit & Dashboard"


def test_derive_project_fund():
    card = {"title": "Alpaca trade failed", "body": "Saxo margin"}
    assert derive_project(card) == "Fund & Investissement"


def test_derive_project_prospection():
    card = {"title": "LinkedIn prospect scoring", "body": "maya pipeline"}
    assert derive_project(card) == "Prospection & Contenu"


def test_derive_project_voice():
    card = {"title": "Whisper audio listener", "body": "voice recognition mic"}
    assert derive_project(card) == "Voix & Audio"


def test_derive_project_wiki():
    card = {"title": "Wiki memory compile", "body": "synthesis of shared wiki"}
    assert derive_project(card) == "Wiki & Mémoire"


def test_derive_project_autre():
    card = {"title": "Random thing", "body": "no matching keywords here"}
    assert derive_project(card) == "Autre"


def test_derive_project_empty_card():
    card = {}
    assert derive_project(card) == "Autre"


def test_group_by_department_basic():
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    result = group_by_department(all_cards)
    assert "rnd" in result
    assert "ben" in result
    assert "maya" in result
    assert "unassigned" in result  # card with empty owner
    # rnd has 2 cards (ids 1, 5)
    assert len(result["rnd"]) == 2


def test_group_by_department_unassigned_fallback():
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    result = group_by_department(all_cards)
    unassigned = result["unassigned"]
    assert len(unassigned) == 1
    assert unassigned[0]["id"] == "4"


def test_group_by_department_sorted_by_count():
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    result = group_by_department(all_cards)
    counts = [len(v) for v in result.values()]
    # Groups must be sorted descending by card count
    assert counts == sorted(counts, reverse=True)


def test_group_by_project_buckets():
    """Verify that known cards land in expected project buckets.

    Keyword matching is WORD-BOUNDARY based (not raw substring), so 'gate' does
    NOT match inside 'investigate', 'nav' not inside 'navigate', etc."""
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    result = group_by_project(all_cards)
    # All 5 cards must end up somewhere
    total = sum(len(v) for v in result.values())
    assert total == 5
    assert "Cockpit & Dashboard" in result     # card 1: "cockpit", "kanban"
    assert "Fund & Investissement" in result   # card 2: "alpaca", "saxo"
    assert "Voix & Audio" in result            # card 4: "whisper", "voice", "mic"
    assert "Wiki & Mémoire" in result          # card 5: "wiki", "synthesis", "compile"
    # Card 3 ("LinkedIn prospect scoring / maya needs to investigate") must land
    # in Prospection & Contenu (linkedin/prospect/maya) — NOT Cockpit. The old
    # raw-substring matcher wrongly filed it under Cockpit because 'gate' ⊂
    # 'investigate'; word-boundary matching fixes that.
    assert any(c["id"] == "3" for c in result.get("Prospection & Contenu", [])), \
        "Card 3 (linkedin/prospect/maya) must bucket into Prospection & Contenu"


def test_word_boundary_no_false_substring_match():
    """Regression guard: 'investigate' must NOT match the 'gate' keyword, and
    'navigate' must NOT match 'nav'. Both would mis-file into the wrong bucket."""
    from console.routes.kanban import derive_project
    assert derive_project({"title": "Investigate something", "body": ""}) == "Autre"
    assert derive_project({"title": "Navigate the menu", "body": ""}) == "Autre"
    # but a real standalone keyword still matches
    assert derive_project({"title": "Approve the gate", "body": ""}) == "Cockpit & Dashboard"


def test_group_by_project_sorted_by_count():
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    result = group_by_project(all_cards)
    counts = [len(v) for v in result.values()]
    assert counts == sorted(counts, reverse=True)


def test_priority_sort_within_group():
    """High-priority cards should appear before medium and low."""
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    dept_groups = group_by_department(all_cards)
    # 'rnd' has card 1 (high) and card 5 (high) — both high, order by ts
    # Let's check the 'needs_attention' column independently
    from console.routes.kanban import _sort_cards
    cards = [
        {"id": "a", "priority": "low",    "ts_display": "2026-06-19"},
        {"id": "b", "priority": "high",   "ts_display": "2026-06-18"},
        {"id": "c", "priority": "medium", "ts_display": "2026-06-17"},
        {"id": "d", "priority": "",       "ts_display": "2026-06-19"},
    ]
    sorted_cards = _sort_cards(cards)
    ids = [c["id"] for c in sorted_cards]
    assert ids[0] == "b"   # high first
    assert ids[1] == "c"   # medium second
    # low and empty (both rank 2 / 3) come after medium


def test_flatten_columns():
    all_cards = _flatten_columns(SAMPLE_COLUMNS)
    assert len(all_cards) == 5  # 2 + 2 + 0 + 1


def test_group_accent_deterministic():
    """Same group name always returns the same accent colour."""
    assert group_accent("rnd") == group_accent("rnd")
    assert group_accent("Fund & Investissement") == group_accent("Fund & Investissement")


def test_group_accent_returns_hex():
    colour = group_accent("test group")
    assert colour.startswith("#")
    assert len(colour) == 7


def test_build_accent_map():
    groups = {"Alpha": [], "Beta": [], "Gamma": []}
    result = build_accent_map(groups)
    assert set(result.keys()) == {"Alpha", "Beta", "Gamma"}
    for v in result.values():
        assert v.startswith("#")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
