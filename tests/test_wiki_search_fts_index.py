"""tools/wiki-search/fts_index.py -- the dependency-light FTS5-only
candidate source (board #1505 step 2). Ported/adapted from the step-1
research's `tests/test_fts_only_index.py`
(~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/) with one
change: this version builds a SYNTHETIC temp wiki instead of reading Rick's
real `~/.claude/agent-memory/shared-wiki`, so it's hermetic and runs in CI
(the real wiki doesn't exist on a GitHub Actions runner). Uses the
WIKI_SEARCH_WIKI_DIR / WIKI_SEARCH_CACHE_DIR env-var overrides that
tools/wiki-search/wiki_paths.py exists specifically to support.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sqlite3
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = REPO / "tools" / "wiki-search"

_PAGES = {
    "shared/systems/alpha.md": (
        "---\ntitle: Alpha System\ntype: reference\nowner: rnd\n"
        "last_verified: 2026-09-01\n---\n"
        "# Alpha System\n\nHow the kanban dead letter queue works for morty.\n"
        "## Details\nMore body text about the alpha system and its retries.\n"
    ),
    "shared/systems/beta.md": (
        "---\ntitle: Beta System\n---\n"
        "# Beta System\n\nA completely unrelated page about the fleet wiki "
        "search rollout and its own architecture.\n"
    ),
    "rick_rnd/hot.md": (
        "# Rick hot cache\n\nRecent notable actions, most recent first.\n"
    ),
    "archive/old.md": (
        "# Should never be indexed\n\narchive pages are always skipped.\n"
    ),
}


@pytest.fixture()
def wiki_env(tmp_path, monkeypatch):
    """Builds a synthetic wiki dir + a fresh cache dir, points the tool's
    env-var overrides at them, and (re)imports fts_index/wiki_paths so every
    module-level path constant reflects the synthetic tree -- then restores
    sys.modules afterwards so this test's monkeypatching can't leak into
    other test files that import the same tool package."""
    wiki_dir = tmp_path / "shared-wiki"
    for rel, content in _PAGES.items():
        p = wiki_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cache_dir = tmp_path / "cache"

    monkeypatch.setenv("WIKI_SEARCH_WIKI_DIR", str(wiki_dir))
    monkeypatch.setenv("WIKI_SEARCH_CACHE_DIR", str(cache_dir))

    sys.path.insert(0, str(TOOL_DIR))
    for mod_name in ("wiki_paths", "fts_index"):
        sys.modules.pop(mod_name, None)
    wiki_paths = importlib.import_module("wiki_paths")
    fts_index = importlib.import_module("fts_index")
    try:
        yield wiki_dir, cache_dir, fts_index, wiki_paths
    finally:
        sys.modules.pop("fts_index", None)
        sys.modules.pop("wiki_paths", None)
        if str(TOOL_DIR) in sys.path:
            sys.path.remove(str(TOOL_DIR))


def test_build_produces_nonempty_index_skipping_archive(wiki_env):
    wiki_dir, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    stats = fts_index.build(db_path, verbose=False)
    assert stats["n_pages"] == 3  # 4 pages minus archive/old.md
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    assert n == 3
    paths = {row[0] for row in conn.execute("SELECT path FROM pages").fetchall()}
    assert "archive/old.md" not in paths
    conn.close()


def test_build_is_fast(wiki_env):
    _, cache_dir, fts_index, _ = wiki_env
    stats = fts_index.build(cache_dir / "fts.db", verbose=False)
    assert stats["elapsed_s"] < 5.0


def test_search_returns_expected_tuple_shape(wiki_env):
    _, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    fts_index.build(db_path, verbose=False)
    conn = sqlite3.connect(str(db_path))
    results = fts_index.search_fts_only(conn, "kanban dead letter queue morty", top_k=5)
    conn.close()
    assert results
    for row in results:
        assert len(row) == 5  # (score, path, title, desc, domain)
        score, path, title, desc, domain = row
        assert isinstance(path, str)
        assert path.endswith(".md")
    assert results[0][1] == "shared/systems/alpha.md"


def test_search_respects_top_k(wiki_env):
    _, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    fts_index.build(db_path, verbose=False)
    conn = sqlite3.connect(str(db_path))
    results = fts_index.search_fts_only(conn, "system wiki fleet", top_k=1)
    conn.close()
    assert len(results) <= 1


def test_search_empty_query_returns_empty(wiki_env):
    _, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    fts_index.build(db_path, verbose=False)
    conn = sqlite3.connect(str(db_path))
    assert fts_index.search_fts_only(conn, "", top_k=5) == []
    conn.close()


def test_search_punctuation_only_returns_empty_not_crash(wiki_env):
    _, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    fts_index.build(db_path, verbose=False)
    conn = sqlite3.connect(str(db_path))
    assert fts_index.search_fts_only(conn, "???...", top_k=5) == []
    conn.close()


def test_ensure_fresh_rebuilds_only_when_wiki_changes(wiki_env):
    wiki_dir, cache_dir, fts_index, _ = wiki_env
    db_path = cache_dir / "fts.db"
    fts_index.build(db_path, verbose=False)
    mtime_after_build = db_path.stat().st_mtime

    # No change on disk -> ensure_fresh must be a no-op (index file untouched).
    fts_index.ensure_fresh(db_path, verbose=False)
    assert db_path.stat().st_mtime == mtime_after_build

    # A new page appears -> ensure_fresh must rebuild.
    (wiki_dir / "shared" / "systems" / "gamma.md").write_text("# Gamma\nnew page\n")
    fts_index.ensure_fresh(db_path, verbose=False)
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    conn.close()
    assert n == 4


def test_extract_title_prefers_frontmatter(wiki_env):
    _, _, fts_index, _ = wiki_env
    content = "---\ntitle: My Title\n---\n# Fallback heading\n"
    assert fts_index.extract_title(content, "some/path.md") == "My Title"


def test_extract_title_falls_back_to_heading(wiki_env):
    _, _, fts_index, _ = wiki_env
    content = "---\nowner: x\n---\n# The Real Heading\nbody\n"
    assert fts_index.extract_title(content, "some/path.md") == "The Real Heading"


def test_extract_description_prefers_frontmatter(wiki_env):
    _, _, fts_index, _ = wiki_env
    content = "---\ndescription: A short desc\n---\n# Title\nbody\n"
    assert fts_index.extract_description(content) == "A short desc"
