#!/usr/bin/env python3
"""wiki-search -- fleet-standard search over the shared wiki
(`~/.claude/agent-memory/shared-wiki`), with an optional Jev rerank pass.

Board `Bubble-invest/bubble-ops-board#1505`, step 2: promotes the step-1
research (`~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/`,
RESULTS_WIKI_RERANK_V2.md) to a fleet standard, vendored into every dept via
vendor-dept-libs.sh (see this dir's README.md).

CANDIDATE SOURCE (auto-detected unless --candidate-source overrides it):
  hybrid    Ollama (nomic-embed-text) + FTS5, weighted 0.7/0.3 -- richer
            retrieval, needs a local Ollama daemon with the model pulled.
            Today that's Rick's Mac only (verified: no ollama.service on
            the VPS, 2026-09-27).
  fts_only  Pure SQLite FTS5, built directly from the wiki markdown, zero
            network/model dependency. Within ~1.5-2.3 points of hybrid on
            every headline accuracy metric at pool=5 (RESULTS_WIKI_RERANK_V2.md
            sec 3) -- the config every VPS dept and every Mac without Ollama
            uses. Auto-detect probes hybrid_index.ollama_available() (a
            0.5s-timeout localhost check) and falls back to this instantly
            if it's not there.

RERANK (ON by default, pool=5, `choice` design, `outline_snip` state
variant, threshold 0.15 -- the winning config from RESULTS_WIKI_RERANK_V2.md
sec 5, validated on a fresh 130-row held-out gold set never touched during
tuning): pool=5 dominates pool=10 on BOTH accuracy (P@1 .900 vs .885, P@3
.879 vs .862, hit@3 .923 vs .915, MRR .912 vs .899) AND net token savings
(58.8% vs 33.7%), and has the cleanest no-answer behavior of any config
tested (0/10 fresh-test no-good-page queries returned a page). Use
--no-rerank to get the plain candidate-source top-N unchanged.

HARD FALLBACK: reranking is a pure enhancement layer that can never make a
real query come back empty or crash. If --rerank is on (the default) but no
OpenRouter key is available, the Jev call errors/times out, or every
candidate scores below --rerank-threshold, this prints a one-line warning to
stderr and returns the plain candidate-source top-N UNCHANGED. The only way
this tool returns zero results for a real query is if the candidate source
itself found nothing (a genuinely empty wiki, or a query with no matchable
tokens) -- reranking is never the reason a search comes back empty.

RECEIPTS: one JSONL row per reranked query, matching jev.py `ask`'s own
receipt shape (state_digest, question_set_version, backend, model, ok,
cost_usd, latency, timestamp -- see `references/contract.md` sec 5 in
skills/system-one-decisions/, cited by board #1505's own receipts contract).
No rows are written when --no-rerank is passed (nothing was decided) or when
--receipts '' disables it explicitly.

INTERNAL DATA ONLY: the shared wiki is Bubble Invest's own internal
fleet-operations knowledge base. Per Joris's data-residency rule (Telegram
msg 9715, 2026-09-25; skills/system-one-decisions/SKILL.md "Backend
choice"), it's cleared for the OpenRouter Jev backend used here. Never point
this tool at an external-client wiki/knowledge base without re-checking that
rule.
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fts_index  # noqa: E402
import hybrid_index  # noqa: E402
from wiki_paths import DEFAULT_RECEIPTS_PATH, FTS_DB_PATH, HYBRID_DB_PATH  # noqa: E402

DEFAULT_VARIANT = "outline_snip"
DEFAULT_DESIGN = "choice"
DEFAULT_THRESHOLD = 0.15
DEFAULT_RERANK_TOP_N = 5
DEFAULT_MAX_SPEND = 0.05


def detect_candidate_source() -> str:
    """auto: hybrid iff a local Ollama daemon + nomic-embed-text are up
    (0.5s-timeout probe), else fts_only. Never raises."""
    try:
        if hybrid_index.ollama_available():
            return "hybrid"
    except Exception:
        pass
    return "fts_only"


def get_candidates(candidate_source: str, query: str, top_k: int, verbose: bool = False) -> list:
    """Returns (score, path, title, desc, domain) tuples regardless of
    source -- callers never need to branch on which one ran."""
    if candidate_source == "hybrid":
        return hybrid_index.search(query, top_k=top_k, db_path=HYBRID_DB_PATH, verbose=verbose)
    return fts_index.search(query, top_k=top_k, db_path=FTS_DB_PATH, verbose=verbose)


def rerank_or_fallback(candidates, query, top, rerank_top_n, variant, design, threshold,
                        max_spend, receipts_path):
    """candidates: the FULL candidate-source hit list (already capped to
    max(top, rerank_top_n) by the caller). Returns (results, used_rerank,
    warning) -- `results` is the SAME (score, path, title, desc, domain)
    tuple shape either way."""
    if not candidates:
        return [], False, None

    try:
        from jev_bridge import load_openrouter_key_into_env, SpendCap, append_receipt
        from rerank import rerank_noul, rerank_choice, apply_threshold
    except Exception as e:  # pragma: no cover -- import-time failure is still a fallback path
        return candidates[:top], False, f"rerank modules unavailable ({e}); falling back to plain candidates"

    if not load_openrouter_key_into_env():
        return candidates[:top], False, "no OpenRouter key available; falling back to plain candidates"

    pool = [(h[1], h[2], h[4]) for h in candidates[:rerank_top_n]]
    rerank_fn = rerank_choice if design == "choice" else rerank_noul
    cap = SpendCap(max_spend=max_spend)

    t0 = time.time()
    try:
        scored, receipt, tokens = rerank_fn(pool, query, variant, cap)
    except Exception as e:
        return candidates[:top], False, f"rerank call raised ({e}); falling back to plain candidates"
    latency = time.time() - t0

    if receipts_path:
        append_receipt(receipts_path, {
            "tool": "wiki-search", "query": query, "variant": variant, "design": design,
            "threshold": threshold, "rerank_top_n": rerank_top_n,
            "backend": receipt.get("backend"), "ok": receipt.get("ok"),
            "state_digest": receipt.get("state_digest"),
            "question_set_version": receipt.get("question_set_version"),
            "model": receipt.get("model"), "cost_usd": receipt.get("cost_usd"),
            "latency_s": latency, "timestamp": receipt.get("timestamp"),
            "n_candidates": len(pool),
        })

    if not receipt.get("ok"):
        return candidates[:top], False, f"Jev call failed ({receipt.get('error')}); falling back to plain candidates"

    kept_paths = apply_threshold(scored, threshold)
    if not kept_paths:
        # every candidate scored below threshold -- fall back rather than
        # return nothing, so a rerank false-negative can never make search
        # go silent for a caller that doesn't distinguish "no results" from
        # "reranker was over-cautious".
        return candidates[:top], False, "all candidates scored below --rerank-threshold; falling back to plain candidates"

    by_path = {h[1]: h for h in candidates}
    reranked = [by_path[p] for p in kept_paths if p in by_path][:top]
    return reranked, True, None


def format_results(results: list) -> str:
    if not results:
        return "No results found."
    lines = []
    for i, (score, path, title, desc, domain) in enumerate(results, 1):
        lines.append(f"{i}. [{domain}] {title}")
        lines.append(f"   Path: {path}")
        if desc:
            lines.append(f"   {desc[:150]}")
        lines.append(f"   Score: {score:.3f}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Fleet wiki search + optional Jev rerank (board #1505)")
    parser.add_argument("query", nargs="*", help="Search query")
    parser.add_argument("--reindex", action="store_true", help="Force a full rebuild of the active candidate index")
    parser.add_argument("--top", type=int, default=5, help="Number of results to return")
    parser.add_argument("--candidate-source", choices=["auto", "hybrid", "fts_only"], default="auto",
                        help="'auto' (default) picks hybrid iff a local Ollama daemon + nomic-embed-text "
                             "are reachable, else fts_only. Override to force one.")
    parser.add_argument("--no-rerank", action="store_true", help="Disable the Jev rerank pass (plain candidate-source top-N)")
    parser.add_argument("--rerank-top-n", type=int, default=DEFAULT_RERANK_TOP_N,
                         help="Candidate pool size fed to the reranker (default 5 -- validated, see module docstring)")
    parser.add_argument("--rerank-variant", choices=["minimal", "outline_snip", "summary"], default=DEFAULT_VARIANT)
    parser.add_argument("--rerank-design", choices=["noul", "choice"], default=DEFAULT_DESIGN)
    parser.add_argument("--rerank-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND, help="Hard cap (USD) on this run's OpenRouter spend")
    parser.add_argument("--receipts", default=str(DEFAULT_RECEIPTS_PATH),
                         help="Append one JSONL receipt per reranked query here. Pass '' to disable.")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    candidate_source = args.candidate_source
    if candidate_source == "auto":
        candidate_source = detect_candidate_source()

    if args.reindex:
        if candidate_source == "hybrid":
            conn = hybrid_index.init_db(HYBRID_DB_PATH)
            hybrid_index.reindex(conn, verbose=True)
            conn.close()
        else:
            fts_index.build(FTS_DB_PATH, verbose=True)
        return

    query = " ".join(args.query)
    if not query:
        print(f"wiki-search: candidate source = {candidate_source}. Pass a query to search, "
              f"or --reindex to force a rebuild.")
        return

    rerank = not args.no_rerank
    pool_size = max(args.top, args.rerank_top_n) if rerank else args.top
    candidates = get_candidates(candidate_source, query, pool_size, verbose=True)

    used_rerank, warning = False, None
    if rerank:
        receipts_path = args.receipts or None
        results, used_rerank, warning = rerank_or_fallback(
            candidates, query, args.top, args.rerank_top_n, args.rerank_variant,
            args.rerank_design, args.rerank_threshold, args.max_spend, receipts_path)
    else:
        results = candidates[:args.top]

    if warning:
        print(f"wiki-search: {warning}", file=sys.stderr)

    if args.json:
        out = [{"score": s, "path": p, "title": t, "description": d, "domain": dm}
               for s, p, t, d, dm in results]
        print(json.dumps({
            "candidate_source": candidate_source,
            "reranked": used_rerank,
            "results": out,
        }, indent=2))
    else:
        print(f"[{candidate_source}{' + reranked' if used_rerank else (' (rerank fallback)' if rerank else '')}]")
        print(format_results(results))


if __name__ == "__main__":
    main()
