"""Tests for gate-directory hygiene (#1076, Rick 2026-09-03).

`queues/gates/` grew to 37 cards while only 1 was genuinely open: L3 archived
the DECISION (to inbox/decisions/.processed/ or .abandoned/) but never the
corresponding GATE CARD (queues/gates/<id>.yaml), so resolved cards accumulated
and drowned the open ones. These tests guard the durable code fix:

  - archive_gate_card()  — inline publish/abandon archival (atomic, idempotent)
  - reconcile_gate_dir() — startup sweep for already-resolved orphans
  - count_open_gates()   — open-count that excludes .done/**
  - build_dispatch_ctx() — wires the reconcile into the mutating (materialize) branch
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.lib.dispatch_helpers import (  # noqa: E402
    archive_gate_card,
    count_open_gates,
    reconcile_gate_dir,
    build_dispatch_ctx,
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _gate(repo: Path, gate_id: str, body: str | None = None) -> Path:
    d = repo / "queues" / "gates"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{gate_id}.yaml"
    p.write_text(body or f"id: {gate_id}\nkind: trade_proposal\n", encoding="utf-8")
    return p


def _decision(repo: Path, gate_id: str, outcome: str) -> Path:
    """Simulate L3 having archived a decision: outcome in {processed, abandoned}."""
    sub = ".processed" if outcome == "processed" else ".abandoned"
    d = repo / "inbox" / "decisions" / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{gate_id}.yaml"
    p.write_text(f"id: {gate_id}\naction: {outcome}\n", encoding="utf-8")
    return p


# ── 1. publish → archives gate card ──────────────────────────────────────────
def test_publish_archives_gate_card(tmp_path):
    card = _gate(tmp_path, "pub-gate-1")
    dest = archive_gate_card(tmp_path, "pub-gate-1", "published")
    assert dest is not None
    assert not card.exists()
    assert dest == tmp_path / "queues" / "gates" / ".done" / "published" / "pub-gate-1.yaml"
    assert dest.is_file()
    assert "pub-gate-1" in dest.read_text()


# ── 2. abandon → archives gate card ──────────────────────────────────────────
def test_abandon_archives_gate_card(tmp_path):
    card = _gate(tmp_path, "aband-gate-1")
    dest = archive_gate_card(tmp_path, "aband-gate-1", "abandoned")
    assert dest is not None
    assert not card.exists()
    assert dest == tmp_path / "queues" / "gates" / ".done" / "abandoned" / "aband-gate-1.yaml"
    assert dest.is_file()


# ── archive_gate_card edge cases ─────────────────────────────────────────────
def test_archive_is_idempotent_when_source_absent(tmp_path):
    # No card on disk → no-op, returns None (already archived / never existed).
    (tmp_path / "queues" / "gates").mkdir(parents=True)
    assert archive_gate_card(tmp_path, "ghost", "published") is None


def test_archive_rejects_bad_outcome(tmp_path):
    _gate(tmp_path, "g")
    with pytest.raises(ValueError):
        archive_gate_card(tmp_path, "g", "approved")  # not published/abandoned


def test_archive_rejects_path_escaping_id(tmp_path):
    _gate(tmp_path, "g")
    # A traversal-y id must never resolve outside queues/gates/.
    assert archive_gate_card(tmp_path, "../../etc/passwd", "published") is None
    assert (tmp_path / "queues" / "gates" / "g.yaml").exists()  # untouched


# ── 3. startup reconcile → archives orphaned resolved cards ──────────────────
def test_reconcile_archives_orphaned_published(tmp_path):
    _gate(tmp_path, "resolved-pub")
    _decision(tmp_path, "resolved-pub", "processed")
    moved = reconcile_gate_dir(tmp_path)
    assert len(moved) == 1
    assert not (tmp_path / "queues" / "gates" / "resolved-pub.yaml").exists()
    assert (tmp_path / "queues" / "gates" / ".done" / "published" / "resolved-pub.yaml").is_file()


def test_reconcile_archives_orphaned_abandoned(tmp_path):
    _gate(tmp_path, "resolved-ab")
    _decision(tmp_path, "resolved-ab", "abandoned")
    moved = reconcile_gate_dir(tmp_path)
    assert len(moved) == 1
    assert (tmp_path / "queues" / "gates" / ".done" / "abandoned" / "resolved-ab.yaml").is_file()


def test_reconcile_leaves_genuinely_open_card(tmp_path):
    _gate(tmp_path, "still-open")  # no decision anywhere
    moved = reconcile_gate_dir(tmp_path)
    assert moved == []
    assert (tmp_path / "queues" / "gates" / "still-open.yaml").is_file()


def test_reconcile_matches_by_doc_id_not_just_stem(tmp_path):
    # Card whose top-level id differs from its filename stem; the decision was
    # keyed by the doc id — reconcile must still match.
    _gate(tmp_path, "filestem", body="id: doc-id-xyz\nkind: trade_proposal\n")
    _decision(tmp_path, "doc-id-xyz", "processed")
    moved = reconcile_gate_dir(tmp_path)
    assert len(moved) == 1
    # Archived under the real filename.
    assert (tmp_path / "queues" / "gates" / ".done" / "published" / "filestem.yaml").is_file()


def test_reconcile_is_idempotent(tmp_path):
    _gate(tmp_path, "r")
    _decision(tmp_path, "r", "processed")
    assert len(reconcile_gate_dir(tmp_path)) == 1
    assert reconcile_gate_dir(tmp_path) == []  # nothing left to move


def test_reconcile_missing_gates_dir_is_safe(tmp_path):
    assert reconcile_gate_dir(tmp_path) == []


# ── 4. open-gate count excludes .done/ ───────────────────────────────────────
def test_count_open_gates_excludes_done(tmp_path):
    _gate(tmp_path, "open-a")
    _gate(tmp_path, "open-b")
    # Two archived + a dotfile that must not be counted.
    archive_gate_card(tmp_path, "open-a", "published")  # -> .done/published
    done_ab = tmp_path / "queues" / "gates" / ".done" / "abandoned"
    done_ab.mkdir(parents=True, exist_ok=True)
    (done_ab / "old.yaml").write_text("id: old\n", encoding="utf-8")
    (tmp_path / "queues" / "gates" / ".gitkeep").write_text("", encoding="utf-8")
    # Only open-b remains genuinely open.
    assert count_open_gates(tmp_path) == 1


def test_count_open_gates_missing_dir(tmp_path):
    assert count_open_gates(tmp_path) == 0


# ── reconciliation is explicit; DECIDE never mutates ─────────────────────────
def test_reconcile_gate_dir_is_explicit(tmp_path):
    _gate(tmp_path, "ctx-resolved")
    _decision(tmp_path, "ctx-resolved", "processed")
    _gate(tmp_path, "ctx-open")  # genuinely open, must survive
    reconcile_gate_dir(tmp_path)
    assert not (tmp_path / "queues" / "gates" / "ctx-resolved.yaml").exists()
    assert (tmp_path / "queues" / "gates" / ".done" / "published" / "ctx-resolved.yaml").is_file()
    assert (tmp_path / "queues" / "gates" / "ctx-open.yaml").is_file()
    assert count_open_gates(tmp_path) == 1


def test_build_dispatch_ctx_readonly_does_not_reconcile(tmp_path):
    # materialize=False is the read-only gate/probe path (#454) — it must NOT
    # move files as a side effect.
    _gate(tmp_path, "probe-resolved")
    _decision(tmp_path, "probe-resolved", "processed")
    build_dispatch_ctx(tmp_path, materialize=False)
    assert (tmp_path / "queues" / "gates" / "probe-resolved.yaml").is_file()  # untouched
