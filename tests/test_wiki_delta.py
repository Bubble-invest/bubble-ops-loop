from __future__ import annotations

import importlib.util
import json
import os
import pathlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/cloud-wiki-compile/scripts/wiki_delta.py"
SPEC = importlib.util.spec_from_file_location("wiki_delta", SCRIPT)
wiki_delta = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wiki_delta)


def row(text: str, role: str = "user") -> bytes:
    return (json.dumps({
        "type": role, "timestamp": "2026-09-13T12:00:00Z",
        "message": {"content": text},
    }) + "\n").encode()


def empty_scan():
    return {folder: [] for folder in wiki_delta.FOLDERS}


def args(tmp_path: pathlib.Path, **overrides):
    values = {
        "state": str(tmp_path / "state.json"),
        "plan": str(tmp_path / "plan.json"),
        "run_root": str(tmp_path / "runs"),
        "max_folders": 4, "max_batches": 3, "max_reduced_chars": 240_000,
        "context_rows": 8, "sla_target_runs": 3,
        "bootstrap_safety_hours": 48,
        "success_log_dir": str(tmp_path / "logs"),
        "date_from": None, "date_to": None,
        "full": False, "weekly": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def seed_empty_state(plan_args):
    pathlib.Path(plan_args.state).write_text(json.dumps(wiki_delta.default_state()))


def plan_json(plan_args):
    return json.loads(pathlib.Path(plan_args.plan).read_text())


def successful_result(plan_args, *, receipt: str | None = None):
    plan = plan_json(plan_args)
    result = pathlib.Path(plan_args.plan).with_name("result.log")
    result.write_text(
        "Background tasks still running.\n"
        + json.dumps({
            "type": "result", "is_error": False, "subtype": "success",
            "result": receipt if receipt is not None else f"WIKI_COMPILE_RECEIPT:{plan['run_id']}",
        })
        + "\n"
    )
    return result


def accept_and_commit(plan_args):
    marker = pathlib.Path(plan_args.plan).with_name("marker.json")
    wiki_delta.command_accept_result(SimpleNamespace(
        plan=plan_args.plan, marker=str(marker), result=str(successful_result(plan_args)),
    ))
    wiki_delta.command_commit(SimpleNamespace(plan=plan_args.plan, marker=str(marker)))


def test_result_requires_exact_plan_bound_receipt(tmp_path):
    plan_args = args(tmp_path)
    seed_empty_state(plan_args)
    scan = empty_scan()
    original = wiki_delta.scan_sources
    wiki_delta.scan_sources = lambda: scan
    try:
        wiki_delta.command_plan(plan_args)
    finally:
        wiki_delta.scan_sources = original
    marker = tmp_path / "marker.json"
    for receipt in ("", "WIKI_COMPILE_RECEIPT:wrong"):
        with pytest.raises(ValueError, match="plan-bound"):
            wiki_delta.command_accept_result(SimpleNamespace(
                plan=plan_args.plan, marker=str(marker),
                result=str(successful_result(plan_args, receipt=receipt)),
            ))
    wiki_delta.command_accept_result(SimpleNamespace(
        plan=plan_args.plan, marker=str(marker), result=str(successful_result(plan_args)),
    ))


def test_result_rejects_unrelated_json_and_error_subtype(tmp_path):
    result = tmp_path / "result.log"
    result.write_text(json.dumps({"is_error": False}) + "\n")
    assert not wiki_delta.result_is_success(result)
    result.write_text(json.dumps({
        "type": "result", "is_error": False, "subtype": "error_max_budget_usd",
    }) + "\n")
    assert not wiki_delta.result_is_success(result)


def test_valid_unterminated_row_waits_then_consumes_once(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    complete = row("A syntactically valid row waits for its terminating newline before capture.")
    transcript.write_bytes(complete.rstrip(b"\n"))
    scan = empty_scan(); scan["morty"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    assert plan_json(plan_args)["selected_chunk_ids"] == []
    accept_and_commit(plan_args)
    transcript.write_bytes(complete)
    wiki_delta.command_plan(plan_args)
    assert len(plan_json(plan_args)["selected_chunk_ids"]) == 1
    accept_and_commit(plan_args)
    wiki_delta.command_plan(plan_args)
    assert plan_json(plan_args)["selected_chunk_ids"] == []


def test_append_has_prior_context_but_capture_watermark_only_new_bytes(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    request = row("Please implement the durable queue and verify it before claiming completion.")
    transcript.write_bytes(request)
    scan = empty_scan(); scan["rick_rnd"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args); accept_and_commit(plan_args)
    prior_offset = json.loads(pathlib.Path(plan_args.state).read_text())["capture_files"][str(transcript)]["offset"]

    reply = row("Implemented that and the verification output confirms the queue now passes.", "assistant")
    transcript.write_bytes(request + reply)
    wiki_delta.command_plan(plan_args)
    plan = plan_json(plan_args)
    feed = pathlib.Path(plan["aggregate_feed"]).read_text()
    assert "[CONTEXT_ONLY" in feed and "Please implement" in feed
    assert "[NEW" in feed and "Implemented that" in feed
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert state["capture_files"][str(transcript)]["offset"] == prior_offset + len(reply)
    chunk = state["delta_queue"][0]
    assert chunk["new_start"] == prior_offset


def test_post_frontier_capture_survives_failed_plan_and_source_deletion(monkeypatch, tmp_path):
    transcript = tmp_path / "active.jsonl"
    initial = b"".join(
        row(f"Active frozen chunk {i} remains ahead of the next capture frontier.")
        for i in range(3)
    )
    transcript.write_bytes(initial)
    scan = empty_scan(); scan["tony_ceo"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path, max_folders=1, max_reduced_chars=120); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    failed_run = plan_json(plan_args)["run_id"]
    transcript.write_bytes(initial + row(
        "VALUABLE_POST_FRONTIER must survive even if its source disappears before active drain."
    ))
    # Replanning the still-uncommitted failed plan captures behind it, then
    # reuses the same semantic run without disturbing the active frontier.
    wiki_delta.command_plan(plan_args)
    assert plan_json(plan_args)["run_id"] == failed_run
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert state["next_delta_queue"]
    next_spool = pathlib.Path(state["next_delta_queue"][0]["feed_path"])
    assert "VALUABLE_POST_FRONTIER" in next_spool.read_text()
    transcript.unlink(); scan["tony_ceo"] = []
    accept_and_commit(plan_args)
    selected_post_frontier = False
    for _ in range(8):
        wiki_delta.command_plan(plan_args)
        feed = pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
        if "VALUABLE_POST_FRONTIER" in feed:
            selected_post_frontier = True
            break
        accept_and_commit(plan_args)
    assert selected_post_frontier


def test_forward_lookahead_shows_proof_without_acking_it(monkeypatch, tmp_path):
    transcript = tmp_path / "claims.jsonl"
    claim = "I completed the queue migration and it is fully deployed to the target service."
    proof = "Verification proof: service readback shows the new queue generation is active."
    transcript.write_bytes(row(claim, "assistant") + row(proof, "assistant"))
    scan = empty_scan(); scan["morty"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path, max_folders=1, max_reduced_chars=150); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    first = pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
    assert f"[NEW morty" in first and claim in first
    assert "[CONTEXT_ONLY morty" in first and proof in first
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert len(state["delta_queue"]) == 2  # proof is still its own unacked NEW chunk
    accept_and_commit(plan_args)
    wiki_delta.command_plan(plan_args)
    second = pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
    assert "[NEW morty" in second and proof in second


def test_frozen_frontier_drains_before_hot_append_cycle(monkeypatch, tmp_path):
    transcript = tmp_path / "hot.jsonl"
    initial = b"".join(row(f"Frozen backlog item {i} has enough semantic content for capture.") for i in range(3))
    transcript.write_bytes(initial)
    scan = empty_scan(); scan["tony_ceo"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path, max_folders=1, max_reduced_chars=120); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    first_cycle = json.loads(pathlib.Path(plan_args.state).read_text())["delta_cycle"]["id"]
    transcript.write_bytes(initial + row("Hot append belongs only to the next frozen capture cycle."))
    seen = ""
    while json.loads(pathlib.Path(plan_args.state).read_text())["delta_cycle"]["id"] == first_cycle:
        seen += pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
        accept_and_commit(plan_args)
        state = json.loads(pathlib.Path(plan_args.state).read_text())
        if state["delta_queue"] and state["delta_cycle"]["id"] == first_cycle:
            wiki_delta.command_plan(plan_args)
    assert "Hot append" not in seen
    wiki_delta.command_plan(plan_args)
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert state["delta_cycle"]["id"] != first_cycle
    assert "Hot append" in pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()


def test_empty_drain_clears_cycle_before_later_append(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(row("The first isolated cycle drains without any next-frontier work waiting."))
    scan = empty_scan(); scan["tony_ceo"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    first_cycle = json.loads(pathlib.Path(plan_args.state).read_text())["delta_cycle"]
    accept_and_commit(plan_args)
    drained = json.loads(pathlib.Path(plan_args.state).read_text())
    assert drained["delta_queue"] == [] and drained["delta_cycle"] is None

    transcript.write_bytes(transcript.read_bytes() + row(
        "A later append must receive a fresh cycle identifier and capture timestamp."
    ))
    monkeypatch.setattr(wiki_delta, "utc_now", lambda: "2099-01-02T03:04:05Z")
    wiki_delta.command_plan(plan_args)
    later_cycle = json.loads(pathlib.Path(plan_args.state).read_text())["delta_cycle"]
    assert later_cycle["id"] != first_cycle["id"]
    assert later_cycle["captured_at"] == "2099-01-02T03:04:05Z"


def test_saturday_failure_is_upgraded_to_weekly_on_sunday(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(row("Monday backlog evidence must join a resumed Saturday plan on Sunday."))
    scan = empty_scan(); scan["ben_fund"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args); accept_and_commit(plan_args)
    transcript.write_bytes(transcript.read_bytes() + row(
        "Saturday work fails before its semantic acknowledgement and must resume Sunday."
    ))
    wiki_delta.command_plan(plan_args)
    saturday = plan_json(plan_args); assert not saturday["weekly"]
    plan_args.weekly = True
    wiki_delta.command_plan(plan_args)
    sunday = plan_json(plan_args)
    assert sunday["run_id"] == saturday["run_id"] and sunday["weekly"]
    weekly = pathlib.Path(sunday["weekly_aggregate_feed"]).read_text()
    assert "Monday backlog" in weekly and "Saturday work" in weekly


def test_full_generation_persists_and_progresses_in_bounded_runs(monkeypatch, tmp_path):
    transcript = tmp_path / "history.jsonl"
    transcript.write_bytes(b"".join(
        row(f"Historical full rebuild item {i} requires bounded persistent processing.")
        for i in range(4)
    ))
    scan = empty_scan(); scan["claudette"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path, max_folders=1, max_reduced_chars=120); seed_empty_state(plan_args)
    # First drain the live delta capture, then request a separate full generation.
    wiki_delta.command_plan(plan_args)
    while json.loads(pathlib.Path(plan_args.state).read_text())["delta_queue"]:
        accept_and_commit(plan_args)
        if json.loads(pathlib.Path(plan_args.state).read_text())["delta_queue"]:
            wiki_delta.command_plan(plan_args)
    plan_args.full = True
    wiki_delta.command_plan(plan_args)
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    generation_id = state["full_generation"]["id"]
    initial = len(state["full_generation"]["queue"]); assert initial > 1
    plan_args.full = False
    runs = 0
    while json.loads(pathlib.Path(plan_args.state).read_text())["full_generation"]:
        assert plan_json(plan_args)["queue_kind"] == "full"
        accept_and_commit(plan_args); runs += 1
        if json.loads(pathlib.Path(plan_args.state).read_text())["full_generation"]:
            wiki_delta.command_plan(plan_args)
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert runs > 1 and state["last_full_generation"]["id"] == generation_id


def test_bootstrap_seeds_old_history_and_queues_recent(monkeypatch, tmp_path):
    old = tmp_path / "old.jsonl"; recent = tmp_path / "recent.jsonl"
    old.write_bytes(row("Previously covered history is seeded from the legacy success watermark."))
    recent.write_bytes(row("Recent overlap remains queued during migration to the durable planner."))
    os.utime(old, (1, 1))
    cutoff = wiki_delta.datetime.now(wiki_delta.timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(wiki_delta, "bootstrap_cutoff", lambda *_: (cutoff, "test-success"))
    scan = empty_scan(); scan["maya_sales"] = [old, recent]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path)
    wiki_delta.command_plan(plan_args)
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert state["capture_files"][str(old)]["bootstrap_seeded"]
    assert "Recent overlap" in pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()


def test_plan_date_window_filters_transcript_mtime_inclusive(monkeypatch, tmp_path):
    before = tmp_path / "before.jsonl"
    start = tmp_path / "start.jsonl"
    end = tmp_path / "end.jsonl"
    after = tmp_path / "after.jsonl"
    for path in (before, start, end, after):
        path.write_bytes(row(path.stem))
    for path, modified in (
        (before, datetime(2026, 9, 17, 23, 59, 59, tzinfo=timezone.utc)),
        (start, datetime(2026, 9, 18, 0, 0, 0, tzinfo=timezone.utc)),
        (end, datetime(2026, 9, 18, 23, 59, 59, tzinfo=timezone.utc)),
        (after, datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)),
    ):
        os.utime(path, (modified.timestamp(), modified.timestamp()))
    scan = empty_scan(); scan["maya_sales"] = [before, start, end, after]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    monkeypatch.setattr(
        wiki_delta, "bootstrap_cutoff",
        lambda *_: pytest.fail("date window must replace bootstrap cutoff"),
    )
    plan_args = args(
        tmp_path, date_from=date(2026, 9, 18), date_to=date(2026, 9, 18)
    )
    wiki_delta.command_plan(plan_args)
    feed = pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
    assert "start" in feed and "end" in feed
    assert "before" not in feed and "after" not in feed
    assert plan_json(plan_args)["date_window"] == {
        "from": "2026-09-18", "to": "2026-09-18",
    }
    with pytest.raises(ValueError, match="--from must be <= --to"):
        wiki_delta.date_window(date(2026, 9, 19), date(2026, 9, 18))


def test_fresh_date_window_bootstrap_prevents_later_historical_ingestion(
    monkeypatch, tmp_path,
):
    before = tmp_path / "before.jsonl"
    current = tmp_path / "current.jsonl"
    after = tmp_path / "after.jsonl"
    for path in (before, current, after):
        path.write_bytes(row(path.stem))
    for path, modified in (
        (before, datetime(2026, 9, 17, 12, tzinfo=timezone.utc)),
        (current, datetime(2026, 9, 18, 12, tzinfo=timezone.utc)),
        (after, datetime(2026, 9, 19, 12, tzinfo=timezone.utc)),
    ):
        os.utime(path, (modified.timestamp(), modified.timestamp()))
    scan = empty_scan(); scan["maya_sales"] = [before, current, after]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(
        tmp_path, date_from=date(2026, 9, 18), date_to=date(2026, 9, 18)
    )

    wiki_delta.command_plan(plan_args)
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert state["capture_files"][str(before)]["bootstrap_seeded"]
    assert state["bootstrap"]["source"] == "operator_date_window"
    assert state["bootstrap"]["from"] == "2026-09-18"
    assert state["bootstrap"]["to"] == "2026-09-18"
    assert "current" in pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
    accept_and_commit(plan_args)

    plan_args.date_from = None
    plan_args.date_to = None
    wiki_delta.command_plan(plan_args)
    feed = pathlib.Path(plan_json(plan_args)["aggregate_feed"]).read_text()
    assert "after" in feed
    assert "before" not in feed


def test_date_window_supersedes_stuck_plan_without_dropping_queue(monkeypatch, tmp_path):
    old = tmp_path / "old.jsonl"
    current = tmp_path / "current.jsonl"
    old.write_bytes(row("outside requested date window"))
    current.write_bytes(row("inside requested date window"))
    for path, modified in (
        (old, datetime(2026, 9, 17, 12, tzinfo=timezone.utc)),
        (current, datetime(2026, 9, 18, 12, tzinfo=timezone.utc)),
    ):
        os.utime(path, (modified.timestamp(), modified.timestamp()))
    scan = empty_scan(); scan["rick_rnd"] = [old, current]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    stuck_run = plan_json(plan_args)["run_id"]

    plan_args.date_from = date(2026, 9, 18)
    plan_args.date_to = date(2026, 9, 18)
    wiki_delta.command_plan(plan_args)
    replacement = plan_json(plan_args)
    feed = pathlib.Path(replacement["aggregate_feed"]).read_text()
    state = json.loads(pathlib.Path(plan_args.state).read_text())
    assert replacement["run_id"] != stuck_run
    assert "inside requested" in feed and "outside requested" not in feed
    assert len(state["delta_queue"]) == 2


def test_malformed_complete_row_fails_closed(monkeypatch, tmp_path):
    transcript = tmp_path / "bad.jsonl"; transcript.write_bytes(b"{bad json}\n")
    scan = empty_scan(); scan["morty"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    with pytest.raises(ValueError, match="malformed transcript row"):
        wiki_delta.command_plan(plan_args)
    assert json.loads(pathlib.Path(plan_args.state).read_text())["capture_files"] == {}


def test_tampered_plan_feed_cannot_be_acknowledged(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(row("A digest-protected semantic feed cannot be modified before acknowledgement."))
    scan = empty_scan(); scan["rick_rnd"] = [transcript]
    monkeypatch.setattr(wiki_delta, "scan_sources", lambda: scan)
    plan_args = args(tmp_path); seed_empty_state(plan_args)
    wiki_delta.command_plan(plan_args)
    plan = plan_json(plan_args)
    pathlib.Path(plan["aggregate_feed"]).write_text("tampered\n")
    marker = tmp_path / "marker.json"
    with pytest.raises(ValueError, match="SHA-256"):
        wiki_delta.command_accept_result(SimpleNamespace(
            plan=plan_args.plan, marker=str(marker), result=str(successful_result(plan_args)),
        ))
    assert not marker.exists()


def test_skill_contract_has_receipt_and_pr_only_proposal_grouping():
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    assert "WIKI_COMPILE_RECEIPT:{run_id}" in skill
    assert "FINAL COMPLETION RECEIPT" in skill
    assert "Group ALL A blocks by proposal page" in skill
    assert "without editing the live wiki checkout" in skill
    assert "propose-operator-intents.py" in skill
    assert "git push --dry-run" in skill


def test_skill_contract_separates_shared_wiki_proposal_pr_from_vault():
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    assert "auto-pushed shared-wiki\n`main` checkout" in skill
    assert "named-branch shared-wiki PR" in skill
    assert "Joris reviews/merges that proposal PR" in skill
    assert "separately and manually" in skill
    assert "no agent performs a CORE/wiki" in skill


def test_skill_step10_never_reads_the_denied_secret_and_only_queues_a_file():
    """Board #1482: STEP 10 must not attempt to read /run/claude-agent/env (the
    sandboxed headless session is denied that path) or call Telegram itself —
    it only queues plain report text to a file for the launcher to send."""
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    step10 = skill.split("## STEP 10", 1)[1].split("## STEP 11", 1)[0]
    assert "best-effort" in step10
    # The path may be MENTIONED (to explain the boundary), but no code in
    # this step may actually read it or hit the Telegram API directly.
    assert "awk -F= '/^TELEGRAM_BOT_TOKEN" not in step10
    assert "api.telegram.org" not in step10
    assert "curl" not in step10
    assert "REPORT_FILE=/home/claude/monitoring/wiki-compile-delta/telegram-report.txt" in step10
    # Quiet-night behavior must clear, never resend, a stale queued message.
    assert 'rm -f "$REPORT_FILE"' in step10
    # A queue-write failure must never block STEP 11 or be surfaced in the
    # final response.
    assert "do NOT surface it in your final response" in step10


def test_skill_step11_forbids_any_caveat_in_the_final_turn():
    """Board #1482 hardening: STEP 11 must say explicitly that ANY status or
    caveat (about STEP 10 or anything else) belongs before the final turn,
    never inside it — the exact-string receipt check has zero tolerance for a
    trailing sentence, even a true and helpful one."""
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    step11 = skill.split("## STEP 11", 1)[1]
    normalized = " ".join(step11.split())
    assert "belongs BEFORE this final turn, never IN it" in normalized
    assert "EXACT string equality" in normalized
    assert "STEP 10" in step11


def test_launcher_report_send_happens_only_after_commit_succeeds():
    """Board #1482: the launcher's telegram-send block must live in the
    success branch of the two-phase watermark check (only after accept-result
    AND commit both succeed), never unconditionally and never before the
    receipt is verified."""
    script = (REPO / "skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh").read_text()
    commit_idx = script.index('python3 "$DELTA_SCRIPT" commit --plan "$DELTA_PLAN" --marker "$DELTA_MARKER"')
    send_idx = script.index("REPORT_FILE=/home/claude/monitoring/wiki-compile-delta/telegram-report.txt")
    else_idx = script.rindex("else", commit_idx, send_idx)
    assert commit_idx < else_idx < send_idx, "report send must be in the commit success (else) branch"
    # Must read the already-filtered, non-secret headless env — never the raw
    # /run/claude-agent/env the model itself is denied.
    tail = script[send_idx:]
    assert '/run/bubble-headless-cloud-wiki-${MODE}/env' in tail
    assert 'rm -f "$REPORT_FILE"' in tail
