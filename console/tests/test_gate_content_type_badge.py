"""
test_gate_content_type_badge.py — Substack content_type badge (#475).

Board card #475 asked for 3 items on the gate render for Substack content:
  1. content_type badge (note / essay_free / investment_case_paid)  -- NEW
  2. full draft_body render                                         -- already shipped
  3. attachments image preview                                      -- already shipped

This file covers item 1 only. The badge renders in the shared
components/gate-identity-chips.html partial (included by both
gate_card.html and gate_batch.html), so one partial fix covers both
surfaces — asserted separately below for each route.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Extracts the inner text of the content-type chip span. The class name
# includes the raw content_type slug (gate-chip--content-note, etc.), so
# anchor on the class prefix rather than a fixed suffix.
def _CONTENT_CHIP_RE(content_type: str) -> re.Pattern[str]:
    return re.compile(
        r'class="gate-chip gate-chip--content-' + re.escape(content_type) + r'"[^>]*>\s*(.*?)\s*</span>',
        re.DOTALL,
    )


def _content_chip_text(html: str, content_type: str) -> str | None:
    m = _CONTENT_CHIP_RE(content_type).search(html)
    return m.group(1).strip() if m else None


def _write_gate(repo: Path, gate_id: str, fields: dict) -> Path:
    gates_dir = repo / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    p = gates_dir / f"{gate_id}.yaml"
    p.write_text(yaml.safe_dump(fields, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p


def _base_gate(gate_id: str, **extra) -> dict:
    doc = {
        "id": gate_id,
        "kind": "content_publish",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def fixture_repo(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


class TestContentTypeBadgeSingleGateView:

    def test_note_badge_renders_blue_label(self, client, fixture_repo):
        _write_gate(fixture_repo, "ctype-note-1", _base_gate(
            "ctype-note-1", content_type="note"))
        r = client.get("/gate/fixture/ctype-note-1")
        assert r.status_code == 200, r.text
        assert _content_chip_text(r.text, "note") == "Note"

    def test_essay_free_badge_renders_gray_label(self, client, fixture_repo):
        _write_gate(fixture_repo, "ctype-essay-1", _base_gate(
            "ctype-essay-1", content_type="essay_free"))
        r = client.get("/gate/fixture/ctype-essay-1")
        assert r.status_code == 200, r.text
        assert _content_chip_text(r.text, "essay_free") == "Essai"

    def test_investment_case_paid_badge_renders_gold_label(self, client, fixture_repo):
        _write_gate(fixture_repo, "ctype-ic-1", _base_gate(
            "ctype-ic-1", content_type="investment_case_paid", paid=True))
        r = client.get("/gate/fixture/ctype-ic-1")
        assert r.status_code == 200, r.text
        assert _content_chip_text(r.text, "investment_case_paid") == "Investment Case"

    def test_absent_content_type_renders_nothing(self, client, fixture_repo):
        """Most gates are not Substack content — no content_type field must
        render no badge and must not crash the page."""
        _write_gate(fixture_repo, "ctype-none-1", _base_gate("ctype-none-1"))
        r = client.get("/gate/fixture/ctype-none-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--content-" not in r.text

    def test_unknown_content_type_value_renders_nothing(self, client, fixture_repo):
        """An unmapped content_type value must degrade gracefully (no badge,
        no crash) rather than render a raw/invented label."""
        _write_gate(fixture_repo, "ctype-unknown-1", _base_gate(
            "ctype-unknown-1", content_type="some_future_type"))
        r = client.get("/gate/fixture/ctype-unknown-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--content-" not in r.text


class TestContentTypeBadgeBatchView:

    def test_badge_renders_in_batch_view(self, client, fixture_repo):
        _write_gate(fixture_repo, "ctype-batch-1", _base_gate(
            "ctype-batch-1", kind="content_publish", content_type="essay_free"))
        r = client.get("/gate/fixture/kind/content_publish")
        assert r.status_code == 200, r.text
        assert _content_chip_text(r.text, "essay_free") == "Essai"

    def test_batch_view_no_crash_when_content_type_absent(self, client, fixture_repo):
        _write_gate(fixture_repo, "ctype-batch-2", _base_gate(
            "ctype-batch-2", kind="content_publish"))
        r = client.get("/gate/fixture/kind/content_publish")
        assert r.status_code == 200, r.text
