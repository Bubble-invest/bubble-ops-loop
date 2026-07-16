"""
test_gate_anteriority.py — per-card "when was this created" display for
pending gate/queue cards (board #666, Jade's triage need).

Every pending gate must show its origin date (compact "12/07 · il y a 4 j")
in both the list/batch view and the single-card detail view, so an operator
can spot the oldest pending proposals without opening each one.

Resolution order for the date, first hit wins (github_reader._attach_gate_date):
  1. gate.created  (real gates confirmed to set this, both Ben and content dept)
  2. a YYYY-MM-DD embedded in gate.id (content-dept ids always end in one)
  3. the gate YAML file's mtime on disk (last-resort fallback)
Gates older than humanize.GATE_AGE_WARNING_DAYS (7d) get an amber "stale"
treatment (gate_age_is_stale). Pending gates are listed oldest-first.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


def _write_gate(repo_root: Path, gate_id: str, extra: dict | None = None) -> Path:
    gates = repo_root / "queues" / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": gate_id,
        "kind": "trade_proposal",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "ticker": "TST",
        "side": "buy",
        "proposed_qty": 5,
        "summary": "Anteriority test gate.",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    if extra:
        doc.update(extra)
    p = gates / f"{gate_id}.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def ben_root(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. `created` field is the primary source ─────────────────────────────────

def test_gate_date_from_created_field(ben_root, monkeypatch):
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    _write_gate(ben_root, "created-gate-1", {"created": "2026-07-01"})
    gates = list_pending_gates("fixture")
    g = next(g for g in gates if g["id"] == "created-gate-1")
    assert g["_gate_date"] == date(2026, 7, 1)
    assert isinstance(g["_gate_age_days"], int)


# ── 2. Falls back to the date embedded in the gate id ─────────────────────────

def test_gate_date_falls_back_to_id_embedded_date(ben_root, monkeypatch):
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    # No `created` field at all — id carries the date, content-dept style.
    _write_gate(ben_root, "publish-linkedin-example-2026-06-20")
    gates = list_pending_gates("fixture")
    g = next(g for g in gates if g["id"] == "publish-linkedin-example-2026-06-20")
    assert g["_gate_date"] == date(2026, 6, 20)


# ── 3. Falls back to file mtime when neither is available ────────────────────

def test_gate_date_falls_back_to_mtime(ben_root, monkeypatch):
    import os
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    p = _write_gate(ben_root, "no-date-anywhere")
    target_mtime = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(p, (target_mtime, target_mtime))
    gates = list_pending_gates("fixture")
    g = next(g for g in gates if g["id"] == "no-date-anywhere")
    assert g["_gate_date"] == date(2026, 5, 1)


# ── 4. `created` takes priority over the id-embedded date ────────────────────

def test_gate_date_created_field_wins_over_id(ben_root, monkeypatch):
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    _write_gate(ben_root, "publish-linkedin-example-2026-06-20",
                {"created": "2026-06-18"})
    gates = list_pending_gates("fixture")
    g = next(g for g in gates if g["id"] == "publish-linkedin-example-2026-06-20")
    assert g["_gate_date"] == date(2026, 6, 18)


# ── 5. format_gate_age renders the compact "DD/MM · il y a N j" string ───────

def test_format_gate_age_compact_string():
    from console.services.humanize import format_gate_age
    gate = {"_gate_date": date(2026, 7, 12), "_gate_age_days": 4}
    assert format_gate_age(gate) == "12/07 · il y a 4 j"


def test_format_gate_age_today():
    from console.services.humanize import format_gate_age
    gate = {"_gate_date": date.today(), "_gate_age_days": 0}
    assert "aujourd'hui" in format_gate_age(gate)


def test_format_gate_age_singular_day():
    from console.services.humanize import format_gate_age
    gate = {"_gate_date": date(2026, 7, 15), "_gate_age_days": 1}
    assert format_gate_age(gate) == "15/07 · il y a 1 j"


def test_format_gate_age_none_when_no_date():
    from console.services.humanize import format_gate_age
    assert format_gate_age({"_gate_date": None, "_gate_age_days": None}) is None
    assert format_gate_age({}) is None
    assert format_gate_age(None) is None


# ── 6. gate_age_is_stale — amber highlight threshold ──────────────────────────

def test_gate_age_is_stale_threshold():
    from console.services.humanize import GATE_AGE_WARNING_DAYS, gate_age_is_stale
    assert gate_age_is_stale({"_gate_age_days": GATE_AGE_WARNING_DAYS + 1}) is True
    assert gate_age_is_stale({"_gate_age_days": GATE_AGE_WARNING_DAYS}) is False
    assert gate_age_is_stale({"_gate_age_days": 0}) is False
    assert gate_age_is_stale({"_gate_age_days": None}) is False
    assert gate_age_is_stale({}) is False
    assert gate_age_is_stale(None) is False


# ── 7. Detail + batch views render the age chip ──────────────────────────────

def test_gate_detail_renders_age_chip(client, ben_root):
    _write_gate(ben_root, "age-detail-1", {"created": "2026-06-01"})
    r = client.get("/gate/fixture/age-detail-1")
    assert r.status_code == 200, r.text
    assert "gate-chip--age" in r.text
    assert "01/06" in r.text


def test_gate_batch_renders_age_chip(client, ben_root):
    _write_gate(ben_root, "age-batch-1", {"created": "2026-06-05"})
    r = client.get("/gate/fixture/kind/trade_proposal")
    assert r.status_code == 200, r.text
    assert "gate-chip--age" in r.text
    assert "05/06" in r.text


def test_gate_detail_age_chip_gets_stale_class_past_threshold(client, ben_root):
    old_date = (date.today() - timedelta(days=10)).isoformat()
    _write_gate(ben_root, "age-stale-1", {"created": old_date})
    r = client.get("/gate/fixture/age-stale-1")
    assert r.status_code == 200, r.text
    assert "gate-chip--age-stale" in r.text


def test_gate_detail_age_chip_not_stale_within_threshold(client, ben_root):
    recent_date = (date.today() - timedelta(days=2)).isoformat()
    _write_gate(ben_root, "age-fresh-1", {"created": recent_date})
    r = client.get("/gate/fixture/age-fresh-1")
    assert r.status_code == 200, r.text
    assert "gate-chip--age-stale" not in r.text


# ── 8. Pending gates are listed oldest-first ──────────────────────────────────

def test_list_pending_gates_sorted_oldest_first(ben_root, monkeypatch):
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    _write_gate(ben_root, "order-newest", {"created": "2026-07-10"})
    _write_gate(ben_root, "order-oldest", {"created": "2026-06-01"})
    _write_gate(ben_root, "order-middle", {"created": "2026-06-20"})
    gates = list_pending_gates("fixture")
    ids_in_order = [g["id"] for g in gates if g["id"].startswith("order-")]
    assert ids_in_order == ["order-oldest", "order-middle", "order-newest"]


# ── 9. Malformed gate cards still get a date (mtime) so sort/render never crash ──

def test_malformed_gate_card_gets_fallback_date(ben_root, monkeypatch):
    import sys
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import list_pending_gates

    gates_dir = ben_root / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / "broken.yaml").write_text("instrument: TLT: 90d call\n", encoding="utf-8")
    gates = list_pending_gates("fixture")
    broken = next(g for g in gates if g.get("_malformed"))
    assert broken["_gate_date"] is not None  # mtime fallback, never crashes
