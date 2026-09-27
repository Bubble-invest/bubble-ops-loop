"""tools/wiki-search/search.py -- the hard-fallback contract (board #1505
step 2). Ported/adapted from the step-1 research's `tests/test_fallback.py`
(~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/): these must
pass with NO network access and NO OpenRouter key present -- that's the
whole point. Reranking is strictly additive; its absence/failure must never
make a search go empty or crash. Adapted to search.py's own
`rerank_or_fallback(candidates, ...)` signature (a plain candidate-tuple
list, not a wiki_search module + db connection double).
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from unittest import mock

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = REPO / "tools" / "wiki-search"

FAKE_HITS = [
    (0.9, "shared/systems/a.md", "A", "desc a", "shared"),
    (0.8, "shared/systems/b.md", "B", "desc b", "shared"),
    (0.7, "shared/systems/c.md", "C", "desc c", "shared"),
]


@pytest.fixture()
def wsearch(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_SEARCH_WIKI_DIR", str(tmp_path / "shared-wiki"))
    monkeypatch.setenv("WIKI_SEARCH_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "shared-wiki").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(TOOL_DIR))
    for mod_name in ("wiki_paths", "fts_index", "hybrid_index", "context_builder",
                     "jev_bridge", "rerank", "search"):
        sys.modules.pop(mod_name, None)
    importlib.import_module("wiki_paths")
    search = importlib.import_module("search")
    try:
        yield search
    finally:
        for mod_name in ("search", "rerank", "jev_bridge", "context_builder",
                         "hybrid_index", "fts_index", "wiki_paths"):
            sys.modules.pop(mod_name, None)
        if str(TOOL_DIR) in sys.path:
            sys.path.remove(str(TOOL_DIR))


def test_falls_back_when_no_openrouter_key(wsearch):
    with mock.patch("jev_bridge.load_openrouter_key_into_env", return_value=False):
        results, used_rerank, warning = wsearch.rerank_or_fallback(
            FAKE_HITS, query="q", top=3, rerank_top_n=3,
            variant="minimal", design="choice", threshold=0.15,
            max_spend=0.05, receipts_path=None)
    assert not used_rerank
    assert warning is not None and "no OpenRouter key" in warning
    assert [h[1] for h in results] == [h[1] for h in FAKE_HITS[:3]]


def test_falls_back_when_jev_call_raises(wsearch):
    with mock.patch("jev_bridge.load_openrouter_key_into_env", return_value=True), \
         mock.patch("rerank.rerank_choice", side_effect=RuntimeError("boom")):
        results, used_rerank, warning = wsearch.rerank_or_fallback(
            FAKE_HITS, query="q", top=3, rerank_top_n=3,
            variant="minimal", design="choice", threshold=0.15,
            max_spend=0.05, receipts_path=None)
    assert not used_rerank
    assert "rerank call raised" in warning
    assert [h[1] for h in results] == [h[1] for h in FAKE_HITS[:3]]


def test_falls_back_when_jev_call_not_ok(wsearch):
    bad_receipt = {"ok": False, "error": "500 server error"}
    with mock.patch("jev_bridge.load_openrouter_key_into_env", return_value=True), \
         mock.patch("rerank.rerank_choice", return_value=([], bad_receipt, 0)):
        results, used_rerank, warning = wsearch.rerank_or_fallback(
            FAKE_HITS, query="q", top=3, rerank_top_n=3,
            variant="minimal", design="choice", threshold=0.15,
            max_spend=0.05, receipts_path=None)
    assert not used_rerank
    assert "Jev call failed" in warning
    assert [h[1] for h in results] == [h[1] for h in FAKE_HITS[:3]]


def test_falls_back_when_everything_scores_below_threshold(wsearch):
    ok_receipt = {"ok": True}
    scored = [("shared/systems/a.md", 0.02), ("shared/systems/b.md", 0.01)]
    with mock.patch("jev_bridge.load_openrouter_key_into_env", return_value=True), \
         mock.patch("rerank.rerank_choice", return_value=(scored, ok_receipt, 100)):
        results, used_rerank, warning = wsearch.rerank_or_fallback(
            FAKE_HITS, query="q", top=3, rerank_top_n=3,
            variant="minimal", design="choice", threshold=0.15,
            max_spend=0.05, receipts_path=None)
    assert not used_rerank
    assert "below --rerank-threshold" in warning
    assert [h[1] for h in results] == [h[1] for h in FAKE_HITS[:3]]


def test_empty_candidates_returns_empty_not_a_crash(wsearch):
    results, used_rerank, warning = wsearch.rerank_or_fallback(
        [], query="q", top=3, rerank_top_n=3,
        variant="minimal", design="choice", threshold=0.15,
        max_spend=0.05, receipts_path=None)
    assert results == []
    assert not used_rerank
    assert warning is None


def test_successful_rerank_reorders_and_filters(wsearch):
    ok_receipt = {"ok": True}
    # reranker prefers c over a/b, and drops b below threshold
    scored = [("shared/systems/c.md", 0.9), ("shared/systems/a.md", 0.5),
              ("shared/systems/b.md", 0.05)]
    with mock.patch("jev_bridge.load_openrouter_key_into_env", return_value=True), \
         mock.patch("rerank.rerank_choice", return_value=(scored, ok_receipt, 100)):
        results, used_rerank, warning = wsearch.rerank_or_fallback(
            FAKE_HITS, query="q", top=3, rerank_top_n=3,
            variant="minimal", design="choice", threshold=0.15,
            max_spend=0.05, receipts_path=None)
    assert used_rerank
    assert warning is None
    assert [h[1] for h in results] == ["shared/systems/c.md", "shared/systems/a.md"]


def test_detect_candidate_source_never_raises_when_ollama_unreachable(wsearch, monkeypatch):
    """auto-detect must degrade to fts_only, never raise, when Ollama is
    simply not there (every VPS dept, most other Macs)."""
    monkeypatch.setattr(wsearch.hybrid_index, "ollama_available", lambda: (_ for _ in ()).throw(OSError("no route")))
    assert wsearch.detect_candidate_source() == "fts_only"
