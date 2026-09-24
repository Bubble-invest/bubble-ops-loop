"""Behavioral tests for the deterministic citation lint (board #1483/#1485).

STEP 4.7b in cloud-wiki-compile/SKILL.md extends the intent-drift family with
a NIGHTLY, deterministic (regex, not agentic) pass over the SAME reduced feed
STEP 4/4.7 already piggyback, flagging sentences shaped like unsourced
operator-intent claims: `(operator|Joris|Jade) (said|flagged|wants|asked|
told|decided)` (or the French equivalents) with no nearby msg/tg/message_id/
dated citation. These tests exercise the shipped module directly (import by
file path, like `test_wiki_intent_audit.py` and `test_wiki_delta.py` already
do for their scripts), not a reimplementation.
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


# ---------------------------------------------------------------------------
# Claim detection: cited vs uncited, including Ben's real #1483 example.
# ---------------------------------------------------------------------------


def test_bens_uncited_cost_conscious_claim_is_flagged() -> None:
    # Verbatim quote from the #1483 audit findings table (board comment
    # IC_kwDOS_uT-M8AAAABWkYO7A): the actual real-world miss this feature
    # must catch.
    text = "operator flagged spend — be cost-conscious."
    claims = lint.find_claims_in_text("ben_fund", "session.jsonl", "2026-09-11T12:34:10", "assistant", text)
    assert len(claims) == 1
    assert "operator flagged spend" in claims[0].sentence


def test_claim_with_msg_id_citation_is_not_flagged() -> None:
    text = "Joris asked (msg3096) to scope card #1484 down to the templated wake prompt."
    claims = lint.find_claims_in_text("rick_rnd", "session.jsonl", "2026-09-24T09:00:00", "assistant", text)
    assert claims == []


def test_claim_with_tg_id_citation_is_not_flagged() -> None:
    text = "the operator wants the daily letter kept (tg2234), never lean-scoped."
    claims = lint.find_claims_in_text("ben_fund", "session.jsonl", "2026-09-23T22:18:00", "assistant", text)
    assert claims == []


def test_claim_with_dated_citation_is_not_flagged() -> None:
    text = "Joris told the dept on 2026-09-14T08:33:00Z to prioritize the drawdown review."
    claims = lint.find_claims_in_text("ben_fund", "session.jsonl", "2026-09-14T09:00:00", "assistant", text)
    assert claims == []


def test_claim_with_message_id_word_citation_is_not_flagged() -> None:
    text = "Joris said to hold off (message_id: 4821), confirmed in the same thread."
    claims = lint.find_claims_in_text("tony_ceo", "session.jsonl", "2026-09-20T10:00:00", "assistant", text)
    assert claims == []


def test_citation_in_adjacent_sentence_still_counts_as_nearby() -> None:
    text = "Context first. Joris flagged the budget. See msg9594 for the thread. Unrelated closing line."
    claims = lint.find_claims_in_text("rick_rnd", "session.jsonl", "2026-09-24T09:04:00", "assistant", text)
    assert claims == []


def test_french_equivalent_uncited_claim_is_flagged() -> None:
    text = "l'opérateur a décidé de couper le budget marketing."
    claims = lint.find_claims_in_text("maya_sales", "session.jsonl", "2026-09-20T10:00:00", "assistant", text)
    assert len(claims) == 1


def test_french_equivalent_cited_claim_is_not_flagged() -> None:
    text = "Joris a demandé (tg2621) de ralentir les envois cette semaine."
    claims = lint.find_claims_in_text("maya_sales", "session.jsonl", "2026-09-20T10:00:00", "assistant", text)
    assert claims == []


def test_routine_narration_without_claim_pattern_is_not_flagged() -> None:
    text = "I read the file and checked the current status before proceeding."
    claims = lint.find_claims_in_text("rick_rnd", "session.jsonl", "2026-09-20T10:00:00", "assistant", text)
    assert claims == []


def test_jade_as_subject_is_recognized() -> None:
    text = "Jade told the team to hold the Telegram send until review."
    claims = lint.find_claims_in_text("claudette", "session.jsonl", "2026-09-20T10:00:00", "user", text)
    assert len(claims) == 1


# ---------------------------------------------------------------------------
# Feed parsing: NEW vs CONTEXT_ONLY rows.
# ---------------------------------------------------------------------------


def test_scan_feed_only_reads_new_rows_by_default() -> None:
    feed = feed_row(
        "ben_fund", "a.jsonl", "2026-09-11T12:34:10", "assistant",
        "operator flagged spend — be cost-conscious.", tag="CONTEXT_ONLY",
    )
    assert lint.scan_feed(feed) == []
    assert len(lint.scan_feed(feed, include_context_only=True)) == 1


def test_scan_feed_ignores_malformed_lines() -> None:
    feed = "not a reduced row at all\n" + feed_row(
        "ben_fund", "a.jsonl", "2026-09-11T12:34:10", "assistant",
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
        feed_row("ben_fund", f"session-{i}.jsonl", f"2026-09-{11 + i:02d}T08:00:00", "assistant", claim_text)
        for i in range(6)
    )
    candidates = lint.scan_and_dedupe(feed)
    assert len(candidates) == 1
    assert candidates[0].occurrences == 6
    assert candidates[0].dept == "ben"
    assert candidates[0].folder == "ben_fund"


def test_distinct_claims_in_same_folder_produce_distinct_candidates() -> None:
    feed = (
        feed_row("ben_fund", "a.jsonl", "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
        + feed_row("ben_fund", "b.jsonl", "2026-09-12T08:00:00", "assistant", "Joris wants the letter dropped entirely.")
    )
    candidates = lint.scan_and_dedupe(feed)
    assert len(candidates) == 2
    assert {c.occurrences for c in candidates} == {1, 1}


def test_same_claim_text_in_different_folders_stays_distinct() -> None:
    text = "operator flagged spend — be cost-conscious."
    feed = (
        feed_row("ben_fund", "a.jsonl", "2026-09-11T08:00:00", "assistant", text)
        + feed_row("maya_sales", "b.jsonl", "2026-09-11T08:05:00", "assistant", text)
    )
    candidates = lint.scan_and_dedupe(feed)
    assert len(candidates) == 2
    assert {c.folder for c in candidates} == {"ben_fund", "maya_sales"}


def test_candidate_title_and_body_are_board_ready() -> None:
    feed = feed_row("ben_fund", "a.jsonl", "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
    [candidate] = lint.scan_and_dedupe(feed)
    assert candidate.title.startswith("citation-lint:")
    assert "ben_fund" in candidate.title
    assert "msg" in candidate.body  # instructs the human how to source it
    assert "#1483" in candidate.body


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
    feed = feed_row("some_future_dept", "a.jsonl", "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
    [candidate] = lint.scan_and_dedupe(feed)
    assert candidate.dept == "rnd"


# ---------------------------------------------------------------------------
# CLI: JSON output shape + atomic --output write.
# ---------------------------------------------------------------------------


def test_cli_writes_json_candidates(tmp_path: Path) -> None:
    feed_path = tmp_path / "aggregate_feed.txt"
    feed_path.write_text(
        feed_row("ben_fund", "a.jsonl", "2026-09-11T08:00:00", "assistant", "operator flagged spend — be cost-conscious.")
        + feed_row("ben_fund", "a.jsonl", "2026-09-12T08:00:00", "assistant", "operator flagged spend — be cost-conscious."),
        encoding="utf-8",
    )
    output_path = tmp_path / "citation-lint.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path), "--output", str(output_path)],
        capture_output=True, text=True, check=True,
    )
    assert "candidates=1" in result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["occurrences"] == 2


def test_cli_missing_feed_errors_loudly(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(tmp_path / "missing.txt")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_cli_quiet_feed_emits_zero_candidates(tmp_path: Path) -> None:
    feed_path = tmp_path / "aggregate_feed.txt"
    feed_path.write_text(
        feed_row("rick_rnd", "a.jsonl", "2026-09-24T08:00:00", "assistant", "I read the file and checked status."),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--feed", str(feed_path)],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["candidates"] == []
    assert "candidates=0" in result.stderr
