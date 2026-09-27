#!/usr/bin/env python3
"""Dependency-light FTS5-only index over the shared wiki -- built DIRECTLY
from markdown files, with NO Ollama / embedding dependency at all.

Board #1505 step 2 (fleet standard). Ported from the step-1 research
(`~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/scripts/
fts_only_index.py`, see RESULTS_WIKI_RERANK_V2.md sec 2/4): FTS-only lands
within 1.5-2.3 points of the Ollama-backed hybrid candidate pool on every
headline accuracy metric at pool=5, builds in 0.08s and 4.7MB for the whole
905-page wiki, and needs nothing beyond the Python stdlib's sqlite3 -- so
this is the candidate source every VPS dept and every Mac without the local
Ollama embedding model uses (see hybrid_index.py for the richer, Ollama-
dependent alternative and search.py for the auto-detect logic that picks
between them).

Unlike the step-1 research script, this version is CACHED and auto-rebuilds
only when the wiki has actually changed (`wiki_paths.wiki_signature()`),
not on every call -- see `ensure_fresh()`.

INTERNAL DATA ONLY: the shared wiki is Bubble Invest's own internal
fleet-operations knowledge base (board cards, incident notes, dept
architecture). Per Joris's data-residency rule (Telegram msg 9715,
2026-09-25, condensed in skills/system-one-decisions/SKILL.md "Backend
choice"), reading and indexing it locally is unrestricted; the OpenRouter
Jev rerank pass this feeds (see rerank.py/jev_bridge.py) is likewise cleared
for this data because it never leaves the internal-data bucket. Never point
this indexer at an external-client wiki/knowledge base without re-checking
that rule.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wiki_paths import WIKI_DIR, FTS_DB_PATH, iter_wiki_files, wiki_signature  # noqa: E402


def extract_frontmatter(content: str) -> dict:
    fm = {}
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if m:
        for line in m.group(1).split("\n"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                fm[parts[0].strip()] = parts[1].strip().strip("'\"")
    return fm


def extract_title(content: str, path: str) -> str:
    fm = extract_frontmatter(content)
    if "title" in fm:
        return fm["title"]
    if "name" in fm:
        return fm["name"]
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return path


def extract_description(content: str) -> str:
    fm = extract_frontmatter(content)
    if "description" in fm:
        return fm["description"]
    in_fm = False
    for line in content.split("\n"):
        if line.strip() == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("|") and len(line) > 20:
            return line[:200]
    return ""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()  # always a clean full rebuild -- cheap enough (0.08s/905 pages) that
                           # incremental-update machinery isn't worth the complexity here.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE pages (
            path TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            domain TEXT
        )
    """)
    # Same FTS5 column shape as the (Ollama-backed) hybrid index's own
    # pages_fts, so BM25 ranking behavior is directly comparable -- a plain,
    # self-contained (non-external-content) FTS5 table since this is always
    # a full rebuild, never an incremental update.
    conn.execute("""
        CREATE VIRTUAL TABLE pages_fts USING fts5(path, title, description, content)
    """)
    conn.execute("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)
    """)
    conn.commit()
    return conn


def build(db_path: Path = FTS_DB_PATH, verbose: bool = True):
    t0 = time.time()
    conn = init_db(db_path)
    n = 0
    for f in iter_wiki_files():
        rel = str(f.relative_to(WIKI_DIR))
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        title = extract_title(content, rel)
        desc = extract_description(content)
        domain = rel.split("/")[0] if "/" in rel else "root"
        conn.execute(
            "INSERT INTO pages_fts (path, title, description, content) VALUES (?, ?, ?, ?)",
            (rel, title, desc, content[:5000]),  # matches the hybrid index's own indexed content length
        )
        conn.execute(
            "INSERT INTO pages (path, title, description, domain) VALUES (?, ?, ?, ?)",
            (rel, title, desc, domain),
        )
        n += 1
    count, max_mtime = wiki_signature()
    conn.execute("INSERT INTO meta (key, value) VALUES ('file_count', ?)", (str(count),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('max_mtime', ?)", (str(max_mtime),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)", (str(time.time()),))
    conn.commit()
    elapsed = time.time() - t0
    conn.close()
    size_bytes = db_path.stat().st_size
    if verbose:
        print(f"Built FTS5-only index: {n} pages in {elapsed:.2f}s -> {db_path} "
              f"({size_bytes / 1024 / 1024:.2f} MB)", file=sys.stderr)
    return {"n_pages": n, "elapsed_s": elapsed, "size_bytes": size_bytes, "db_path": str(db_path)}


def _is_stale(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(str(db_path))
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        conn.close()
    except sqlite3.Error:
        return True  # corrupt/incompatible DB -- rebuild rather than error
    count, max_mtime = wiki_signature()
    try:
        stale = (int(rows.get("file_count", -1)) != count
                 or float(rows.get("max_mtime", -1)) != max_mtime)
    except (TypeError, ValueError):
        return True
    return stale


def ensure_fresh(db_path: Path = FTS_DB_PATH, verbose: bool = False):
    """Rebuild the FTS-only index iff the wiki's on-disk signature (file
    count + max mtime) has changed since the index was last built, or the
    index doesn't exist yet. A no-op stat-only check otherwise."""
    if _is_stale(db_path):
        build(db_path, verbose=verbose)


def search_fts_only(conn: sqlite3.Connection, query: str, top_k: int = 10) -> list:
    """Returns (score, path, title, desc, domain) tuples -- SAME shape the
    hybrid index's search functions return, so downstream code (rerank.py,
    search.py) works unchanged against either candidate source. Pure BM25
    keyword search, no embeddings."""
    safe_query = re.sub(r"[^\w\s]", " ", query)
    tokens = safe_query.split()
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    try:
        rows = conn.execute(
            """SELECT pages_fts.path, pages_fts.title, pages_fts.description, rank * -1 as score
               FROM pages_fts
               WHERE pages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, top_k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for path, title, desc, score in rows:
        domain = path.split("/")[0] if "/" in path else "root"
        out.append((score, path, title, desc, domain))
    return out


def search(query: str, top_k: int = 10, db_path: Path = FTS_DB_PATH, verbose: bool = False) -> list:
    """Convenience one-shot: ensure the index is fresh, then search."""
    ensure_fresh(db_path, verbose=verbose)
    conn = sqlite3.connect(str(db_path))
    try:
        return search_fts_only(conn, query, top_k=top_k)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true", help="Force a full rebuild")
    ap.add_argument("--query", default=None)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--db", default=str(FTS_DB_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    if args.build:
        stats = build(db_path)
        print(json.dumps(stats))
    else:
        ensure_fresh(db_path, verbose=True)

    if args.query:
        conn = sqlite3.connect(str(db_path))
        results = search_fts_only(conn, args.query, args.top)
        conn.close()
        if args.json:
            print(json.dumps([
                {"score": s, "path": p, "title": t, "description": d, "domain": dm}
                for s, p, t, d, dm in results
            ], indent=2))
        else:
            for score, path, title, desc, domain in results:
                print(f"{score:.3f}  [{domain}] {title}  ({path})")


if __name__ == "__main__":
    main()
