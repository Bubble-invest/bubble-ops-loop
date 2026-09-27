#!/usr/bin/env python3
"""Rerank a wiki-search candidate pool for one query using Jev (openrouter
backend). Ported near-verbatim from the step-1 research (`rerank.py` in
`~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/scripts/`) --
only the imports changed (`context_builder`/`jev_bridge` are this tool's own
modules now). Two question designs, both respecting
`skills/system-one-decisions/references/contract.md`'s rules (explicit
no-match option on Choice; atomic, single-judgment questions; host-owned
menu -- code builds the candidate list, the model only picks among it):

  noul   -- one independent yes/no ("does `cX` answer the query?") per
            candidate, ALL batched into one call. Ranking = sort candidates
            by their own noul probability.

  choice -- one `choice` question over all candidate ids + "none_relevant".
            Ranking = sort candidates by their probability in the returned
            distribution. THIS IS THE DEFAULT (search.py) -- validated on a
            fresh held-out test set (RESULTS_WIKI_RERANK_V2.md): P@1 0.900,
            P@3 0.879, hit@3 0.923, MRR 0.912 at pool=5, plus the cleanest
            no-answer suppression of any config tested (0/10 no-answer
            queries returned a page).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from context_builder import build_candidate_state  # noqa: E402
from jev_bridge import ask_openrouter, SpendCap  # noqa: E402


def _cand_ids(candidates):
    return [f"c{i}" for i in range(len(candidates))]


def build_state_and_ids(candidates, query, variant):
    """candidates: list of (path, title, domain) tuples, in candidate-pool
    rank order. Returns (state_dict, id_to_path, total_tokens)."""
    ids = _cand_ids(candidates)
    state = {"query": query, "candidates": {}}
    id_to_path = {}
    total_tokens = 0
    for cid, (path, title, domain) in zip(ids, candidates):
        cand_state, tok = build_candidate_state(path, query, variant, title=title, domain=domain)
        # path is dropped from what the model reads under the candidate id
        # (kept in our own id_to_path map instead) -- the model judges the
        # DESCRIPTIVE fields, not memorized path strings.
        cand_state.pop("path", None)
        state["candidates"][cid] = cand_state
        id_to_path[cid] = path
        total_tokens += tok
    return state, id_to_path, total_tokens


def noul_questions(ids):
    q = {"_version": "wiki-rerank-noul-v1"}
    for cid in ids:
        q[f"rel_{cid}"] = {
            "type": "noul",
            "instructions": (
                f"Does the wiki page described at state.candidates.{cid} answer "
                f"state.query -- i.e. would a person reading that page find the "
                f"answer to the query? Judge only from the fields given (title, "
                f"frontmatter, outline, snippets, summary), not from the id or path."
            ),
        }
    return q


def choice_questions(ids):
    criteria = {cid: f"the wiki page described at state.candidates.{cid}" for cid in ids}
    criteria["none_relevant"] = "none of the candidate pages actually answer state.query"
    return {
        "_version": "wiki-rerank-choice-v1",
        "relevant": {
            "type": "choice",
            "instructions": (
                "Which ONE candidate page in state.candidates best answers "
                "state.query? Judge each candidate only from its given fields "
                "(title, frontmatter, outline, snippets, summary). If none of "
                "them actually answer the query, pick none_relevant."
            ),
            "criteria": criteria,
        },
    }


def rerank_noul(candidates, query, variant, spend_cap: SpendCap):
    state, id_to_path, tokens = build_state_and_ids(candidates, query, variant)
    ids = list(id_to_path)
    questions = noul_questions(ids)
    receipt = ask_openrouter(state, questions, spend_cap)
    ranked = []
    if receipt["ok"]:
        answers = receipt["response"].get("answers", {})
        scored = []
        for cid in ids:
            p = (answers.get(f"rel_{cid}") or {}).get("noul")
            scored.append((id_to_path[cid], p if p is not None else 0.0))
        scored.sort(key=lambda t: -t[1])
        ranked = scored
    return ranked, receipt, tokens


def rerank_choice(candidates, query, variant, spend_cap: SpendCap):
    state, id_to_path, tokens = build_state_and_ids(candidates, query, variant)
    ids = list(id_to_path)
    questions = choice_questions(ids)
    receipt = ask_openrouter(state, questions, spend_cap)
    ranked = []
    if receipt["ok"]:
        answers = receipt["response"].get("answers", {})
        probs = (answers.get("relevant") or {}).get("probabilities") or {}
        scored = [(id_to_path[cid], probs.get(cid, 0.0)) for cid in ids]
        scored.sort(key=lambda t: -t[1])
        ranked = scored
    return ranked, receipt, tokens


def apply_threshold(scored, threshold):
    """scored: [(path, prob), ...] sorted desc. Returns just the paths whose
    prob >= threshold, in order (dropping items below a confidence floor is
    a feature, not a bug: the reranker may return fewer than N)."""
    return [p for p, prob in scored if prob >= threshold]
