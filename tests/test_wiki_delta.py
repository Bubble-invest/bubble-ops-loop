from __future__ import annotations

import importlib.util
import json
import os
import pathlib
from datetime import timedelta
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


def test_skill_contract_has_receipt_and_atomic_proposal_grouping():
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    assert "WIKI_COMPILE_RECEIPT:{run_id}" in skill
    assert "FINAL COMPLETION RECEIPT" in skill
    assert "Group ALL A blocks by proposal page" in skill
    assert "one atomic Edit/Write per proposal page" in skill


def test_skill_contract_separates_compile_from_core_pr_builder():
    skill = (REPO / "skills/cloud-wiki-compile/SKILL.md").read_text()
    assert "scheduled/live compiler always runs in the shared `main` checkout" in skill
    assert "authorization arrives in its trusted launch/task input" in skill
    assert "named non-main branch" in skill
    assert "`intent-proposer` PR-only credential" in skill
    assert "only Joris manually merges" in skill
    assert "It never uses `WIKI_ALLOW_CORE_EDIT`" in skill
