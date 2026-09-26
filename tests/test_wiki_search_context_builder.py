"""tools/wiki-search/context_builder.py -- per-candidate state builder for
the Jev reranker (board #1505 step 2). Ported/adapted from the step-1
research's `tests/test_context_builder.py`
(~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/): same
assertions (token-budget monotonicity + ceilings, snippet extraction,
frontmatter/outline handling), but against a SYNTHETIC page written to a
temp dir instead of a real page under Rick's `~/.claude/agent-memory/
shared-wiki` -- hermetic, runs in CI where that wiki doesn't exist.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = REPO / "tools" / "wiki-search"

REAL_PAGE_REL = "shared/systems/sops-dotenv-reencryption-trap.md"
REAL_PAGE_CONTENT = (
    "---\ntitle: SOPS dotenv re-encryption trap\ntype: reference\nowner: rnd\n"
    "last_verified: 2026-08-01\n---\n"
    "# SOPS dotenv re-encryption trap\n\n"
    "This page is a full-size synthetic stand-in for a real wiki page (~2.5KB,\n"
    "matching the real page's own rough size, so the token-budget-ceiling\n"
    "assertions below stay meaningful against a realistic full-page baseline\n"
    "instead of a tiny fixture that would be dominated by JSON-structure\n"
    "overhead rather than actual body content).\n\n"
    "## What goes wrong\n\n"
    "Re-encrypting a SOPS dotenv file without --input-type=dotenv silently "
    "corrupts every value in the file, because sops defaults to treating the "
    "content as YAML/JSON and the dotenv trap breaks re-encryption badly. "
    "Every KEY=value line gets mangled the moment sops tries to parse it as "
    "structured data instead of a flat key-value list, and the damage is not "
    "always obvious until a downstream consumer fails to decrypt a specific "
    "secret. This has bitten more than one dept onboarding flow, always in "
    "the same shape: a re-encrypt step runs without the dotenv flags, and the\n"
    "file silently rots.\n\n"
    "## Fix\n\n"
    "Always pass --input-type=dotenv --output-type=dotenv on both encrypt "
    "and decrypt for a .env-shaped secrets file. Never rely on sops's default "
    "format auto-detection for a dotenv file -- it guesses wrong just often "
    "enough to be a real incident source, and the fix is always the same two "
    "explicit flags on every single invocation, scripted so a human never has "
    "to remember them by hand.\n\n"
    "## Related\n\n"
    "See the operator-set-secret tooling and the fleet's SOPS rotation "
    "runbook for the scripted wrapper that always passes these flags.\n"
)
REAL_QUERY = "What goes wrong re-encrypting a SOPS dotenv file without --input-type=dotenv?"


@pytest.fixture()
def cb(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "shared-wiki"
    page_path = wiki_dir / REAL_PAGE_REL
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(REAL_PAGE_CONTENT)

    monkeypatch.setenv("WIKI_SEARCH_WIKI_DIR", str(wiki_dir))
    monkeypatch.setenv("WIKI_SEARCH_CACHE_DIR", str(tmp_path / "cache"))

    sys.path.insert(0, str(TOOL_DIR))
    for mod_name in ("wiki_paths", "context_builder"):
        sys.modules.pop(mod_name, None)
    importlib.import_module("wiki_paths")
    context_builder = importlib.import_module("context_builder")
    try:
        yield context_builder
    finally:
        sys.modules.pop("context_builder", None)
        sys.modules.pop("wiki_paths", None)
        if str(TOOL_DIR) in sys.path:
            sys.path.remove(str(TOOL_DIR))


class TestVariantsMonotonic:
    """Each variant must be a strict token-cost superset of the previous one
    -- that's the whole point of having three budget tiers to compare."""

    def test_token_counts_increase_with_variant(self, cb):
        toks = {}
        for v in cb.VARIANTS:
            _, tok = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, v)
            toks[v] = tok
        assert toks["minimal"] < toks["outline_snip"]
        assert toks["outline_snip"] <= toks["summary"]

    def test_minimal_has_no_outline_or_snippet(self, cb):
        state, _ = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "minimal")
        assert "outline" not in state
        assert "matching_snippets" not in state
        assert "summary" not in state

    def test_outline_snip_has_no_summary_field(self, cb):
        state, _ = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "outline_snip")
        assert "outline" in state
        assert "matching_snippets" in state
        assert "summary" not in state

    def test_summary_variant_has_all_fields(self, cb):
        state, _ = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "summary")
        assert "outline" in state
        assert "matching_snippets" in state
        assert "summary" in state

    def test_never_sends_full_page_body(self, cb):
        full_page_tokens = cb.estimate_tokens(REAL_PAGE_CONTENT)
        for v in cb.VARIANTS:
            _, tok = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, v)
            assert tok < full_page_tokens, (
                f"{v} variant ({tok} tok) should be cheaper than the full page ({full_page_tokens} tok)")


class TestTokenBudgetCeiling:
    """Hard ceilings per candidate, so N candidates x variants never
    balloons into a large-context call by accident."""

    def test_minimal_under_150_tokens(self, cb):
        _, tok = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "minimal")
        assert tok < 150

    def test_outline_snip_under_400_tokens(self, cb):
        _, tok = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "outline_snip")
        assert tok < 400

    def test_summary_under_500_tokens(self, cb):
        _, tok = cb.build_candidate_state(REAL_PAGE_REL, REAL_QUERY, "summary")
        assert tok < 500


class TestSnippetExtraction:
    def test_matches_query_terms_when_present(self, cb):
        body = "intro filler text. " * 5 + "The SOPS dotenv trap breaks re-encryption badly. " + "trailer " * 20
        snippets = cb.matching_snippets(body, "SOPS dotenv trap")
        assert snippets
        assert "dotenv" in snippets[0].lower()

    def test_falls_back_to_page_start_when_no_match(self, cb):
        body = "Nothing related here at all, just filler content for a page. " * 3
        snippets = cb.matching_snippets(body, "completely unrelated xylophone query")
        assert snippets  # falls back to the start, never empty for non-empty body

    def test_empty_body_returns_empty(self, cb):
        assert cb.matching_snippets("", "anything") == []


class TestFrontmatterAndOutline:
    def test_frontmatter_roundtrip(self, cb):
        fm = cb.extract_frontmatter(REAL_PAGE_CONTENT)
        assert fm.get("owner") == "rnd"
        body = cb.strip_frontmatter(REAL_PAGE_CONTENT)
        assert not body.startswith("---")

    def test_outline_capped(self, cb):
        body = "\n".join(f"## heading {i}" for i in range(50))
        heads = cb.outline(body, max_headings=10)
        assert len(heads) == 10
