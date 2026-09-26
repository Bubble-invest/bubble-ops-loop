#!/usr/bin/env python3
"""Build compact per-candidate state for the Jev reranker (board #1505 step
2, ported near-verbatim from the step-1 research at
`~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/scripts/
context_builder.py` -- only the wiki-dir resolution changed, to the
host-generic `wiki_paths.WIKI_DIR`).

`outline_snip` is the variant validated in RESULTS_WIKI_RERANK_V2.md and is
this tool's default (see search.py): minimal + H1/H2 outline + the
snippet(s) around query terms. None of the three variants ever sends the
whole page body -- a full-page state would cost 10-100x the tokens for a
bounded judgment that doesn't need it, per system-one-decisions'
`references/contract.md` "minimum sufficient state".

Token counts are an approximation (len(text)/4) since tiktoken isn't a
fleet-wide dependency -- documented as an estimate everywhere it's reported.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wiki_paths import WIKI_DIR  # noqa: E402

_page_cache = {}


def _read_page(path):
    if path not in _page_cache:
        p = WIKI_DIR / path
        _page_cache[path] = p.read_text(errors="replace") if p.exists() else ""
    return _page_cache[path]


def extract_frontmatter(content):
    fm = {}
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if m:
        for line in m.group(1).split("\n"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                fm[parts[0].strip()] = parts[1].strip().strip("'\"")
    return fm


def strip_frontmatter(content):
    return re.sub(r"^---\n.*?\n---\n?", "", content, count=1, flags=re.DOTALL)


def outline(body, max_headings=10):
    heads = []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            heads.append(s)
        if len(heads) >= max_headings:
            break
    return heads


def lead_paragraph(body, max_chars=300):
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if p and not p.startswith("#") and len(p) > 20:
            return p[:max_chars]
    return ""


_WORD_RE = re.compile(r"[A-Za-z0-9_À-ÿ]+")


def _query_terms(query, min_len=3):
    stop = {"the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
            "what", "which", "who", "how", "why", "when", "for", "and", "or",
            "of", "in", "on", "to", "it", "its", "this", "that", "with"}
    terms = [w.lower() for w in _WORD_RE.findall(query) if len(w) >= min_len]
    return [t for t in terms if t not in stop]


def matching_snippets(body, query, window=220, max_snippets=2):
    """Find up to max_snippets windows of `body` around the first occurrences
    of query terms (longest terms first, so a distinctive word wins over a
    generic one), falling back to the page's own start if nothing matches."""
    terms = sorted(set(_query_terms(query)), key=len, reverse=True)
    lower_body = body.lower()
    found = []
    used_spans = []
    for t in terms:
        idx = lower_body.find(t)
        if idx == -1:
            continue
        if any(abs(idx - s) < window for s in used_spans):
            continue
        start = max(0, idx - window // 3)
        end = min(len(body), idx + window)
        snippet = body[start:end].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        found.append(snippet)
        used_spans.append(idx)
        if len(found) >= max_snippets:
            break
    if not found:
        snippet = re.sub(r"\s+", " ", body[:window]).strip()
        if snippet:
            found.append(snippet)
    return found


def estimate_tokens(obj):
    """len(text)/4 approximation, applied to the canonical str() of the
    object -- documented as an estimate, not an exact model tokenizer count."""
    import json
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return max(1, len(s) // 4)


def build_candidate_state(path, query, variant, title=None, domain=None):
    """Return (state_dict, token_estimate) for one candidate page under the
    given variant ('minimal' | 'outline_snip' | 'summary')."""
    content = _read_page(path)
    fm = extract_frontmatter(content)
    body = strip_frontmatter(content)

    state = {
        "path": path,
        "domain": domain or (path.split("/")[0] if "/" in path else "root"),
        "title": title or fm.get("title") or fm.get("name") or path,
    }
    fm_fields = {k: fm[k] for k in ("type", "owner", "last_verified") if k in fm}
    if fm_fields:
        state["frontmatter"] = fm_fields

    if variant in ("outline_snip", "summary"):
        state["outline"] = outline(body)
        state["matching_snippets"] = matching_snippets(body, query)

    if variant == "summary":
        summary = fm.get("description") or lead_paragraph(body)
        if summary:
            state["summary"] = summary

    return state, estimate_tokens(state)


VARIANTS = ("minimal", "outline_snip", "summary")
