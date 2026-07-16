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

import re
from pathlib import Path

import pytest
import yaml

# Extracts the inner text of a specific gate-chip span, e.g.
# _CHIP_RE("type").search(html).group(1) == "publication sociale".
# Anchoring to the span itself (not `r.text` as a whole) is required because
# gate_card.html's pre-existing subtitle ALSO renders humanize_kind(gate.kind)
# — a bare substring check on the whole page would pass even if the CHIP
# itself were reverted to the raw, unhumanized gate.kind.
def _CHIP_RE(variant: str) -> re.Pattern[str]:
    return re.compile(
        r'class="gate-chip gate-chip--' + re.escape(variant) + r'"[^>]*>\s*(.*?)\s*</span>',
        re.DOTALL,
    )


def _chip_text(html: str, variant: str) -> str | None:
    m = _CHIP_RE(variant).search(html)
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
        """The type pill shows the human label (from the existing _HUMAN_KIND
        map), not the raw kind slug.

        Uses kind="prospect_dm" -> "DM à approuver" deliberately: humanized
        and raw forms are very different, AND gate_card.html's pre-existing
        subtitle line also calls humanize_kind(gate.kind), so a bare
        substring check on the whole page (`"DM à approuver" in r.text`)
        would pass even if the CHIP itself rendered the raw slug — the
        subtitle would satisfy it regardless. Assert on the chip's own
        extracted text, not the page, so a broken chip (e.g. `{{ gate.kind
        }}` instead of `{{ humanize_kind(gate.kind) }}`) actually fails
        this test."""
        _write_gate(fixture_repo, "chip-type-1", _base_gate(
            "chip-type-1", kind="prospect_dm"))
        r = client.get("/gate/fixture/chip-type-1")
        assert r.status_code == 200, r.text
        chip = _chip_text(r.text, "type")
        assert chip is not None, "gate-chip--type span not found"
        assert chip == "DM à approuver"
        assert chip != "prospect_dm"

    def test_type_chip_falls_back_gracefully_for_unmapped_kind(self, client, fixture_repo):
        """A kind with no _HUMAN_KIND entry (e.g. a content-type slug like
        investment_case/essay/newsletter that has no confirmed enum anywhere
        in this codebase — #433 scout check) must still render readable
        prose via humanize_kind's snake_case fallback, never a raw enum leak
        and never an invented label for an unconfirmed slug."""
        _write_gate(fixture_repo, "chip-type-2", _base_gate(
            "chip-type-2", kind="investment_case"))
        r = client.get("/gate/fixture/chip-type-2")
        assert r.status_code == 200, r.text
        chip = _chip_text(r.text, "type")
        assert chip == "investment case"
        assert chip != "investment_case"

    def test_channel_chip_renders_from_explicit_channel(self, client, fixture_repo):
        _write_gate(fixture_repo, "chip-chan-1", _base_gate(
            "chip-chan-1", kind="content_publish", channel="linkedin"))
        r = client.get("/gate/fixture/chip-chan-1")
        assert r.status_code == 200, r.text
        assert _chip_text(r.text, "channel") == "LINKEDIN"

    def test_channel_chip_infers_linkedin_for_maya_kinds(self, client, fixture_repo):
        """news_post/prospect_dm/followup_draft carry no explicit channel field
        but are always LinkedIn — same inference already used by the
        content-proposal block, now also driving the header chip."""
        _write_gate(fixture_repo, "chip-chan-2", _base_gate(
            "chip-chan-2", kind="prospect_dm"))
        r = client.get("/gate/fixture/chip-chan-2")
        assert r.status_code == 200, r.text
        assert _chip_text(r.text, "channel") == "LINKEDIN"

    def test_source_chip_renders_item_ref_basename(self, client, fixture_repo):
        """Source pill shows only the basename of approval_bridge.item_ref —
        the full repo-relative path is noise on a phone-sized card."""
        _write_gate(fixture_repo, "chip-src-1", _base_gate(
            "chip-src-1", kind="content_publish",
            approval_bridge={"item_ref":
                "drafts/2026-07-01/publish-linkedin-plaide-contre-sa-conviction.md"}))
        r = client.get("/gate/fixture/chip-src-1")
        assert r.status_code == 200, r.text
        assert _chip_text(r.text, "source") == \
            "📄 publish-linkedin-plaide-contre-sa-conviction.md"
        # full path not dumped into the visible pill text (only in the title attr)
        assert 'title="Source — drafts/2026-07-01/' in r.text

    def test_all_three_chips_render_together(self, client, fixture_repo):
        """The PANW-essay scenario from Jade's feedback: kind + channel + source
        all present, all three pills must render in one scannable row.

        Uses kind="prospect_dm" (humanized "DM à approuver" != raw), not
        "essay" — "essay" has no _HUMAN_KIND entry and its fallback form
        equals its raw form (no underscores to strip), so it can't
        distinguish a working chip from a reverted one. That distinguishing
        case is covered separately by
        test_type_chip_falls_back_gracefully_for_unmapped_kind."""
        _write_gate(fixture_repo, "chip-panw-1", _base_gate(
            "chip-panw-1", kind="prospect_dm", channel="substack",
            approval_bridge={"item_ref": "drafts/panw-essay-2026-06-30.md"}))
        r = client.get("/gate/fixture/chip-panw-1")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" in r.text
        type_chip = _chip_text(r.text, "type")
        channel_chip = _chip_text(r.text, "channel")
        source_chip = _chip_text(r.text, "source")
        assert type_chip == "DM à approuver" and type_chip != "prospect_dm"
        assert channel_chip == "SUBSTACK"
        assert source_chip == "📄 panw-essay-2026-06-30.md"

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

    def test_only_age_chip_when_nothing_else_to_show(self, client, fixture_repo):
        """A gate with no kind, no channel, no source still gets the identity
        chips row — but now only for the anteriority chip (#666: every
        pending gate always has SOME date, at minimum from the YAML file's
        own mtime, so the age chip is never absent). The type/channel/source/
        content-type chips remain individually optional and stay absent here."""
        doc = {
            "id": "chip-none-1",
            "source_layer": 2, "target_layer": 3, "risk_level": "low",
            "requires_human": True, "current_mode": "manual_required",
            "actions": ["approve", "reject", "modify", "defer"],
        }
        _write_gate(fixture_repo, "chip-none-1", doc)
        r = client.get("/gate/fixture/chip-none-1")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" in r.text
        assert "gate-chip--age" in r.text
        assert "gate-chip--type" not in r.text
        assert "gate-chip--channel" not in r.text
        assert "gate-chip--source" not in r.text

    def test_source_chip_absent_when_no_approval_bridge(self, client, fixture_repo):
        _write_gate(fixture_repo, "chip-nosrc-1", _base_gate(
            "chip-nosrc-1", kind="social_post"))
        r = client.get("/gate/fixture/chip-nosrc-1")
        assert r.status_code == 200, r.text
        assert "gate-chip--source" not in r.text

    def test_chips_render_in_batch_view_too(self, client, fixture_repo):
        """gate_batch.html must carry the same de-stacked header per card.

        Uses kind="prospect_dm" (humanized "DM à approuver" != raw slug),
        not "newsletter" — "newsletter" has no _HUMAN_KIND entry and its
        fallback equals its raw form, so a reverted chip
        (`{{ gate.kind }}` instead of `{{ humanize_kind(gate.kind) }}`)
        would be indistinguishable from a working one in that case."""
        _write_gate(fixture_repo, "chip-batch-1", _base_gate(
            "chip-batch-1", kind="prospect_dm", channel="substack",
            approval_bridge={"item_ref": "drafts/weekly-2026-07-01.md"}))
        r = client.get("/gate/fixture/kind/prospect_dm")
        assert r.status_code == 200, r.text
        assert "gate-identity-chips" in r.text
        type_chip = _chip_text(r.text, "type")
        channel_chip = _chip_text(r.text, "channel")
        source_chip = _chip_text(r.text, "source")
        assert type_chip == "DM à approuver" and type_chip != "prospect_dm"
        assert channel_chip == "SUBSTACK"
        assert source_chip == "📄 weekly-2026-07-01.md"


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
