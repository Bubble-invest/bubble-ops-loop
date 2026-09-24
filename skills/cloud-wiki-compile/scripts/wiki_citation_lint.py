#!/usr/bin/env python3
"""Deterministic citation lint for unsourced operator-intent claims.

Board #1483/#1485: free-text self-notes (CronCreate wake prompts, subagent
briefs, HANDOFF.md, compaction summaries) have no enforced citation
requirement for operator-intent claims. One unsourced "operator flagged
spend -- be cost-conscious" (Ben, 2026-09-11) got copied forward into ~36
wake prompts and ~15 subagent briefs before anyone caught it, and silently
narrowed a mission deliverable for 12 days. The existing STEP 4.8 intent-drift
extractor (an agentic, weekly-only Task subagent) already proved the fleet
can catch this kind of drift (board #1350/#1354) -- this module generalizes
the SAME signal into something deterministic, cheap, and testable enough to
run on EVERY compile, not just the weekly digest.

This is a REGEX pass, not a reading-judgment pass. It deliberately does not
decide whether a flagged sentence is TRUE operator intent or a hallucination
-- like `wiki_intent_audit.py`, it reports structural evidence only (a
sentence shaped like an operator-attribution claim with no citation nearby);
a human (via the emitted needs:human card) makes the semantic call. Unlike
every OTHER extractor in cloud-wiki-compile/SKILL.md, this one needs no model
at all: same injection-safety property `wiki_intent_audit.py` has (a
transcript cannot argue its way past a fixed pattern the way it might sway a
reading model), and it is unit-testable in isolation.

IMPORTANT (board #1485 constraint): this module only ever reads the ALREADY
REDUCED feed text the wiki-compile delta planner (`wiki_delta.py`) hands to
every other extractor (`aggregate_feed` / `weekly_aggregate_feed`, the exact
`[NEW ...]` / `[CONTEXT_ONLY ...]` tagged rows STEP 4 already produced from
one pass over each JSONL). It never re-opens, re-scans, or widens access to
raw transcripts, HANDOFF.md, or any VPS path outside what the delta plan
already resolved -- see SKILL.md STEP 4.7b for the call site.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Claim pattern: (operator|Joris|Jade) (said|flagged|wants|asked|told|decided)
# -- plus the French equivalents the fleet also uses in transcripts/notes.
# ---------------------------------------------------------------------------

_SUBJECT = r"(?:the\s+)?(?:operator|Joris|Jade|l['’]op[ée]rateur)"
_VERB_EN = r"(?:said|flagged|wants?|asked|told|decided)"
_VERB_FR = (
    r"(?:a\s+dit|a\s+signal[ée]e?|a\s+flagu[ée]e?|veut|a\s+demand[ée]e?|"
    r"a\s+racont[ée]e?|a\s+pr[ée]cis[ée]e?|a\s+d[ée]cid[ée]e?|"
    r"a\s+confirm[ée]e?|a\s+valid[ée]e?)"
)

# Subject and verb may be separated by a short qualifier ("Joris explicitly
# said", "the operator apparently flagged") -- up to 60 chars, never crossing
# a sentence boundary (the string this runs against is already one sentence).
CLAIM_RE = re.compile(
    rf"\b{_SUBJECT}\b[^.!?\n]{{0,60}}?\b(?:{_VERB_EN}|{_VERB_FR})\b",
    re.IGNORECASE,
)

# A "sourced" claim carries a msg id, a tg id, an explicit message_id
# reference, or a dated citation (ISO date, optionally with a time) nearby.
CITATION_RE = re.compile(
    r"\bmsg\s?\d+\b"
    r"|\btg\s?\d+\b"
    r"|\bmessage[_ ]id\b(?:\s*[:=]\s*\d+)?"
    r"|\b\d{4}-\d{2}-\d{2}(?:[Tt]\d{2}:\d{2}(?::\d{2})?Z?)?\b",
    re.IGNORECASE,
)

# One reduced feed row, exactly as `wiki_delta.py`'s `write_semantic_chunks`
# emits it: "[NEW folder basename timestamp role] text" or the CONTEXT_ONLY
# variant. See wiki_delta.py::reduced_row / write_semantic_chunks.
LINE_RE = re.compile(r"^\[(NEW|CONTEXT_ONLY) (\S+) (\S+) (\S+) (\w+)\] (.*)$")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    return [sentence for sentence in _SENTENCE_SPLIT_RE.split(text.strip()) if sentence]


class Claim:
    """One unsourced operator-intent-claim sentence found in the feed."""

    def __init__(self, folder: str, basename: str, timestamp: str, role: str, sentence: str) -> None:
        self.folder = folder
        self.basename = basename
        self.timestamp = timestamp
        self.role = role
        self.sentence = sentence

    @property
    def dedup_key(self) -> str:
        """Stable within-run + cross-run dedup key: folder + normalized text.

        Board #1485: Ben's real incident copy-forwarded ONE claim into ~36
        wake prompts and ~15 subagent briefs across a week -- without this,
        every literal occurrence would otherwise reach the board emitter as
        a separate call. Cross-run stability (no date/session baked in)
        lets `emit_kanban_item.sh`'s own task+title dedup collapse a claim
        that keeps recurring night after night into the SAME open card.
        """
        normalized = re.sub(r"[^a-z0-9]+", "-", self.sentence.lower()).strip("-")
        return f"{self.folder}:{normalized[:120]}"


def find_claims_in_text(
    folder: str, basename: str, timestamp: str, role: str, text: str
) -> list[Claim]:
    """Return unsourced-claim sentences from one reduced transcript row.

    A claim is UNSOURCED when no msg\\d+/tg\\d+/message_id/dated citation
    appears in the same sentence or an immediately adjacent one. Reduced
    feed rows are already single short turns (<=500 chars, see
    `wiki_delta.py::reduced_row`), so "nearby" is scoped to sentence-level
    proximity within one row -- not the whole multi-KB feed file.
    """
    claims: list[Claim] = []
    sentences = _split_sentences(text)
    for index, sentence in enumerate(sentences):
        if not CLAIM_RE.search(sentence):
            continue
        window = " ".join(sentences[max(0, index - 1) : index + 2])
        if CITATION_RE.search(window):
            continue
        claims.append(Claim(folder, basename, timestamp, role, sentence.strip()))
    return claims


def scan_feed(feed_text: str, *, include_context_only: bool = False) -> list[Claim]:
    """Scan an already-reduced wiki-compile feed for unsourced claims.

    Reads ONLY the plan's existing reduced feed text passed in by the
    caller -- never opens a file, never re-scans raw transcripts (board
    #1485 constraint). Only `[NEW ...]` rows originate a finding by
    default, matching the exhaustiveness contract every other piggyback
    pass in SKILL.md follows (CONTEXT_ONLY rows are backdrop, not signal).
    """
    claims: list[Claim] = []
    for line in feed_text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        tag, folder, basename, timestamp, role, text = match.groups()
        if tag == "CONTEXT_ONLY" and not include_context_only:
            continue
        claims.extend(find_claims_in_text(folder, basename, timestamp, role, text))
    return claims


# Wiki folder -> board dept/owner slug, reusing the SAME enum every other
# extractor in SKILL.md already uses for its DEPT field ("rnd|ben|maya|tony|
# content|security|accountant|morty|claudette"). tonio_extrnd and
# ellie_assistant have no dedicated board dept label (Tonio/Ellie are Mac-only
# assistants, not board-tracked depts) -- they route to "rnd" like the
# existing extractors' own DEPT judgment would for those folders.
FOLDER_TO_DEPT = {
    "tony_ceo": "tony",
    "tonio_extrnd": "rnd",
    "maya_sales": "maya",
    "claudette": "claudette",
    "morty": "morty",
    "rick_rnd": "rnd",
    "ben_fund": "ben",
    "miranda_socials": "content",
    "ellie_assistant": "rnd",
    "geraldine_accounting": "accountant",
}


class Candidate:
    """One deduped citation-lint finding, ready for `emit_kanban_item.sh`."""

    def __init__(
        self, dedup_key: str, dept: str, folder: str, title: str, body: str, occurrences: int
    ) -> None:
        self.dedup_key = dedup_key
        self.dept = dept
        self.folder = folder
        self.title = title
        self.body = body
        self.occurrences = occurrences


def _excerpt(sentence: str, limit: int = 70) -> str:
    sentence = " ".join(sentence.split())
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "…"


def dedupe_candidates(claims: Iterable[Claim]) -> list[Candidate]:
    """Collapse repeats of the SAME claim into ONE emit candidate."""
    groups: dict[str, list[Claim]] = {}
    for claim in claims:
        groups.setdefault(claim.dedup_key, []).append(claim)

    candidates: list[Candidate] = []
    for key, group in groups.items():
        first = group[0]
        dept = FOLDER_TO_DEPT.get(first.folder, "rnd")
        excerpt = _excerpt(first.sentence)
        title = f"citation-lint: unsourced claim in {first.folder} — \"{excerpt}\""
        evidence_lines = "\n".join(
            f'- "{claim.sentence}" ({claim.folder}/{claim.basename}, '
            f"{claim.timestamp}, {claim.role})"
            for claim in group[:5]
        )
        more = ""
        if len(group) > 5:
            more = f"\n...and {len(group) - 5} more occurrence(s) this run."
        body = (
            "Unsourced operator-intent claim (board #1483/#1485 citation lint, "
            "deterministic pass, wiki-compile STEP 4.7b).\n\n"
            "No msg\\d+/tg\\d+/message_id/dated citation was found near this "
            "claim in the reduced transcript feed:\n"
            f"{evidence_lines}{more}\n\n"
            "Confirm this is a real, correctly-sourced operator instruction "
            "(reply with the msg/tg id or date it traces to), or flag it as "
            "drift/hallucination so it can be corrected before it propagates "
            "further into wake prompts / subagent briefs / HANDOFF.md "
            "(see #1483's Ben incident: one unsourced claim like this spread "
            "into ~36 wake prompts and silently dropped a mission deliverable "
            "for 12 days)."
        )
        candidates.append(
            Candidate(
                dedup_key=key,
                dept=dept,
                folder=first.folder,
                title=title,
                body=body,
                occurrences=len(group),
            )
        )
    candidates.sort(key=lambda candidate: candidate.dedup_key)
    return candidates


def scan_and_dedupe(
    feed_text: str, *, include_context_only: bool = False
) -> list[Candidate]:
    return dedupe_candidates(scan_feed(feed_text, include_context_only=include_context_only))


def _atomic_write(path: Path, payload: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feed",
        type=Path,
        required=True,
        help="reduced wiki-compile feed file (a plan's aggregate_feed or "
        "weekly_aggregate_feed) -- never a raw transcript",
    )
    parser.add_argument("--output", type=Path, help="atomically write JSON here")
    parser.add_argument(
        "--include-context-only",
        action="store_true",
        help="also scan CONTEXT_ONLY rows (default: NEW rows only)",
    )
    args = parser.parse_args()

    if not args.feed.is_file():
        parser.error(f"feed file not found: {args.feed}")

    feed_text = args.feed.read_text(encoding="utf-8", errors="replace")
    candidates = scan_and_dedupe(feed_text, include_context_only=args.include_context_only)
    payload = {
        "schema_version": 1,
        "feed": str(args.feed),
        "candidates": [
            {
                "dedup_key": candidate.dedup_key,
                "dept": candidate.dept,
                "folder": candidate.folder,
                "title": candidate.title,
                "body": candidate.body,
                "occurrences": candidate.occurrences,
            }
            for candidate in candidates
        ],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        _atomic_write(args.output, text)
    else:
        print(text, end="")
    print(f"wiki_citation_lint: candidates={len(candidates)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
