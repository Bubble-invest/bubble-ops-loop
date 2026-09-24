"""Behavioral tests for the deterministic citation lint (board #1483/#1485).

STEP 4.7b in cloud-wiki-compile/SKILL.md extends the intent-drift family with
a NIGHTLY, deterministic (regex + session-metadata, not agentic) pass over
the SAME reduced feed STEP 4/4.7 already piggyback, flagging SELF-NOTE
sentences shaped like unsourced operator-intent claims: `(operator|Joris|
Jade) (said|flagged|wants|asked|told|decided)` (or the French equivalents)
with no nearby msg/tg/message_id/dated citation AND no live-conversation
grounding in the same session.

v2 (board #1485 review round 2): a v1 dry-run against a REAL production feed
(9/368 backlog chunks) produced 18 candidates, all false positives --
assistant narration of a request the operator had JUST made in the same live
conversation. v2 adds: (a) only `role="assistant"` rows can originate a
claim, (b) a SOURCE CHECK that clears a claim grounded by a genuine inbound
row in the SAME session within 24h, with subagent transcripts
(`agent-<hex>.jsonl`) structurally excluded from ever supplying that
grounding (their own "user" row is a synthetic Task-tool brief, not a real
operator message -- board #1485's proven Ben incident), and (c) an
at-most-one-digest-per-run cap with a persistent cross-night seen-hash ledger.

These tests exercise the shipped module directly (import by file path, like
`test_wiki_intent_audit.py` and `test_wiki_delta.py` already do for their
scripts), not a reimplementation.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/cloud-wiki-compile/scripts/wiki_citation_lint.py"
SPEC = importlib.util.spec_from_file_location("wiki_citation_lint", SCRIPT)
assert SPEC and SPEC.loader
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)


def feed_row(folder: str, basename: str, timestamp: str, role: str, text: str, *, tag: str = "NEW") -> str:
    return f"[{tag} {folder} {basename} {timestamp} {role}] {text}\n"


SUBAGENT_BASENAME = "agent-a967d11ddc72bea2e.jsonl"  # real live naming, board #1485 review
SESSION_BASENAME = "b1d644ea-77c0-4e5f-af4d-a14bdef6d257.jsonl"  # real top-level session naming


# ---------------------------------------------------------------------------
# find_claims_in_text: citation-in-sentence check only (unchanged from v1).
# ---------------------------------------------------------------------------


def test_bens_uncited_cost_conscious_claim_has_no_literal_citation() -> None:
    # Verbatim quote from the #1483 audit findings table.
    text = "operator flagged spend — be cost-conscious."
    claims = lint.find_claims_in_text("ben_fund", SUBAGENT_BASENAME, "2026-09-11T12:34:10", "assistant", text)
    assert len(claims) == 1
    assert "operator flagged spend" in claims[0].sentence


def test_claim_with_msg_id_citation_is_not_flagged() -> None:
    text = "Joris asked (msg3096) to scope card #1484 down to the templated wake prompt."
    claims = lint.find_claims_in_text("rick_rnd", SESSION_BASENAME, "2026-09-24T09:00:00", "assistant", text)
    assert claims == []


def test_claim_with_tg_id_citation_is_not_flagged() -> None:
    text = "the operator wants the daily letter kept (tg2234), never lean-scoped."
    claims = lint.find_claims_in_text("ben_fund", SUBAGENT_BASENAME, "2026-09-23T22:18:00", "assistant", text)
    assert claims == []


def test_claim_with_dated_citation_is_not_flagged() -> None:
    text = "Joris told the dept on 2026-09-14T08:33:00Z to prioritize the drawdown review."
    claims = lint.find_claims_in_text("ben_fund", SUBAGENT_BASENAME, "2026-09-14T09:00:00", "assistant", text)
    assert claims == []


def test_french_equivalent_uncited_claim_is_flagged() -> None:
    text = "l'opérateur a décidé de couper le budget marketing."
    claims = lint.find_claims_in_text("maya_sales", SUBAGENT_BASENAME, "2026-09-20T10:00:00", "assistant", text)
    assert len(claims) == 1


def test_french_equivalent_cited_claim_is_not_flagged() -> None:
    text = "Joris a demandé (tg2621) de ralentir les envois cette semaine."
    claims = lint.find_claims_in_text("maya_sales", SUBAGENT_BASENAME, "2026-09-20T10:00:00", "assistant", text)
    assert claims == []


def test_routine_narration_without_claim_pattern_is_not_flagged() -> None:
    text = "I read the file and checked the current status before proceeding."
    claims = lint.find_claims_in_text("rick_rnd", SESSION_BASENAME, "2026-09-20T10:00:00", "assistant", text)
    assert claims == []


# ---------------------------------------------------------------------------
# Subagent-basename detection (the structural self-note-surface signal).
# ---------------------------------------------------------------------------


def test_subagent_basename_pattern_matches_real_live_naming() -> None:
    assert lint.is_subagent_basename("agent-a967d11ddc72bea2e.jsonl")
    assert lint.is_subagent_basename("agent-ac4a76ab24283f3c8.jsonl")


def test_subagent_basename_pattern_does_not_match_top_level_session() -> None:
    assert not lint.is_subagent_basename("b1d644ea-77c0-4e5f-af4d-a14bdef6d257.jsonl")
    assert not lint.is_subagent_basename("f7b6c6c3-3d13-45fc-8f55-c8929be7b881.jsonl")


# ---------------------------------------------------------------------------
# SOURCE CHECK: the actual bug the review round caught.
# ---------------------------------------------------------------------------


def test_live_narration_with_prior_inbound_is_sourced_and_not_flagged() -> None:
    """The exact v1 false-positive pattern: Joris asks something, the
    assistant immediately narrates it back in the SAME top-level session --
    that live conversation grounds the claim even with no msg/tg id."""
    feed = (
        feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:00", "user",
                  '<channel source="plugin:telegram:telegram" chat_id="7470271615"> Get Olivier\'s email from the CRM.')
        + feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:05", "assistant",
                    "Jade wants Olivier's email.")
    )
    assert lint.scan_feed(feed) == []


def test_claim_with_no_inbound_anywhere_in_session_is_flagged() -> None:
    feed = feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:05", "assistant",
                     "Jade wants Olivier's email.")
    claims = lint.scan_feed(feed)
    assert len(claims) == 1


def test_inbound_more_than_24h_before_does_not_source_the_claim() -> None:
    feed = (
        feed_row("maya_sales", SESSION_BASENAME, "2026-09-19T09:00:00", "user", "Go check the CRM later.")
        + feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:05", "assistant",
                    "Jade wants Olivier's email.")
    )
    claims = lint.scan_feed(feed)
    assert len(claims) == 1


def test_inbound_in_a_different_session_does_not_source_the_claim() -> None:
    feed = (
        feed_row("maya_sales", "other-session-uuid.jsonl", "2026-09-20T09:59:00", "user", "Get Olivier's email.")
        + feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:05", "assistant",
                    "Jade wants Olivier's email.")
    )
    claims = lint.scan_feed(feed)
    assert len(claims) == 1


def test_ben_incident_subagent_brief_with_no_inbound_is_flagged() -> None:
    """Board #1483's actual proven incident, verified live against the REAL
    file the #1483 audit cites
    (joris-cx33:.../subagents/agent-a910fd47bf3adee99.jsonl): the claim is
    the subagent's very FIRST `role="user"` row -- the Task-tool PROMPT the
    *orchestrating* Ben session wrote, not anything Joris said. A subagent's
    "user" row is a self-authored brief, never a real operator message, so
    it can never ground itself or anything else -- this must survive the
    SOURCE CHECK and be flagged even though its own role is "user"."""
    feed = feed_row(
        "ben_fund", SUBAGENT_BASENAME, "2026-09-11T12:34:10", "user",
        "Be COST-CONSCIOUS this run (the operator flagged spend): keep it tight.",
    )
    claims = lint.scan_feed(feed)
    assert len(claims) == 1
    assert "operator flagged spend" in claims[0].sentence


def test_subagent_assistant_narration_is_also_scanned() -> None:
    """A subagent's OWN assistant replies are self-note content too (there
    is no live operator on the other end of a subagent transcript)."""
    feed = feed_row("ben_fund", SUBAGENT_BASENAME, "2026-09-11T12:34:10", "assistant",
                     "operator flagged spend — be cost-conscious.")
    claims = lint.scan_feed(feed)
    assert len(claims) == 1


def test_user_role_claim_text_is_never_flagged() -> None:
    """An operator's OWN words are definitionally sourced -- they ARE the
    source, not an assertion about it."""
    feed = feed_row("rick_rnd", SESSION_BASENAME, "2026-09-20T10:00:00", "user",
                     "Jade wants Olivier's email, can you get it?")
    assert lint.scan_feed(feed) == []


def test_scan_feed_only_reads_new_rows_by_default() -> None:
    feed = feed_row(
        "ben_fund", SUBAGENT_BASENAME, "2026-09-11T12:34:10", "assistant",
        "operator flagged spend — be cost-conscious.", tag="CONTEXT_ONLY",
    )
    assert lint.scan_feed(feed) == []
    assert len(lint.scan_feed(feed, include_context_only=True)) == 1


def test_context_only_inbound_row_still_grounds_a_new_claim() -> None:
    """Context rows ARE real conversation history (same convention as every
    other piggyback pass) -- they should still ground a NEW claim even
    though they are not findings themselves."""
    feed = (
        feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:00", "user",
                  "Get Olivier's email.", tag="CONTEXT_ONLY")
        + feed_row("maya_sales", SESSION_BASENAME, "2026-09-20T10:00:05", "assistant",
                    "Jade wants Olivier's email.")
    )
    assert lint.scan_feed(feed) == []


def test_scan_feed_ignores_malformed_lines() -> None:
    feed = "not a reduced row at all\n" + feed_row(
        "ben_fund", SUBAGENT_BASENAME, "2026-09-11T12:34:10", "assistant",
        "operator flagged spend — be cost-conscious.",
    )
    claims = lint.scan_feed(feed)
    assert len(claims) == 1


# ---------------------------------------------------------------------------
# Dedup: Ben's real incident (~36 wake prompts + ~15 subagent briefs) must
# collapse to ONE candidate, not one emit call per occurrence.
# ---------------------------------------------------------------------------


def test_repeated_verbatim_claim_collapses_to_one_candidate() -> None:
    claim_text = "operator flagged spend — be cost-conscious."
    feed = "".join(
        feed_row("ben_fund", f"agent-{i:012x}.jsonl", f"2026-09-{11 + i:02d}T08:00:00", "assistant", claim_text)
        for i in range(6)
    )
    candidates = lint.dedupe_candidates(lint.scan_feed(feed))
    assert len(candidates) == 1
    assert candidates[0].occurrences == 6
    assert candidates[0].dept == "ben"
    assert candidates[0].folder == "ben_fund"


def test_distinct_claims_in_same_folder_produce_distinct_candidates() -> None:
    feed = (
        feed_row("ben_fund", SUBAGENT_BASENAME, "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
        + feed_row("ben_fund", "agent-b00000000000.jsonl", "2026-09-12T08:00:00", "assistant", "Joris wants the letter dropped entirely.")
    )
    candidates = lint.dedupe_candidates(lint.scan_feed(feed))
    assert len(candidates) == 2
    assert {c.occurrences for c in candidates} == {1, 1}


def test_dept_mapping_covers_every_canonical_folder() -> None:
    for folder in [
        "tony_ceo", "tonio_extrnd", "maya_sales", "claudette", "morty",
        "rick_rnd", "ben_fund", "miranda_socials", "ellie_assistant",
        "geraldine_accounting",
    ]:
        assert lint.FOLDER_TO_DEPT[folder] in {
            "rnd", "ben", "maya", "tony", "content", "security", "accountant",
            "morty", "claudette",
        }


def test_unknown_folder_falls_back_to_rnd() -> None:
    feed = feed_row("some_future_dept", SUBAGENT_BASENAME, "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
    [candidate] = lint.dedupe_candidates(lint.scan_feed(feed))
    assert candidate.dept == "rnd"


# ---------------------------------------------------------------------------
# Digest: at most one card per run, capped, cross-night dedup by claim hash.
# ---------------------------------------------------------------------------


def _candidates_for(n: int) -> list["lint.Candidate"]:
    feed = "".join(
        feed_row("ben_fund", f"agent-{i:012x}.jsonl", "2026-09-11T08:00:00", "assistant",
                  f"operator flagged spend number {i} — be cost-conscious.")
        for i in range(n)
    )
    return lint.dedupe_candidates(lint.scan_feed(feed))


def test_digest_caps_shown_findings_and_reports_overflow() -> None:
    candidates = _candidates_for(15)
    digest = lint.build_digest(candidates, seen_hashes=set(), cap=10, today="2026-09-25")
    assert digest is not None
    assert len(digest.shown) == 10
    assert digest.overflow == 5
    assert "15 unsourced" in digest.title
    assert digest.body.count("- [") == 10
    assert "5 more" in digest.body


def test_digest_is_none_when_nothing_new() -> None:
    candidates = _candidates_for(3)
    seen = {c.claim_hash for c in candidates}
    assert lint.build_digest(candidates, seen_hashes=seen, cap=10, today="2026-09-25") is None


def test_digest_never_exceeds_one_card_regardless_of_candidate_count() -> None:
    candidates = _candidates_for(37)  # matches Ben's real ~36+ propagation count
    digest = lint.build_digest(candidates, seen_hashes=set(), cap=10, today="2026-09-25")
    assert digest is not None
    assert len(digest.shown) == 10
    assert digest.overflow == 27


def test_seen_hash_from_previous_night_excludes_that_claim() -> None:
    candidates = _candidates_for(5)
    already_seen = {candidates[0].claim_hash, candidates[2].claim_hash}
    digest = lint.build_digest(candidates, seen_hashes=already_seen, cap=10, today="2026-09-25")
    assert digest is not None
    assert len(digest.shown) == 3
    shown_hashes = {c.claim_hash for c in digest.shown}
    assert already_seen.isdisjoint(shown_hashes)


def test_digest_new_seen_hashes_only_cover_shown_not_overflow() -> None:
    candidates = _candidates_for(12)
    digest = lint.build_digest(candidates, seen_hashes=set(), cap=10, today="2026-09-25")
    assert digest is not None
    assert len(digest.new_seen_hashes) == 10  # overflow (2) stays eligible for a later night


# ---------------------------------------------------------------------------
# CLI: JSON output shape, --seen-store persistence, atomic --output write.
# ---------------------------------------------------------------------------


def test_cli_writes_digest_json(tmp_path: Path) -> None:
    feed_path = tmp_path / "aggregate_feed.txt"
    feed_path.write_text(
        feed_row("ben_fund", SUBAGENT_BASENAME, "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
        + feed_row("ben_fund", SUBAGENT_BASENAME, "2026-09-12T08:00:00", "assistant", "operator flagged spend — be cost-conscious."),
        encoding="utf-8",
    )
    output_path = tmp_path / "citation-lint.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path), "--output", str(output_path), "--today", "2026-09-25"],
        capture_output=True, text=True, check=True,
    )
    assert "digest_shown=1" in result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["total_candidates"] == 1
    assert payload["digest"]["shown_count"] == 1
    assert payload["digest"]["dept"] == "rnd"


def test_cli_seen_store_persists_across_two_runs(tmp_path: Path) -> None:
    feed_path = tmp_path / "aggregate_feed.txt"
    feed_path.write_text(
        feed_row("ben_fund", SUBAGENT_BASENAME, "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious."),
        encoding="utf-8",
    )
    seen_store = tmp_path / "seen.json"

    first = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path), "--seen-store", str(seen_store), "--today", "2026-09-25"],
        capture_output=True, text=True, check=True,
    )
    assert "digest_shown=1" in first.stderr
    assert seen_store.is_file()
    stored = json.loads(seen_store.read_text(encoding="utf-8"))
    assert len(stored) == 1

    # Same claim recurs the next night (e.g. a wake prompt copy-forward) --
    # it must NOT resurface once already seen.
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path), "--seen-store", str(seen_store), "--today", "2026-09-26"],
        capture_output=True, text=True, check=True,
    )
    assert "digest_shown=0" in second.stderr


def test_cli_missing_feed_errors_loudly(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(tmp_path / "missing.txt")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_cli_quiet_feed_emits_no_digest(tmp_path: Path) -> None:
    feed_path = tmp_path / "aggregate_feed.txt"
    feed_path.write_text(
        feed_row("rick_rnd", SESSION_BASENAME, "2026-09-24T08:00:00", "assistant", "I read the file and checked status."),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path)],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["digest"] is None
    assert "digest_shown=0" in result.stderr
