#!/usr/bin/env python3
"""Shared host-generic path resolution for the wiki-search tool (board #1505,
step 2 -- fleet standard).

WHY host-generic, not Rick-Mac-hardcoded: this tool ships from the framework
(`tools/wiki-search/`) and is vendored byte-for-byte into every dept's tree
(vendor-dept-libs.sh) -- Rick's Mac, other Macs, and every VPS dept
(agent-<slug>). Each host/dept already resolves its OWN copy of the shared
wiki at `$HOME/.claude/agent-memory/shared-wiki` (Mac: the real directory;
VPS depts since the #1120 uid-isolation cutover: a per-dept symlink into
`/var/lib/bubble-shared-wiki/<slug>/current`, kept fresh by
cloud-wiki-sync/wiki-transcript-sync -- verified on hetzner-root 2026-09-27
for ben/claudette/maya/morty/tony). So `Path.home() / ...` alone is already
correct on every host -- no per-host branching needed, same convention
`tools/wiki/wiki_search.py` (Rick's Mac-only prototype) and
`fts_only_index.py` (the step-1 research script) both already relied on.

Both WIKI_DIR and CACHE_DIR are overridable via environment variables for
testing and for any host where the convention doesn't apply.
"""
import os
from pathlib import Path

WIKI_DIR = Path(
    os.environ.get("WIKI_SEARCH_WIKI_DIR")
    or (Path.home() / ".claude/agent-memory/shared-wiki")
)

# Cache/index lives NEXT TO the wiki itself (same $HOME, every host) rather
# than under any one dept's own repo -- so the tool works identically whether
# it's invoked from the framework checkout, a vendored dept tree, or
# Rick_RnD's own workspace. Hidden (dot-prefixed) so it never shows up in a
# casual `ls` of agent-memory/ or gets mistaken for a wiki page.
CACHE_DIR = Path(
    os.environ.get("WIKI_SEARCH_CACHE_DIR")
    or (Path.home() / ".claude/agent-memory/.wiki-search-cache")
)

FTS_DB_PATH = CACHE_DIR / "fts.db"
HYBRID_DB_PATH = CACHE_DIR / "hybrid.db"
DEFAULT_RECEIPTS_PATH = CACHE_DIR / "receipts.jsonl"


def iter_wiki_files():
    """Yield every indexable wiki .md path (skips archive/), sorted for a
    stable iteration order. Shared by every indexer in this tool so the
    "what counts as a page" definition never drifts between fts_index.py and
    hybrid_index.py."""
    if not WIKI_DIR.is_dir():
        return
    for f in sorted(WIKI_DIR.rglob("*.md")):
        if "archive" in str(f.relative_to(WIKI_DIR)).split(os.sep):
            continue
        yield f


def wiki_signature():
    """Cheap (one stat per page) fingerprint of the wiki's current on-disk
    state: (file_count, max_mtime). Used to decide whether a cached index is
    stale without hashing content -- a rename/edit bumps mtime, a delete
    changes the count, so this catches every real change cheaply (~900
    pages stat in well under the 0.08s the step-1 results measured for a
    full FTS rebuild, so checking on every call is not worth avoiding)."""
    count = 0
    max_mtime = 0.0
    for f in iter_wiki_files():
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        count += 1
        if mtime > max_mtime:
            max_mtime = mtime
    return count, max_mtime
