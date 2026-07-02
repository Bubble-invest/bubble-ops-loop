"""
test_gate_card_identity_chips.py — type/channel/source header chip (#433).

Board issue #433 (Jade direct feedback, 2026-06-30): the gate card stacks
~7 metadata rows above the content, and the reviewer "cannot understand
properly what this essay is: is it an IC? a long article? a post? what's
the source?". This test covers the de-stacked header — a scannable row of
pills right after the <h1> surfacing:
  - type    : humanize_kind(gate.kind), e.g. "Investment Case" / "essai" /
              "publication sociale"
  - channel : gate.channel (or the linkedin inference already used by the
              content-proposal block)
  - source  : approval_bridge.item_ref, basename only

Also covers the companion Part 1 regression (no-attachment gate must not
error) with an extra malformed-shape case not in test_gate_attachments.py:
`attachments` present but as a dict instead of a list (the board comment's
hypothesis for what a "real" failing gate might have looked like).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


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
        "kind": "social_post",
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


# ── Header chip: type / channel / source ────────────────────────────────────

class TestIdentityChips:

    def test_type_chip_renders_humanized_kind(self, client, fixture_repo):
        """The type pill shows the human label, not the raw kind slug."""
        _write_gate(fixture_repo, "chip-type-1", _base_gate(
            "chip-type-1", kind="investment_case"))
        r = client.get("/gate/fixture/chip-type-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--type" in r.text
        assert "Investment Case" in r.text

    def test_channel_chip_renders_from_explicit_channel(self, client, fixture_repo):
        _write_gate(fixture_repo, "chip-chan-1", _base_gate(
            "chip-chan-1", kind="content_publish", channel="linkedin"))
        r = client.get("/gate/fixture/chip-chan-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--channel" in r.text
        assert "LINKEDIN" in r.text

    def test_channel_chip_infers_linkedin_for_maya_kinds(self, client, fixture_repo):
        """news_post/prospect_dm/followup_draft carry no explicit channel field
        but are always LinkedIn — same inference already used by the
        content-proposal block, now also driving the header chip."""
        _write_gate(fixture_repo, "chip-chan-2", _base_gate(
            "chip-chan-2", kind="prospect_dm"))
        r = client.get("/gate/fixture/chip-chan-2")
        assert r.status_code == 200, r.text
        assert "gate-chip--channel" in r.text
        assert "LINKEDIN" in r.text

    def test_source_chip_renders_item_ref_basename(self, client, fixture_repo):
        """Source pill shows only the basename of approval_bridge.item_ref —
        the full repo-relative path is noise on a phone-sized card."""
        _write_gate(fixture_repo, "chip-src-1", _base_gate(
            "chip-src-1", kind="essay",
            approval_bridge={"item_ref":
                "drafts/2026-07-01/publish-linkedin-plaide-contre-sa-conviction.md"}))
        r = client.get("/gate/fixture/chip-src-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--source" in r.text
        assert "publish-linkedin-plaide-contre-sa-conviction.md" in r.text
        # full path not dumped into the visible pill text (only in the title attr)
        assert 'title="Source — drafts/2026-07-01/' in r.text

    def test_all_three_chips_render_together(self, client, fixture_repo):
        """The PANW-essay scenario from Jade's feedback: kind + channel + source
        all present, all three pills must render in one scannable row."""
        _write_gate(fixture_repo, "chip-panw-1", _base_gate(
            "chip-panw-1", kind="essay", channel="substack",
            approval_bridge={"item_ref": "drafts/panw-essay-2026-06-30.md"}))
        r = client.get("/gate/fixture/chip-panw-1")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" in r.text
        assert "gate-chip--type" in r.text and "essai" in r.text
        assert "gate-chip--channel" in r.text and "SUBSTACK" in r.text
        assert "gate-chip--source" in r.text and "panw-essay-2026-06-30.md" in r.text

    def test_chips_row_precedes_stacked_metadata(self, client, fixture_repo):
        """The chip row must appear BEFORE the gate-meta stack in the HTML so
        it's the first thing read, not buried after 7 rows of metadata."""
        _write_gate(fixture_repo, "chip-order-1", _base_gate(
            "chip-order-1", kind="investment_case", channel="substack"))
        r = client.get("/gate/fixture/chip-order-1")
        assert r.status_code == 200, r.text
        chips_pos = r.text.find("gate-identity-chips")
        meta_pos = r.text.find('class="gate-meta"')
        assert chips_pos != -1 and meta_pos != -1
        assert chips_pos < meta_pos, \
            "identity chips must render before the stacked gate-meta block"

    def test_no_chips_container_when_nothing_to_show(self, client, fixture_repo):
        """A gate with no kind, no channel, no source must render no empty
        chip container (no layout shift from an empty box)."""
        doc = {
            "id": "chip-none-1",
            "source_layer": 2, "target_layer": 3, "risk_level": "low",
            "requires_human": True, "current_mode": "manual_required",
            "actions": ["approve", "reject", "modify", "defer"],
        }
        _write_gate(fixture_repo, "chip-none-1", doc)
        r = client.get("/gate/fixture/chip-none-1")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" not in r.text

    def test_source_chip_absent_when_no_approval_bridge(self, client, fixture_repo):
        _write_gate(fixture_repo, "chip-nosrc-1", _base_gate(
            "chip-nosrc-1", kind="social_post"))
        r = client.get("/gate/fixture/chip-nosrc-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--source" not in r.text

    def test_chips_render_in_batch_view_too(self, client, fixture_repo):
        """gate_batch.html must carry the same de-stacked header per card."""
        _write_gate(fixture_repo, "chip-batch-1", _base_gate(
            "chip-batch-1", kind="newsletter", channel="substack",
            approval_bridge={"item_ref": "drafts/weekly-2026-07-01.md"}))
        r = client.get("/gate/fixture/kind/newsletter")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" in r.text
        assert "newsletter" in r.text.lower()
        assert "SUBSTACK" in r.text
        assert "weekly-2026-07-01.md" in r.text


# ── Part 1 regression: no-attachment / malformed-attachment must never error ─

class TestNoAttachmentRegression:

    def test_no_attachments_field_renders_cleanly(self, client, fixture_repo):
        """Baseline: absent `attachments:` key must not error (guards
        gate_card.html:290's {% if gate.attachments %})."""
        _write_gate(fixture_repo, "noatt-1", _base_gate(
            "noatt-1", kind="content_publish", channel="linkedin"))
        r = client.get("/gate/fixture/noatt-1")
        assert r.status_code == 200, r.text
        assert "/gate/fixture/attachment?path=" not in r.text

    def test_malformed_dict_attachments_does_not_500(self, client, fixture_repo):
        """Edge case the board comment flagged as a possible real cause: an
        agent emits `attachments:` as a dict instead of a list. Jinja would
        iterate dict keys as bare strings — must degrade gracefully, never
        500."""
        _write_gate(fixture_repo, "noatt-2", _base_gate(
            "noatt-2", kind="content_publish",
            attachments={"path": "outputs/2026-07-01/attachments/x.png"}))
        r = client.get("/gate/fixture/noatt-2")
        assert r.status_code == 200, r.text

    def test_empty_attachments_list_renders_cleanly(self, client, fixture_repo):
        _write_gate(fixture_repo, "noatt-3", _base_gate(
            "noatt-3", kind="content_publish", attachments=[]))
        r = client.get("/gate/fixture/noatt-3")
        assert r.status_code == 200, r.text
        assert "/gate/fixture/attachment?path=" not in r.text
