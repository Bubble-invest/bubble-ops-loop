#!/usr/bin/env python3
"""Ollama + SQLite FTS5 hybrid index over the shared wiki -- ported from
Rick's Mac-only prototype (`~/claude-workspaces/Rick_RnD/tools/wiki/
wiki_search.py`) into the framework as the FLEET STANDARD's richer candidate
source. Only usable on a host that actually has a local Ollama daemon with
the `nomic-embed-text` model pulled (today: Rick's Mac only -- confirmed no
`ollama.service` on the VPS, 2026-09-27 probe). `search.py`'s auto-detect
picks this over `fts_index.py`'s dependency-light FTS-only pool whenever
`ollama_available()` is true; every other host transparently falls back to
fts_index.py.

Embedding + FTS5 logic here is otherwise UNCHANGED from wiki_search.py (same
SQL, same weighting) -- this is a port to a host-generic cache path
(`wiki_paths.HYBRID_DB_PATH`, not Rick_RnD/monitoring/wiki-search.db),
not a rewrite. wiki_search.py itself is untouched and remains Rick's own
Mac tool; this module is the one depts get vendored.

INTERNAL DATA ONLY -- see fts_index.py's module docstring for the
data-residency note (same wiki, same rule).
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wiki_paths import WIKI_DIR, HYBRID_DB_PATH, iter_wiki_files  # noqa: E402

OLLAMA_URL = "http://localhost:11434/api/embeddings"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
EMBED_MODEL = "nomic-embed-text"

_UPSERT_PAGE_SQL = """
    INSERT INTO pages (
        path, title, description, content, domain, embedding, indexed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        title = excluded.title,
        description = excluded.description,
        content = excluded.content,
        domain = excluded.domain,
        embedding = excluded.embedding,
        indexed_at = excluded.indexed_at
"""


def ollama_available(timeout: float = 0.5) -> bool:
    """Cheap, short-timeout probe: is a local Ollama daemon up AND does it
    have EMBED_MODEL pulled? Used by search.py's candidate-source auto-detect
    -- must fail fast (0.5s, not the default 10s embedding timeout) so a box
    with no Ollama at all (every VPS dept, most other Macs) doesn't stall a
    search waiting on a dead port."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL)
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        names = {m.get("name", "").split(":")[0] for m in data.get("models", [])}
        return EMBED_MODEL in names
    except Exception:
        return False


def get_embedding(text: str, timeout: float = 10) -> list:
    data = json.dumps({"model": EMBED_MODEL, "prompt": text[:2000]}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())["embedding"]
    except Exception as e:
        print(f"Ollama error: {e}", file=sys.stderr)
        return []


def cosine_sim(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def extract_frontmatter(content: str) -> dict:
    fm = {}
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if match:
        for line in match.group(1).split("\n"):
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
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            path TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            content TEXT,
            domain TEXT,
            embedding BLOB,
            indexed_at REAL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            path, title, description, content,
            content='pages',
            content_rowid='rowid'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, path, title, description, content)
            VALUES (new.rowid, new.path, new.title, new.description, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, path, title, description, content)
            VALUES ('delete', old.rowid, old.path, old.title, old.description, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, path, title, description, content)
            VALUES ('delete', old.rowid, old.path, old.title, old.description, old.content);
            INSERT INTO pages_fts(rowid, path, title, description, content)
            VALUES (new.rowid, new.path, new.title, new.description, new.content);
        END
    """)
    conn.commit()
    return conn


def upsert_page(conn: sqlite3.Connection, f: Path) -> bool:
    rel = str(f.relative_to(WIKI_DIR))
    content = f.read_text(errors="replace")
    title = extract_title(content, rel)
    desc = extract_description(content)
    domain = rel.split("/")[0] if "/" in rel else "root"
    embed_text = f"{title}\n{desc}\n{content[:500]}"
    embedding = get_embedding(embed_text)
    if not embedding:
        return False
    emb_blob = json.dumps(embedding).encode()
    conn.execute(
        _UPSERT_PAGE_SQL,
        (rel, title, desc, content[:5000], domain, emb_blob, time.time()),
    )
    return True


def freshen_index(conn: sqlite3.Connection, verbose: bool = False) -> int:
    stored = dict(conn.execute("SELECT path, indexed_at FROM pages").fetchall())
    on_disk = {str(f.relative_to(WIKI_DIR)): f for f in iter_wiki_files()}

    removed = [p for p in stored if p not in on_disk]
    changed = [(rel, f) for rel, f in on_disk.items()
               if stored.get(rel) is None or f.stat().st_mtime > stored[rel]]

    if not removed and not changed:
        return 0

    if verbose:
        print(f"Freshening wiki hybrid index: {len(changed)} new/changed, {len(removed)} removed...",
              file=sys.stderr)

    for rel in removed:
        conn.execute("DELETE FROM pages WHERE path = ?", (rel,))

    embedded = 0
    for rel, f in changed:
        if upsert_page(conn, f):
            embedded += 1
        elif verbose:
            print(f"  SKIP (no embedding, left stale): {rel}", file=sys.stderr)

    conn.commit()
    return embedded + len(removed)


def reindex(conn: sqlite3.Connection, verbose: bool = True):
    pages = []
    for f in iter_wiki_files():
        rel = str(f.relative_to(WIKI_DIR))
        content = f.read_text(errors="replace")
        pages.append((rel, content))

    conn.execute("DELETE FROM pages")
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('delete-all')")
    conn.commit()

    n_ok = 0
    for rel, content in pages:
        title = extract_title(content, rel)
        desc = extract_description(content)
        domain = rel.split("/")[0] if "/" in rel else "root"
        embed_text = f"{title}\n{desc}\n{content[:500]}"
        embedding = get_embedding(embed_text)
        if not embedding:
            if verbose:
                print(f"  SKIP (no embedding): {rel}", file=sys.stderr)
            continue
        emb_blob = json.dumps(embedding).encode()
        conn.execute(
            _UPSERT_PAGE_SQL,
            (rel, title, desc, content[:5000], domain, emb_blob, time.time()),
        )
        n_ok += 1
    conn.commit()
    return n_ok


def search_vector(conn: sqlite3.Connection, query_emb: list, top_k: int = 5) -> list:
    results = []
    rows = conn.execute("SELECT path, title, description, domain, embedding FROM pages").fetchall()
    for path, title, desc, domain, emb_blob in rows:
        emb = json.loads(emb_blob)
        score = cosine_sim(query_emb, emb)
        results.append((score, path, title, desc, domain))
    results.sort(key=lambda x: -x[0])
    return results[:top_k]


def search_keyword(conn: sqlite3.Connection, query: str, top_k: int = 5) -> list:
    safe_query = re.sub(r"[^\w\s]", " ", query)
    tokens = safe_query.split()
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    try:
        rows = conn.execute(
            """SELECT path, title, description,
                      rank * -1 as score
               FROM pages_fts
               WHERE pages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, top_k)
        ).fetchall()
        return [(row[3], row[0], row[1], row[2], "") for row in rows]
    except sqlite3.OperationalError:
        return []


def search_hybrid(conn: sqlite3.Connection, query: str, top_k: int = 5,
                   vector_weight: float = 0.7, keyword_weight: float = 0.3) -> list:
    query_emb = get_embedding(query)

    vec_results = search_vector(conn, query_emb, top_k=top_k * 2) if query_emb else []
    kw_results = search_keyword(conn, query, top_k=top_k * 2)

    range_vec = range_kw = 1
    min_vec = min_kw = 0
    if vec_results:
        max_vec = max(r[0] for r in vec_results)
        min_vec = min(r[0] for r in vec_results)
        range_vec = max_vec - min_vec if max_vec != min_vec else 1
    if kw_results:
        max_kw = max(r[0] for r in kw_results)
        min_kw = min(r[0] for r in kw_results)
        range_kw = max_kw - min_kw if max_kw != min_kw else 1

    scores = {}
    for score, path, title, desc, domain in vec_results:
        norm = (score - min_vec) / range_vec if vec_results else 0
        scores[path] = {"vec": norm, "kw": 0, "title": title, "desc": desc, "domain": domain}

    for score, path, title, desc, _ in kw_results:
        norm = (score - min_kw) / range_kw if kw_results else 0
        if path in scores:
            scores[path]["kw"] = norm
        else:
            scores[path] = {"vec": 0, "kw": norm, "title": title, "desc": desc, "domain": ""}

    ranked = []
    for path, s in scores.items():
        combined = s["vec"] * vector_weight + s["kw"] * keyword_weight
        ranked.append((combined, path, s["title"], s["desc"], s["domain"]))

    ranked.sort(key=lambda x: -x[0])
    return ranked[:top_k]


def search(query: str, top_k: int = 5, db_path: Path = HYBRID_DB_PATH, verbose: bool = False) -> list:
    """Convenience one-shot: ensure the index exists/is fresh, then search."""
    conn = init_db(db_path)
    count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    if count == 0:
        reindex(conn, verbose=verbose)
    else:
        freshen_index(conn, verbose=verbose)
    try:
        return search_hybrid(conn, query, top_k=top_k)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="*")
    ap.add_argument("--reindex", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--keyword-only", action="store_true")
    ap.add_argument("--vector-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(HYBRID_DB_PATH))
    args = ap.parse_args()

    db_path = Path(args.db)
    conn = init_db(db_path)

    if args.reindex:
        reindex(conn)
        return

    query = " ".join(args.query)
    if not query:
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        print(f"Wiki hybrid index: {count} pages." if count else
              "Index is empty. Run: python3 hybrid_index.py --reindex")
        return

    count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    if count == 0:
        reindex(conn, verbose=True)
    else:
        freshen_index(conn, verbose=True)

    if args.keyword_only:
        results = search_keyword(conn, query, args.top)
    elif args.vector_only:
        query_emb = get_embedding(query)
        results = search_vector(conn, query_emb, args.top) if query_emb else []
    else:
        results = search_hybrid(conn, query, args.top)

    if args.json:
        print(json.dumps([
            {"score": s, "path": p, "title": t, "description": d, "domain": dm}
            for s, p, t, d, dm in results
        ], indent=2))
    else:
        for i, (score, path, title, desc, domain) in enumerate(results, 1):
            print(f"{i}. [{domain}] {title}\n   Path: {path}\n   Score: {score:.3f}\n")

    conn.close()


if __name__ == "__main__":
    main()
