"""
test_list_pending_gates_decision_filter.py

Issue #201: a gate card {{OPERATOR}} has already approved/refused must immediately
leave the "Décisions qu'on attend de toi" list — even before the dept's agent
processes the decision.

ROOT CAUSE: write_gate_decision writes inbox/decisions/<id>.yaml but does NOT
touch the gate YAML in queues/gates/.  The gate YAML only gets resolved:true
later, when the agent loop drains the inbox.  So between approval and agent
processing, list_pending_gates was still returning the gate as pending.

FIX: list_pending_gates now pre-builds the set of gate ids present in
inbox/decisions/ and skips any gate whose stem matches.

Tests here operate on the service function directly (no HTTP round-trip) so the
assertion is precise and immune to template changes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dept_repo(tmp_path: Path, slug: str = "maya") -> Path:
    """Return a minimal bubble-ops-<slug> directory with a queues/gates/ dir."""
    repo = tmp_path / f"bubble-ops-{slug}"
    (repo / "queues" / "gates").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": slug}}), encoding="utf-8"
    )
    return repo


def _write_gate(repo: Path, gate_id: str) -> Path:
    """Write a minimal pending gate YAML."""
    p = repo / "queues" / "gates" / f"{gate_id}.yaml"
    p.write_text(
        yaml.safe_dump({
            "id": gate_id,
            "kind": "strategic_question",
            "source_layer": 1,
            "target_layer": 2,
            "risk_level": "high",
            "requires_human": True,
            "current_mode": "manual_required",
        }, sort_keys=False),
        encoding="utf-8",
    )
    return p


def _write_decision(repo: Path, gate_id: str) -> Path:
    """Simulate write_gate_decision: land inbox/decisions/<id>.yaml on disk."""
    decisions_dir = repo / "inbox" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    p = decisions_dir / f"{gate_id}.yaml"
    p.write_text(
        yaml.safe_dump({
            "gate_id": gate_id,
            "decision": "approve",
            "decided_by": "joris",
        }, sort_keys=False),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gate_without_decision_file_is_returned(tmp_path, monkeypatch):
    """A gate with no matching inbox/decisions/<id>.yaml must still appear as
    pending — the normal, not-yet-actioned case."""
    from console.services import github_reader

    repo = _make_dept_repo(tmp_path)
    _write_gate(repo, "strategic_question-20260620-101454")

    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    gates = github_reader.list_pending_gates("maya")
    ids = [g.get("id") for g in gates]
    assert "strategic_question-20260620-101454" in ids, (
        "Gate without a decision file must remain in list_pending_gates output"
    )


def test_gate_with_decision_file_is_excluded(tmp_path, monkeypatch):
    """A gate whose id has a matching inbox/decisions/<id>.yaml must be
    excluded from list_pending_gates — {{OPERATOR}} already acted, so it must
    disappear from 'Décisions qu'on attend de toi' immediately, before the
    dept agent processes the inbox."""
    from console.services import github_reader

    repo = _make_dept_repo(tmp_path)
    gate_id = "strategic_question-20260620-101454"
    _write_gate(repo, gate_id)
    _write_decision(repo, gate_id)  # simulate cockpit approval

    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    gates = github_reader.list_pending_gates("maya")
    ids = [g.get("id") for g in gates]
    assert gate_id not in ids, (
        "Gate with a matching inbox/decisions file must be excluded from "
        "list_pending_gates — operator has already decided"
    )


def test_only_decided_gate_excluded_others_remain(tmp_path, monkeypatch):
    """When a dept has multiple pending gates and only one is decided, only
    the decided gate is excluded — the others must still show up."""
    from console.services import github_reader

    repo = _make_dept_repo(tmp_path)
    _write_gate(repo, "gate-decided")
    _write_gate(repo, "gate-still-pending")
    _write_decision(repo, "gate-decided")

    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    gates = github_reader.list_pending_gates("maya")
    ids = [g.get("id") for g in gates]

    assert "gate-decided" not in ids, "decided gate must be filtered out"
    assert "gate-still-pending" in ids, "undecided gate must remain visible"


def test_no_inbox_decisions_dir_is_safe(tmp_path, monkeypatch):
    """When inbox/decisions/ does not exist at all (fresh dept, no decisions
    ever taken), list_pending_gates must not crash and must return all gates."""
    from console.services import github_reader

    repo = _make_dept_repo(tmp_path)
    _write_gate(repo, "gate-alpha")
    # Deliberately do NOT create inbox/decisions/

    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    gates = github_reader.list_pending_gates("maya")
    ids = [g.get("id") for g in gates]
    assert "gate-alpha" in ids


def test_gate_already_resolved_in_yaml_still_excluded(tmp_path, monkeypatch):
    """Pre-existing behaviour: a gate with resolved:true in the YAML (after
    the agent processed the inbox) is still excluded — the new decision-file
    check must not break this."""
    from console.services import github_reader

    repo = _make_dept_repo(tmp_path)
    gate_id = "resolved-gate"
    p = repo / "queues" / "gates" / f"{gate_id}.yaml"
    p.write_text(
        yaml.safe_dump({
            "id": gate_id,
            "kind": "trade_order",
            "resolved": True,
            "decided_by": "joris",
            "current_mode": "manual_required",
        }, sort_keys=False),
        encoding="utf-8",
    )
    # No decision file — gate was already processed by the agent
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    gates = github_reader.list_pending_gates("maya")
    ids = [g.get("id") for g in gates]
    assert gate_id not in ids, "resolved:true gate must still be excluded"
