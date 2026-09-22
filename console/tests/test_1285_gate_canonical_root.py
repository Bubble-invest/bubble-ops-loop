"""#1285 — the cockpit must read Maya's (and any dept's) gate content —
including cold-outreach draft bodies — from the CURRENT canonical runtime
checkout, not a legacy `disk_root()` mirror a uid-isolation cutover can leave
permanently orphaned.

Confirmed on the box (2026-09-22): bubble-ops-maya's legacy mirror
(`/home/claude/agents/bubble-ops-maya`, what `repo_path()` resolves to) froze
at the exact commit of the #1299 resync (2026-09-13) and never advanced again,
while Maya's actual live checkout moved to `/srv/agents/maya` (uid-isolated,
`agent-maya`) and kept advancing daily. `list_pending_gates` and friends read
ONLY `repo_path()`, so the cockpit was serving 9-day-old (and counting) gate
content — including the exact prospect_email/prospect_dm cold-outreach drafts
#1299 believed were already resolved.

This mirrors the #1209 canonical-NAV / whiteboard fix exactly: prefer
`runtime_repo_path(slug)` (which resolves the uid-isolated `/srv/agents/<slug>`
canonical workdir first) and fall back to the legacy `repo_path(slug)` when no
canonical workdir exists (pre-isolation depts, local-dev fixtures).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _write_gate(repo_root: Path, gate_id: str, fields: dict) -> None:
    gates_dir = repo_root / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(fields, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _setup_legacy_and_canonical(tmp_path, monkeypatch, slug="maya"):
    """Build a legacy `bubble-ops-<slug>` mirror (stale) and a canonical
    `/srv/agents/<slug>`-shaped workdir (fresh), wired the same way
    test_canonical_agent_root.py does."""
    from console import settings

    legacy_root = tmp_path / "legacy"
    canonical_root = tmp_path / "canonical"
    legacy_root.mkdir()
    canonical_root.mkdir()
    legacy = legacy_root / f"bubble-ops-{slug}"
    canonical = canonical_root / slug
    legacy.mkdir()
    canonical.mkdir()

    monkeypatch.setattr(settings, "READ_FROM_DISK", str(legacy_root))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(canonical_root))
    return legacy, canonical


def test_list_pending_gates_prefers_canonical_over_legacy_mirror(tmp_path, monkeypatch):
    """The orphaned-mirror scenario: legacy still has the OLD prospect_email
    gate (pre-resolution), canonical has moved on (gate resolved/archived,
    replaced by a fresh one). The cockpit must show the canonical state."""
    from console.services import github_reader

    legacy, canonical = _setup_legacy_and_canonical(tmp_path, monkeypatch)

    _write_gate(legacy, "prospect_email-stale-1", {
        "id": "prospect_email-stale-1", "kind": "prospect_email",
        "draft_message": "STALE pre-doctrine body (frozen mirror)",
    })
    _write_gate(canonical, "news_post-fresh-1", {
        "id": "news_post-fresh-1", "kind": "news_post",
        "post_body": "CURRENT body from the live checkout",
    })

    gates = github_reader.list_pending_gates("maya")
    ids = {g["id"] for g in gates}
    assert "news_post-fresh-1" in ids, "must read the live canonical checkout"
    assert "prospect_email-stale-1" not in ids, (
        "must NOT fall back to the orphaned legacy mirror when a canonical "
        "workdir exists"
    )


def test_list_pending_gates_falls_back_to_legacy_when_no_canonical(tmp_path, monkeypatch):
    """Pre-isolation depts (no /srv/agents/<slug>) must keep working exactly
    as before — legacy is the only tree, so it's used."""
    from console import settings
    from console.services import github_reader

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy = legacy_root / "bubble-ops-ben"
    legacy.mkdir()
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(legacy_root))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(tmp_path / "absent"))

    _write_gate(legacy, "trade-1", {"id": "trade-1", "kind": "trade_proposal"})
    gates = github_reader.list_pending_gates("ben")
    assert {g["id"] for g in gates} == {"trade-1"}


def test_load_gate_direct_prefers_canonical(tmp_path, monkeypatch):
    from console.services import github_reader

    legacy, canonical = _setup_legacy_and_canonical(tmp_path, monkeypatch)
    _write_gate(legacy, "dm-1", {"id": "dm-1", "draft_message": "stale dm"})
    _write_gate(canonical, "dm-1", {"id": "dm-1", "draft_message": "current dm"})

    doc = github_reader.load_gate_direct("maya", "dm-1")
    assert doc is not None
    assert doc["draft_message"] == "current dm"


def test_load_gate_raw_prefers_canonical(tmp_path, monkeypatch):
    from console.services import github_reader

    legacy, canonical = _setup_legacy_and_canonical(tmp_path, monkeypatch)
    _write_gate(legacy, "dm-2", {"id": "dm-2", "draft_message": "stale"})
    _write_gate(canonical, "dm-2", {"id": "dm-2", "draft_message": "current"})

    raw = github_reader.load_gate_raw("maya", "dm-2")
    assert raw is not None
    assert "current" in raw
    assert "stale" not in raw


def test_read_gate_payload_text_prefers_canonical(tmp_path, monkeypatch):
    """A gate's approval_bridge.item_ref payload (long-form draft under
    outputs/) must also come from the canonical checkout."""
    from console.services import github_reader

    legacy, canonical = _setup_legacy_and_canonical(tmp_path, monkeypatch)
    rel = "outputs/2026-09-21/prospect_email-1.md"
    (legacy / "outputs" / "2026-09-21").mkdir(parents=True)
    (legacy / rel).write_text("STALE draft body", encoding="utf-8")
    (canonical / "outputs" / "2026-09-21").mkdir(parents=True)
    (canonical / rel).write_text("CURRENT draft body", encoding="utf-8")

    text = github_reader.read_gate_payload_text("maya", rel)
    assert text == "CURRENT draft body"


def test_read_gate_payload_text_falls_back_to_legacy_when_no_canonical(tmp_path, monkeypatch):
    from console import settings
    from console.services import github_reader

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy = legacy_root / "bubble-ops-ben"
    legacy.mkdir()
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(legacy_root))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(tmp_path / "absent"))

    rel = "outputs/2026-09-21/note.md"
    (legacy / "outputs" / "2026-09-21").mkdir(parents=True)
    (legacy / rel).write_text("only copy", encoding="utf-8")

    assert github_reader.read_gate_payload_text("ben", rel) == "only copy"
