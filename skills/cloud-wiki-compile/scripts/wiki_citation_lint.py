#!/usr/bin/env python3
"""Deterministic citation lint for unsourced operator-intent SELF-NOTES.

Board #1483/#1485: free-text self-notes (CronCreate wake prompts, subagent
briefs, HANDOFF.md, compaction summaries) have no enforced citation
requirement for operator-intent claims. One unsourced "operator flagged
spend -- be cost-conscious" (Ben, 2026-09-11) was written into a SUBAGENT
transcript, then got copied forward into ~36 wake prompts and ~15 subagent
briefs before anyone caught it, and silently narrowed a mission deliverable
for 12 days. The existing STEP 4.8 intent-drift extractor (an agentic,
weekly-only Task subagent) already proved the fleet can catch this kind of
drift (board #1350/#1354) -- this module generalizes the SAME signal into
something deterministic, cheap, and testable enough to run on EVERY compile.

## v1 -> v2 (board #1485 review round 2)

A first version flagged ANY sentence matching the claim pattern anywhere in
the feed. Dry-run against a real production feed (9/368 backlog chunks,
`joris-cx33:.../runs/plans/5005047de6e944e88b7c4fce23aa6941/all-folders.txt`)
produced 18 candidates -- ALL 18 false positives: an assistant narrating a
request the operator had JUST made in the SAME live conversation ("Jade
wants Olivier's email", "Joris wants the findings carded"). Extrapolated
across the full backlog that is hundreds of needless needs:human cards --
worse than the problem.

The real #1483 risk is NOT live-chat narration -- it is a claim carried into
a SELF-NOTE that OUTLIVES the conversation (a CronCreate prompt argument, an
Agent/Task subagent brief, a compaction/summary entry, HANDOFF.md), with no
grounding left once it is re-read cold. v2 narrows scope with two changes,
both built from data the reducer ALREADY captures -- no reducer change, no
widened access:

1. SCOPE: only role="assistant" rows can originate a claim (an operator's own
   words are definitionally sourced -- if Joris types "Jade wants Olivier's
   email" that message not an unsourced assertion, it IS the source). And a
   SUBAGENT transcript (`agent-<hex>.jsonl`, confirmed live naming on
   joris-cx33: `.../subagents/agent-a967d11ddc72bea2e.jsonl`) is the concrete,
   observed self-note surface (Ben's real incident) -- its own "user" row is
   a synthetic Task-tool prompt/brief, never a real inbound message, so it
   can never itself satisfy the SOURCE CHECK below.

   `CronCreate`/`Agent`/`Task` tool-call ARGUMENTS and HANDOFF.md file
   content are NOT literally visible here: `wiki_delta.py::reduced_row` only
   keeps `type=="text"` content blocks from user/assistant messages and
   drops every `tool_use` block outright, and HANDOFF.md is never read by
   this compile (board #1120 uid isolation -- reading a dept's HANDOFF.md
   directly would widen this compile's access, which #1485 explicitly rules
   out). In practice the claim that ENDS UP in a wake prompt or HANDOFF.md is
   almost always first drafted as assistant narration in the SAME mined
   session (a model narrates before or while it calls a tool or writes a
   file), so scanning assistant text is a reasonable, sufficient proxy for
   catching it at the source rather than at every later copy. A follow-up
   that teaches the reducer to ALSO keep `CronCreate`/`Agent`/`Task` tool
   inputs (tagged distinctly, e.g. `[NEW ... tool_use:CronCreate] ...`) would
   give literal coverage of the tool-argument text itself -- proposed, not
   implemented here: `reduced_row()` is shared by every other extractor (4,
   4.6-4.9), so widening what it emits is a separate, more carefully
   reviewed change, not a one-line addition to bundle into this card.

2. SOURCE CHECK: a claim is "sourced" if (a) a msg/tg/message_id/dated
   citation appears in the same or an adjacent sentence (unchanged from v1),
   OR (b) the SAME session (folder + basename, excluding subagent
   transcripts per (1)) has an inbound user-role row within the preceding
   24h -- i.e. the claim was made DURING a live conversation. Being inside a
   live back-and-forth counts as sourced even with no literal msg/tg id,
   because the operator's own adjacent turn in the SAME transcript already
   grounds it.

3. VOLUME: findings collapse into AT MOST ONE digest card per run (never one
   card per claim), capped at `--digest-cap` (default 10) listed findings
   plus a count of the rest, and deduplicated against a persistent
   `--seen-store` by claim hash so a claim already surfaced in a previous
   night's digest never resurfaces.

This is a REGEX + role/session-metadata pass, not a reading-judgment pass --
same injection-safety property `wiki_intent_audit.py` has (fixed-pattern
matching can't be argued with the way a reading model can), and unit-testable
in isolation. It deliberately does not decide whether a flagged sentence is
TRUE operator intent or a hallucination -- a human (via the emitted
needs:human digest) makes the semantic call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
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

# A Task/Agent subagent's OWN transcript file, exactly as this fleet names it
# (confirmed live: /home/claude/.claude/projects/_vps-tony/-srv-agents-tony/
# <parent-session-uuid>/subagents/agent-a967d11ddc72bea2e.jsonl -- `basename`
# is `path.name`, board #1485 review). Its first "user" row is the synthetic
# Task-tool prompt/brief the orchestrator wrote, never a real inbound
# message -- the concrete, PROVEN self-note surface from Ben's incident.
SUBAGENT_BASENAME_RE = re.compile(r"^agent-[0-9a-f]+\.jsonl$", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

SOURCE_WINDOW = timedelta(hours=24)
# Small forward grace: reduced-row timestamps are truncated/ordered by
# capture, not guaranteed strictly monotonic across a rapid exchange.
SOURCE_FORWARD_GRACE = timedelta(minutes=5)


def _split_sentences(text: str) -> list[str]:
    return [sentence for sentence in _SENTENCE_SPLIT_RE.split(text.strip()) if sentence]


def _parse_ts(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


def is_subagent_basename(basename: str) -> bool:
    return bool(SUBAGENT_BASENAME_RE.match(basename))


class Row:
    """One parsed reduced-feed line."""

    __slots__ = ("tag", "folder", "basename", "timestamp", "role", "text")

    def __init__(self, tag: str, folder: str, basename: str, timestamp: str, role: str, text: str) -> None:
        self.tag = tag
        self.folder = folder
        self.basename = basename
        self.timestamp = timestamp
        self.role = role
        self.text = text


def parse_feed(feed_text: str) -> list[Row]:
    """Parse every well-formed reduced-feed line. Malformed lines are skipped."""
    rows: list[Row] = []
    for line in feed_text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        rows.append(Row(*match.groups()))
    return rows


def build_inbound_index(rows: Iterable[Row]) -> dict[tuple[str, str], list[datetime]]:
    """Timestamps of genuine inbound-operator rows, keyed by (folder, basename).

    A row counts as "inbound" when its role is "user" AND its own basename is
    NOT a subagent transcript (see `SUBAGENT_BASENAME_RE` docstring) -- a
    subagent's "user" row is a synthetic brief the orchestrator wrote, not a
    real message from Joris/Jade, so it can never ground a claim. Both `[NEW
    ...]` and `[CONTEXT_ONLY ...]` rows contribute: context rows are exactly
    the SAME-session surrounding conversation this check needs (matching the
    existing convention that context resolves references/corroboration).
    """
    index: dict[tuple[str, str], list[datetime]] = {}
    for row in rows:
        if row.role != "user" or is_subagent_basename(row.basename):
            continue
        timestamp = _parse_ts(row.timestamp)
        if timestamp is None:
            continue
        index.setdefault((row.folder, row.basename), []).append(timestamp)
    for key in index:
        index[key].sort()
    return index


def is_conversationally_sourced(
    folder: str, basename: str, timestamp: str, inbound_index: dict[tuple[str, str], list[datetime]]
) -> bool:
    """True if the SAME session has a genuine inbound row within the window.

    "Being within a live conversation counts as sourced" (board #1485 review):
    a claim written moments after a real operator turn in the SAME top-level
    session needs no separate msg/tg id -- the adjacent turn IS the source.
    A subagent transcript's own basename never appears in `inbound_index`
    (see `build_inbound_index`), so this always returns False for a claim
    inside one, regardless of the window.
    """
    claim_ts = _parse_ts(timestamp)
    if claim_ts is None:
        return False
    for inbound_ts in inbound_index.get((folder, basename), ()):
        delta = claim_ts - inbound_ts
        if -SOURCE_FORWARD_GRACE <= delta <= SOURCE_WINDOW:
            return True
    return False


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
        every literal occurrence would otherwise be counted as a separate
        finding. Cross-run stability (no date/session baked in) also lets
        the persistent seen-store (see `Digest`) collapse a claim that keeps
        recurring night after night.
        """
        normalized = re.sub(r"[^a-z0-9]+", "-", self.sentence.lower()).strip("-")
        return f"{self.folder}:{normalized[:120]}"

    @property
    def claim_hash(self) -> str:
        return hashlib.sha256(self.dedup_key.encode("utf-8")).hexdigest()[:24]


def find_claims_in_text(
    folder: str, basename: str, timestamp: str, role: str, text: str
) -> list[Claim]:
    """Return claim sentences (citation check only) from one reduced row.

    A claim has NO literal citation when no msg\\d+/tg\\d+/message_id/dated
    citation appears in the same sentence or an immediately adjacent one.
    This function does NOT apply the session-level SOURCE CHECK (role
    restriction, live-conversation grounding, subagent exclusion) -- that
    happens in `scan_feed`, which has the whole-feed view this needs.
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
    """Scan an already-reduced wiki-compile feed for unsourced self-note claims.

    Reads ONLY the plan's existing reduced feed text passed in by the
    caller -- never opens a file, never re-scans raw transcripts (board
    #1485 constraint). A claim survives only if ALL of:
    - its row's role is eligible to originate a self-note (see below);
    - it is in a `[NEW ...]` row by default (matches the exhaustiveness
      contract every other piggyback pass in SKILL.md follows);
    - it has no citation in the same/adjacent sentence (`find_claims_in_text`);
    - it is NOT conversationally sourced (`is_conversationally_sourced`) --
      no genuine inbound row in the SAME top-level session within the
      preceding 24h.

    Role eligibility (board #1485 review, verified against Ben's REAL
    subagent transcript on joris-cx33 -- `.../subagents/
    agent-a910fd47bf3adee99.jsonl`, the exact file the #1483 audit cites):
    the historical "operator flagged spend" claim is NOT in an assistant
    row -- it is the subagent's very FIRST `role="user"` row, because that
    row is the Task-tool PROMPT the *orchestrating* agent wrote, not
    anything Joris/Jade typed. So "user rows are the operator's own words"
    is only true in a TOP-LEVEL session; in a subagent transcript
    (`is_subagent_basename`) BOTH roles are self-authored (the orchestrator
    wrote the "user" brief, the subagent wrote the "assistant" replies) and
    both must be scanned. In a top-level session only `role="assistant"`
    is scanned -- a `role="user"` row there is definitionally sourced (it
    IS the operator speaking).
    """
    rows = parse_feed(feed_text)
    inbound_index = build_inbound_index(rows)

    claims: list[Claim] = []
    for row in rows:
        if row.tag == "CONTEXT_ONLY" and not include_context_only:
            continue
        subagent = is_subagent_basename(row.basename)
        if not subagent and row.role != "assistant":
            continue
        for claim in find_claims_in_text(row.folder, row.basename, row.timestamp, row.role, row.text):
            if is_conversationally_sourced(row.folder, row.basename, row.timestamp, inbound_index):
                continue
            claims.append(claim)
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
    """One deduped citation-lint finding (within-run collapse of repeats)."""

    def __init__(
        self, dedup_key: str, claim_hash: str, dept: str, folder: str, excerpt: str,
        evidence: list[Claim], occurrences: int,
    ) -> None:
        self.dedup_key = dedup_key
        self.claim_hash = claim_hash
        self.dept = dept
        self.folder = folder
        self.excerpt = excerpt
        self.evidence = evidence
        self.occurrences = occurrences


def _excerpt(sentence: str, limit: int = 90) -> str:
    sentence = " ".join(sentence.split())
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "…"


def dedupe_candidates(claims: Iterable[Claim]) -> list[Candidate]:
    """Collapse repeats of the SAME claim (within this run) into ONE candidate."""
    groups: dict[str, list[Claim]] = {}
    for claim in claims:
        groups.setdefault(claim.dedup_key, []).append(claim)

    candidates: list[Candidate] = []
    for key, group in groups.items():
        first = group[0]
        candidates.append(
            Candidate(
                dedup_key=key,
                claim_hash=first.claim_hash,
                dept=FOLDER_TO_DEPT.get(first.folder, "rnd"),
                folder=first.folder,
                excerpt=_excerpt(first.sentence),
                evidence=group,
                occurrences=len(group),
            )
        )
    candidates.sort(key=lambda candidate: candidate.dedup_key)
    return candidates


class Digest:
    """The at-most-one-per-run board card: capped listing + rollover state.

    Board #1485 review point 3: "emit at most ONE digest card per night ...
    capped at around 10 with a count of the rest ... deduplicated against
    previous nights by claim hash. Never one card per claim." Only the
    candidates actually SHOWN (up to `cap`) are marked seen -- an overflow
    candidate stays eligible to be shown (and then marked seen) on a later
    night once the backlog is below the cap, so a real finding is never
    silently dropped just for arriving on a busy night.
    """

    def __init__(self, title: str, body: str, dept: str, shown: list[Candidate], overflow: int, new_seen_hashes: list[str]) -> None:
        self.title = title
        self.body = body
        self.dept = dept
        self.shown = shown
        self.overflow = overflow
        self.new_seen_hashes = new_seen_hashes


def build_digest(
    candidates: list[Candidate], seen_hashes: set[str], *, cap: int = 10, today: str = "",
) -> Digest | None:
    """Build the single digest card, or None if nothing new survived.

    `seen_hashes` is the persistent cross-night ledger (claim_hash values
    already shown in a prior digest). Candidates whose hash is already in it
    are dropped entirely (already surfaced once, no need to repeat).
    """
    new_candidates = [c for c in candidates if c.claim_hash not in seen_hashes]
    if not new_candidates:
        return None

    shown = new_candidates[:cap]
    overflow = len(new_candidates) - len(shown)

    lines = [
        "Unsourced operator-intent self-notes (board #1483/#1485 citation "
        "lint, deterministic pass, wiki-compile STEP 4.7b).",
        "",
        "Each line is a sentence shaped like an operator-attribution claim "
        "with NO msg/tg/message_id/dated citation nearby AND no live "
        "conversation turn grounding it in the same session (subagent "
        "briefs never count as grounding -- see #1483's Ben incident: one "
        "such claim spread into ~36 wake prompts and silently dropped a "
        "mission deliverable for 12 days).",
        "",
    ]
    for candidate in shown:
        sample = candidate.evidence[0]
        lines.append(
            f'- [{candidate.dept}/{candidate.folder}] "{candidate.excerpt}" '
            f"({sample.basename}, {sample.timestamp}, x{candidate.occurrences} "
            f"this run) -- confirm the source (msg/tg id or date) or flag as "
            f"drift/hallucination."
        )
    if overflow:
        lines.append(f"\n...and {overflow} more unsourced claim(s) this run (not listed, will "
                      f"surface in a later digest as the backlog clears below the cap).")

    title = f"citation-lint digest: {today} — {len(new_candidates)} unsourced operator-intent claim(s)"
    body = "\n".join(lines)
    return Digest(
        title=title,
        body=body,
        dept="rnd",
        shown=shown,
        overflow=overflow,
        new_seen_hashes=[c.claim_hash for c in shown],
    )


def load_seen_hashes(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return set()
    if not isinstance(data, list):
        return set()
    return {item for item in data if isinstance(item, str)}


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


def save_seen_hashes(path: Path, seen_hashes: set[str]) -> None:
    payload = json.dumps(sorted(seen_hashes), indent=2) + "\n"
    _atomic_write(path, payload)


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
        "--seen-store",
        type=Path,
        help="persistent JSON file of claim_hash values already surfaced in "
        "a previous digest (cross-night dedup); updated in place after a "
        "successful run",
    )
    parser.add_argument("--digest-cap", type=int, default=10, help="max findings listed per digest (default 10)")
    parser.add_argument("--today", default="", help="date string for the digest title (default: empty)")
    parser.add_argument(
        "--include-context-only",
        action="store_true",
        help="also scan CONTEXT_ONLY rows (default: NEW rows only)",
    )
    args = parser.parse_args()

    if not args.feed.is_file():
        parser.error(f"feed file not found: {args.feed}")

    feed_text = args.feed.read_text(encoding="utf-8", errors="replace")
    claims = scan_feed(feed_text, include_context_only=args.include_context_only)
    candidates = dedupe_candidates(claims)
    seen_hashes = load_seen_hashes(args.seen_store)
    digest = build_digest(candidates, seen_hashes, cap=args.digest_cap, today=args.today)

    payload: dict[str, object] = {
        "schema_version": 2,
        "feed": str(args.feed),
        "total_candidates": len(candidates),
        "digest": None,
    }
    if digest is not None:
        payload["digest"] = {
            "title": digest.title,
            "body": digest.body,
            "dept": digest.dept,
            "shown_count": len(digest.shown),
            "overflow_count": digest.overflow,
        }

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        _atomic_write(args.output, text)
    else:
        print(text, end="")

    if digest is not None and args.seen_store is not None:
        save_seen_hashes(args.seen_store, seen_hashes | set(digest.new_seen_hashes))

    shown = len(digest.shown) if digest else 0
    print(
        f"wiki_citation_lint: total_candidates={len(candidates)} digest_shown={shown} "
        f"digest_overflow={digest.overflow if digest else 0}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
