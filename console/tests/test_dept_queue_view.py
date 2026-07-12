"""
test_dept_queue_view.py — card #376: per-layer waiting-items queue in dept left column.

Covers:
  - list_layer_queues() reader: items present, processed excluded, gate cross-cut
  - /dept/<slug>/inbox-fragment endpoint returns 200 with item titles
  - dept_detail initial render includes layer_queues data
  - pending_human items are visually flagged (badge class present)
"""
from __future__ import annotations

import yaml
from pathlib import Path


# ─── Unit tests for list_layer_queues() ──────────────────────────────────────


def _make_queue_repo(root: Path) -> Path:
    """Build a minimal bubble-ops-qtest dept repo with populated queues."""
    repo = root / "bubble-ops-qtest"
    repo.mkdir()

    # dept.yaml + STATE.yaml (Live)
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "qtest", "level": "ops", "mandate": "test"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "qtest", "display_name": "QTest",
            "owner": "operator", "created_at": "2026-01-01T00:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-06-01T00:00:00Z",
            "commits": [],
        }),
        encoding="utf-8",
    )

    # queues/research/ — L1 feed
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "research" / "ri-1.yaml").write_text(
        yaml.safe_dump({
            "id": "ri-1", "kind": "research_item",
            "question_text": "What is the alpha opportunity in NVDA?",
            "created_at": "2026-06-28T09:00:00Z",
        }),
        encoding="utf-8",
    )
    # .processed/ item must be EXCLUDED
    (repo / "queues" / "research" / ".processed").mkdir()
    (repo / "queues" / "research" / ".processed" / "old-ri.yaml").write_text(
        yaml.safe_dump({"id": "old-ri", "kind": "research_item",
                        "question_text": "This is old and must not appear."}),
        encoding="utf-8",
    )

    # inbox/decisions/ — L3 feed
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "inbox" / "decisions" / "dec-1.yaml").write_text(
        yaml.safe_dump({
            "id": "dec-1", "kind": "approved_action",
            "description": "Approved trade proposal for BTC-USD",
        }),
        encoding="utf-8",
    )

    # queues/gates/ — cross-cutting (pending_human)
    (repo / "queues" / "gates").mkdir(parents=True)
    (repo / "queues" / "gates" / "gate-1.yaml").write_text(
        yaml.safe_dump({
            "id": "gate-1", "kind": "social_post",
            "post_body": "Bubble Invest quarterly update — strong performance.",
            "requires_human": True, "current_mode": "manual_required",
        }),
        encoding="utf-8",
    )

    return repo


def test_list_layer_queues_basic(tmp_path, monkeypatch):
    """list_layer_queues returns items in correct layers and excludes .processed."""
    import os
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    _make_queue_repo(tmp_path)
    from console.services.github_reader import list_layer_queues

    result = list_layer_queues("qtest")

    # L1 has the research item + the gate (cross-cut)
    l1_ids = [i["id"] for i in result[1]]
    assert "ri-1" in l1_ids, "research item should appear in L1"
    assert "gate-1" in l1_ids, "gate should appear on all layers (L1)"
    assert "old-ri" not in l1_ids, ".processed items must be excluded"

    # L3 has the approved decision + the gate (cross-cut)
    l3_ids = [i["id"] for i in result[3]]
    assert "dec-1" in l3_ids, "inbox/decisions item should appear in L3"
    assert "gate-1" in l3_ids, "gate should appear on all layers (L3)"

    # Gate has pending_human=True; research item does not
    gate_item = next(i for i in result[1] if i["id"] == "gate-1")
    assert gate_item["pending_human"] is True, "gate item should have pending_human=True"

    ri_item = next(i for i in result[1] if i["id"] == "ri-1")
    assert ri_item["pending_human"] is False, "research item should have pending_human=False"


def test_gates_processed_subdir_excluded_from_pending_and_layer_queues(tmp_path, monkeypatch):
    """Board #442: an archived draft/gate card moved to queues/gates/.processed/
    must be excluded from BOTH list_pending_gates() (the "décisions à prendre"
    / gate-batch source) and list_layer_queues() (the per-dept left column) —
    not just from queues/research/.processed/ (already covered above).

    Repro shape mirrors the real board #442 cards exactly: kind=draft,
    created_by=materialize_due_missions, archived under queues/gates/.processed/.
    """
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)

    # Archive a phantom empty draft card into queues/gates/.processed/, exactly
    # as board #442 describes (archived + pushed, but reportedly still rendering).
    processed_dir = repo / "queues" / "gates" / ".processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    (processed_dir / "draft-linkedin_sage_batch-20260621-170320.yaml").write_text(
        yaml.safe_dump({
            "id": "draft-linkedin_sage_batch-20260621-170320",
            "mission_id": "linkedin_sage_batch",
            "kind": "draft",
            "created_at": "2026-06-21T17:03:20Z",
            "created_by": "materialize_due_missions",
        }),
        encoding="utf-8",
    )

    from console.services.github_reader import list_pending_gates, list_layer_queues

    pending = list_pending_gates("qtest")
    pending_ids = [g.get("id") for g in pending]
    assert "draft-linkedin_sage_batch-20260621-170320" not in pending_ids, (
        "a .processed gate card must not appear in list_pending_gates() "
        "('décisions à prendre' source)"
    )
    assert "gate-1" in pending_ids, "sanity: the still-live gate must still appear"

    result = list_layer_queues("qtest")
    for layer_items in result.values():
        layer_ids = [i["id"] for i in layer_items]
        assert "draft-linkedin_sage_batch-20260621-170320" not in layer_ids, (
            "a .processed gate card must not appear in list_layer_queues() on "
            "any layer (gates are cross-cut to all layers)"
        )


def test_list_layer_queues_title_derived(tmp_path, monkeypatch):
    """Titles are derived from question_text / post_body / description fields."""
    import os
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    _make_queue_repo(tmp_path)
    from console.services.github_reader import list_layer_queues

    result = list_layer_queues("qtest")
    ri = next(i for i in result[1] if i["id"] == "ri-1")
    assert "NVDA" in ri["title"], "title should contain an excerpt of question_text"
    assert len(ri["title"]) <= 65, "title should be truncated to ~60 chars + kind prefix"

    gate = next(i for i in result[1] if i["id"] == "gate-1")
    assert "Bubble" in gate["title"] or "social_post" in gate["title"]


def test_ideas_scout_titles_are_distinct(tmp_path, monkeypatch):
    """#391: two ideas_scout items (no kind/title, schema source+ticker_or_theme)
    must render DISTINCT titles, not both collapse to the bare 'ideas_scout'."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    # Two scout items with the ideas_scout schema (no `kind`, no `title`).
    (repo / "queues" / "research" / "scout-lseg.yaml").write_text(
        yaml.safe_dump({"source": "ideas_scout", "ticker_or_theme": "LSEG",
                        "reason": "AI-data toll-road de-rate.", "priority": "high"}),
        encoding="utf-8",
    )
    (repo / "queues" / "research" / "scout-tdg.yaml").write_text(
        yaml.safe_dump({"source": "ideas_scout", "ticker_or_theme": "TDG",
                        "reason": "Aerospace aftermarket duopoly.", "priority": "high"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    titles = [i["title"] for i in list_layer_queues("qtest")[1]]

    assert "ideas_scout: LSEG" in titles, f"expected distinct LSEG title, got {titles}"
    assert "ideas_scout: TDG" in titles, f"expected distinct TDG title, got {titles}"
    # And crucially they are NOT identical bare 'ideas_scout' duplicates.
    assert titles.count("ideas_scout") == 0, "scout items must not collapse to bare label"


def test_stale_terminal_item_filtered(tmp_path, monkeypatch):
    """#391: an already-acted record (executed_trade) older than the stale window
    must NOT show as pending; a fresh one (today) still shows."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "management").mkdir(parents=True, exist_ok=True)
    (repo / "queues" / "management" / "old-trade.yaml").write_text(
        yaml.safe_dump({"id": "old-trade", "kind": "executed_trade",
                        "created_at": "2026-06-20T14:00:00Z",
                        "summary": "bought 10 ACWI"}),
        encoding="utf-8",
    )
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    (repo / "queues" / "management" / "fresh-note.yaml").write_text(
        yaml.safe_dump({"id": "fresh-note", "kind": "management_note",
                        "created_at": fresh, "summary": "rebalance check today"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    l2_ids = [i["id"] for i in list_layer_queues("qtest")[2]]

    assert "old-trade" not in l2_ids, "stale executed_trade must be filtered from pending"
    assert "fresh-note" in l2_ids, "a fresh non-terminal item must still show"


def test_fresh_terminal_item_not_filtered(tmp_path, monkeypatch):
    """A terminal-kind item created TODAY must still show (only OLD ones drop)."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "management").mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    (repo / "queues" / "management" / "today-trade.yaml").write_text(
        yaml.safe_dump({"id": "today-trade", "kind": "executed_trade",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "summary": "executed today"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    l2_ids = [i["id"] for i in list_layer_queues("qtest")[2]]
    assert "today-trade" in l2_ids, "a same-day terminal item must NOT be filtered"


def test_stale_content_kind_filtered_by_age():
    """#547 fix 3a: an idea_item older than the content stale window (7d)
    is stale; content kinds get a LONGER window than the trade/wrapup
    terminal kinds (1d) since research ideas legitimately sit pending
    longer. Unit-tested directly on the predicate (display-grouping
    collapses same-kind items, which would obscure per-item assertions at
    the list_layer_queues() level — see test_latest_snapshot_* and the
    grouping tests below for the integration-level coverage)."""
    from console.services.github_reader import _is_stale_terminal_item
    from datetime import datetime, timezone

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    assert _is_stale_terminal_item("idea_item", "2026-01-01T09:00:00Z", now=now) is True
    assert _is_stale_terminal_item(
        "idea_item", datetime.now(timezone.utc).isoformat(), now=now
    ) is False


def test_content_kind_within_window_not_filtered():
    """An idea_item just 2 days old must NOT be filtered (well within the
    7-day content stale window) — only genuinely old items age out."""
    from console.services.github_reader import _is_stale_terminal_item
    from datetime import datetime, timezone, timedelta

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    two_days_ago = (now - timedelta(days=2)).isoformat()
    assert _is_stale_terminal_item("idea_item", two_days_ago, now=now) is False
    # Sanity: past the 7-day window, same kind, it flips to stale.
    eight_days_ago = (now - timedelta(days=8)).isoformat()
    assert _is_stale_terminal_item("idea_item", eight_days_ago, now=now) is True


def test_stale_content_kind_unparseable_date_fails_open():
    """An idea_item with a malformed created_at must be KEPT (fail-open),
    never hidden — mirrors the existing terminal-kind contract."""
    from console.services.github_reader import _is_stale_terminal_item
    assert _is_stale_terminal_item("idea_item", "not-a-real-date") is False
    assert _is_stale_terminal_item("idea_item", "") is False


def test_stale_content_kind_future_date_not_filtered():
    """An item dated in the future must never be treated as stale (negative
    age) — sanity/edge-case for the reviewer's adversarial pass."""
    from console.services.github_reader import _is_stale_terminal_item
    from datetime import datetime, timezone, timedelta

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    future = (now + timedelta(days=30)).isoformat()
    assert _is_stale_terminal_item("idea_item", future, now=now) is False


def test_stale_content_kind_integration_via_list_layer_queues(tmp_path, monkeypatch):
    """Integration-level check that the age filter actually reaches
    list_layer_queues(): with only ONE old idea_item present (no grouping
    ambiguity — a single stale item), the layer's item count for research/
    drops to just the always-present base-fixture items."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    baseline_count = len(
        __import__("console.services.github_reader", fromlist=["list_layer_queues"])
        .list_layer_queues("qtest")[1]
    )
    (repo / "queues" / "research" / "old-idea-solo.yaml").write_text(
        yaml.safe_dump({"id": "old-idea-solo", "kind": "idea_item",
                        "created_at": "2026-01-01T09:00:00Z",
                        "title": "An old idea nobody triaged"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    after_count = len(list_layer_queues("qtest")[1])
    assert after_count == baseline_count, (
        "a stale idea_item must be filtered before it ever reaches the "
        "grouping pass — item count must not grow"
    )


def test_other_content_stale_kinds_also_filtered(tmp_path, monkeypatch):
    """narrative_script / pillar_review / voice_challenge all use the same
    content stale window, not just idea_item."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    for kind in ("narrative_script", "pillar_review", "voice_challenge"):
        (repo / "queues" / "research" / f"old-{kind}.yaml").write_text(
            yaml.safe_dump({"id": f"old-{kind}", "kind": kind,
                            "created_at": "2026-01-01T09:00:00Z",
                            "title": f"old {kind}"}),
            encoding="utf-8",
        )
    from console.services.github_reader import list_layer_queues
    l1_titles = [i["title"] for i in list_layer_queues("qtest")[1]]
    for kind in ("narrative_script", "pillar_review", "voice_challenge"):
        assert not any(f"old {kind}" in t.lower() for t in l1_titles), (
            f"old {kind} must be filtered as stale, got {l1_titles}"
        )


# ── *-latest.yaml snapshot exclusion (#547 fix 3b) ────────────────────────────


def test_latest_snapshot_files_excluded_from_pending_queue(tmp_path, monkeypatch):
    """`*-latest.yaml` files (e.g. external-signal-latest.yaml,
    linkedin-sage-scan-latest.yaml) are STATE snapshots a scan mission
    overwrites each run, not pending to-dos — must be excluded entirely
    from list_layer_queues(), regardless of their created_at/kind."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "external-signal-latest.yaml").write_text(
        yaml.safe_dump({"id": "external-signal-latest", "kind": "scan_state",
                        "created_at": "2026-06-28T09:00:00Z",
                        "summary": "latest scan snapshot"}),
        encoding="utf-8",
    )
    (repo / "queues" / "research" / "linkedin-sage-scan-latest.yaml").write_text(
        yaml.safe_dump({"id": "linkedin-sage-scan-latest", "kind": "scan_state",
                        "created_at": "2026-06-28T09:00:00Z"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    l1_ids = [i["id"] for i in list_layer_queues("qtest")[1]]
    assert "external-signal-latest" not in l1_ids, "-latest.yaml snapshot must be excluded"
    assert "linkedin-sage-scan-latest" not in l1_ids, "-latest.yaml snapshot must be excluded"
    # Sanity: the normal research item still shows.
    assert "ri-1" in l1_ids


def test_latest_snapshot_excluded_from_gates_dir_too(tmp_path, monkeypatch):
    """A real -latest.yaml pattern is unlikely in queues/gates/, but the
    exclusion is a generic filename rule applied at the same glob site as
    every non-gate queue dir — verify a non-gate dir (management-like)
    beyond research/ is also covered so the fix isn't accidentally scoped
    to one directory."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "inbox" / "decisions" / "some-thing-latest.yaml").write_text(
        yaml.safe_dump({"id": "some-thing-latest", "kind": "approved_action",
                        "description": "should not show"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    l3_ids = [i["id"] for i in list_layer_queues("qtest")[3]]
    assert "some-thing-latest" not in l3_ids


def test_list_layer_queues_empty_for_unknown_slug(tmp_path, monkeypatch):
    """list_layer_queues returns empty dicts for a slug with no repo."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from console.services.github_reader import list_layer_queues
    result = list_layer_queues("nonexistent-dept-xyz")
    assert result == {1: [], 2: [], 3: [], 4: []}


def test_list_layer_queues_parses_gates_dir_once(tmp_path, monkeypatch):
    """queues/gates/ used to be globbed+parsed twice per request
    (list_pending_gates internally, then again in list_layer_queues' own
    loop) — board #450. Spy on Path.read_text to confirm gate-1.yaml is
    read exactly once."""
    import sys
    from pathlib import Path as _Path

    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    _make_queue_repo(tmp_path)
    from console.services.github_reader import list_layer_queues

    reads = []
    orig_read_text = _Path.read_text

    def spy_read_text(self, *a, **k):
        if self.name == "gate-1.yaml":
            reads.append(self)
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(_Path, "read_text", spy_read_text)
    result = list_layer_queues("qtest")

    assert any(i["id"] == "gate-1" for i in result[1]), "sanity: gate still present"
    assert len(reads) == 1, f"expected gate-1.yaml to be read exactly once, got {len(reads)} reads"


def test_list_layer_queues_malformed_item_logs_warning(tmp_path, monkeypatch, caplog):
    """A malformed YAML in a non-gate queue dir used to be swallowed at
    debug level (invisible by default) — board #450 raises it to warning."""
    import logging
    import sys

    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "broken.yaml").write_text(
        "id: broken\nquestion_text: [unterminated\n", encoding="utf-8"
    )

    from console.services.github_reader import list_layer_queues

    with caplog.at_level(logging.WARNING, logger="console.github_reader"):
        result = list_layer_queues("qtest")

    l1_ids = [i["id"] for i in result[1]]
    assert "broken" not in l1_ids, "malformed item must still be excluded from results"
    assert any(
        "broken.yaml" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    ), f"expected a WARNING log naming broken.yaml, got: {[r.message for r in caplog.records]}"


# ─── Queue-item grouping by kind + channel (#547 fix 2, mirrors #459's
#     _mgmt_queue_items grouping pattern for style consistency) ──────────────


def test_queue_items_grouped_by_kind_and_channel(tmp_path, monkeypatch):
    """3 idea_item/linkedin items in the same layer collapse into ONE grouped
    row 'idea_item · linkedin ×3', not three separate flat rows."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    for i in range(3):
        (repo / "queues" / "research" / f"grp-idea-{i}.yaml").write_text(
            yaml.safe_dump({
                "id": f"grp-idea-{i}", "kind": "idea_item", "channel": "linkedin",
                "created_at": fresh,
                "title": f"idea number {i}",
            }),
            encoding="utf-8",
        )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    grouped = [t for t in titles if "idea_item" in t and "linkedin" in t and "×3" in t]
    assert grouped, f"expected a grouped 'idea_item · linkedin ×3' row, got titles={titles}"
    # The 3 individual items must NOT also appear as separate ungrouped rows.
    assert titles.count("idea number 0") == 0


def test_queue_items_different_channel_not_grouped_together(tmp_path, monkeypatch):
    """idea_item/linkedin and idea_item/substack_note are DIFFERENT groups —
    channel is part of the group key, not just kind."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "li-1.yaml").write_text(
        yaml.safe_dump({"id": "li-1", "kind": "idea_item", "channel": "linkedin",
                        "created_at": fresh, "title": "a"}),
        encoding="utf-8",
    )
    (repo / "queues" / "research" / "li-2.yaml").write_text(
        yaml.safe_dump({"id": "li-2", "kind": "idea_item", "channel": "linkedin",
                        "created_at": fresh, "title": "b"}),
        encoding="utf-8",
    )
    (repo / "queues" / "research" / "sn-1.yaml").write_text(
        yaml.safe_dump({"id": "sn-1", "kind": "idea_item", "channel": "substack_note",
                        "created_at": fresh, "title": "c"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert any("linkedin" in t and "×2" in t for t in titles), titles
    assert any("substack_note" in t for t in titles), titles
    # substack_note group must not carry ×2 (only 1 item)
    substack_titles = [t for t in titles if "substack_note" in t]
    assert not any("×2" in t for t in substack_titles), titles


def test_queue_item_mission_name_shown_when_source_parses(tmp_path, monkeypatch):
    """When an item's `source:` field cleanly names a mission (regex
    narrative_script-<mission_id> or missions/<id>/), the mission name is
    shown instead of a bare kind+channel group."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "ns-1.yaml").write_text(
        yaml.safe_dump({
            "id": "ns-1", "kind": "narrative_script", "channel": "linkedin",
            "source": "narrative_script-weekly_thesis_writeup",
            "created_at": fresh, "title": "script body",
        }),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert any("weekly_thesis_writeup" in t for t in titles), (
        f"expected the parsed mission name in a title, got {titles}"
    )


def test_queue_item_mission_name_parses_missions_slash_id_form(tmp_path, monkeypatch):
    """The alternate source shape `missions/<id>/...` also resolves a
    mission name."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "pr-1.yaml").write_text(
        yaml.safe_dump({
            "id": "pr-1", "kind": "pillar_review", "channel": "substack_note",
            "source": "missions/quarterly_pillar_audit/output.yaml",
            "created_at": fresh, "title": "review body",
        }),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert any("quarterly_pillar_audit" in t for t in titles), (
        f"expected the parsed mission name, got {titles}"
    )


def test_queue_item_no_clean_mission_falls_back_to_kind_channel_group(tmp_path, monkeypatch):
    """When `source` doesn't cleanly name a mission (or is absent), items
    group under kind+channel — never crash, never show a garbled label."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "vc-1.yaml").write_text(
        yaml.safe_dump({
            "id": "vc-1", "kind": "voice_challenge", "channel": "linkedin",
            "source": "some_unrelated_free_text_no_pattern",
            "created_at": fresh, "title": "challenge body",
        }),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert any("voice_challenge" in t and "linkedin" in t for t in titles), titles


def test_queue_item_grouping_no_channel_field_groups_by_kind_only(tmp_path, monkeypatch):
    """Items with no `channel` field (the ~1/3 that don't carry one) still
    group cleanly by kind alone — must not crash on a missing key."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    for i in range(2):
        (repo / "queues" / "research" / f"nochan-{i}.yaml").write_text(
            yaml.safe_dump({
                "id": f"nochan-{i}", "kind": "idea_item",
                "created_at": fresh, "title": f"no channel {i}",
            }),
            encoding="utf-8",
        )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert any("idea_item" in t and "×2" in t for t in titles), titles


def test_queue_item_grouping_missing_kind_does_not_crash(tmp_path, monkeypatch):
    """An item entirely missing `kind` must not crash the grouping pass —
    falls back to whatever the existing title-derivation already produces,
    grouped under an empty/'item' kind bucket."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "nokind-1.yaml").write_text(
        yaml.safe_dump({"id": "nokind-1", "created_at": "2026-06-28T09:00:00Z",
                        "title": "mystery item"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    # Must not raise.
    items = list_layer_queues("qtest")[1]
    assert isinstance(items, list)


def test_single_ungrouped_item_still_renders_normally(tmp_path, monkeypatch):
    """A single item (group size 1) should not show a '×1' — sanity so the
    grouping doesn't uglify the common single-item case."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "solo-idea.yaml").write_text(
        yaml.safe_dump({"id": "solo-idea", "kind": "idea_item", "channel": "linkedin",
                        "created_at": fresh, "title": "solo"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert not any("×1" in t for t in titles), f"a single item must not show ×1: {titles}"


def test_gate_items_not_grouped_pending_human_preserved(tmp_path, monkeypatch):
    """Gate queue items (queues/gates/) are cross-cutting decision cards, not
    content research items — they must keep their existing ungrouped
    rendering + pending_human flag untouched by the new grouping."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    _make_queue_repo(tmp_path)
    from console.services.github_reader import list_layer_queues
    result = list_layer_queues("qtest")
    gate_item = next(i for i in result[1] if i["id"] == "gate-1")
    assert gate_item["pending_human"] is True


def test_grouping_does_not_break_existing_stale_and_processed_filters(tmp_path, monkeypatch):
    """Regression: the new grouping pass must run AFTER stale/.processed
    filtering, not resurrect filtered-out items into a group."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _make_queue_repo(tmp_path)
    (repo / "queues" / "research" / "old-idea-a.yaml").write_text(
        yaml.safe_dump({"id": "old-idea-a", "kind": "idea_item", "channel": "linkedin",
                        "created_at": "2026-01-01T09:00:00Z", "title": "old a"}),
        encoding="utf-8",
    )
    (repo / "queues" / "research" / "old-idea-b.yaml").write_text(
        yaml.safe_dump({"id": "old-idea-b", "kind": "idea_item", "channel": "linkedin",
                        "created_at": "2026-01-02T09:00:00Z", "title": "old b"}),
        encoding="utf-8",
    )
    from console.services.github_reader import list_layer_queues
    items = list_layer_queues("qtest")[1]
    titles = [i["title"] for i in items]
    assert not any("idea_item" in t and "linkedin" in t for t in titles), (
        f"both idea_items are stale (>7d) and must be filtered out before "
        f"grouping — got {titles}"
    )


# ─── Integration tests for the HTTP endpoints ────────────────────────────────


def _build_app_with_queues(tmp_path: Path, monkeypatch):
    """Return a FastAPI TestClient with a Live dept having queue items."""
    import os
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    _make_queue_repo(tmp_path)

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from console.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_inbox_fragment_endpoint_returns_200(tmp_path, monkeypatch):
    """/dept/<slug>/inbox-fragment must return 200 for a Live dept."""
    c = _build_app_with_queues(tmp_path, monkeypatch)
    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"


def test_inbox_fragment_shows_queue_titles(tmp_path, monkeypatch):
    """The inbox fragment must include the queue item titles."""
    c = _build_app_with_queues(tmp_path, monkeypatch)
    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200
    body = r.text
    # The research item title (contains 'NVDA')
    assert "NVDA" in body, "research item excerpt should be visible in inbox fragment"
    # The gate item (contains 'Bubble' or the kind prefix)
    assert "gate-1" in body or "social_post" in body or "Bubble" in body, (
        "gate item should be visible in inbox fragment"
    )


def test_inbox_fragment_flags_pending_human(tmp_path, monkeypatch):
    """Items needing human approval must use the human-flagging CSS class."""
    c = _build_app_with_queues(tmp_path, monkeypatch)
    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200
    body = r.text
    # The --human modifier class must appear for the gate item
    assert "desk-queue-item--human" in body, (
        "gate items (pending_human=True) must have the desk-queue-item--human class"
    )


def test_inbox_fragment_excludes_processed_items(tmp_path, monkeypatch):
    """Items in .processed/ must NOT appear in the inbox fragment."""
    c = _build_app_with_queues(tmp_path, monkeypatch)
    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200
    body = r.text
    assert "old-ri" not in body, ".processed items must be excluded from the inbox view"
    assert "This is old" not in body


def test_dept_detail_includes_htmx_inbox_refresh(tmp_path, monkeypatch):
    """The main dept page must wire the inbox div with htmx every-5s refresh."""
    c = _build_app_with_queues(tmp_path, monkeypatch)
    r = c.get("/dept/qtest")
    assert r.status_code == 200
    body = r.text
    # The htmx trigger on the inbox content div
    assert "inbox-fragment" in body, (
        "dept detail page must reference /dept/<slug>/inbox-fragment for htmx refresh"
    )
    assert "every 5s" in body or "every 5" in body, (
        "inbox div must have an htmx every-5s trigger"
    )
    assert "dept-inbox-content" in body, (
        "inbox content div id must be present for htmx targeting"
    )


def test_inbox_fragment_caps_queue_and_collapses_overflow(tmp_path, monkeypatch):
    """#534 (épuré): the left column must NOT dump dozens of raw queue ids.
    Beyond QUEUE_CAP (6) items, the overflow tucks into a single '+N de plus'
    collapse so the at-a-glance stays a screenful. Recurring missions collapse
    behind a per-layer count too."""
    import os
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    repo = _make_queue_repo(tmp_path)

    # Seed 15 research items into L1 → well past the cap of 6.
    for i in range(15):
        (repo / "queues" / "research" / f"ri-extra-{i}.yaml").write_text(
            yaml.safe_dump({
                "id": f"ri-extra-{i}", "kind": "research_item",
                "question_text": f"Extra research question number {i}?",
                "created_at": "2026-06-28T09:00:00Z",
            }),
            encoding="utf-8",
        )

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200
    body = r.text
    # The overflow collapse must be present with a "+N de plus" summary.
    assert "desk-queue-overflow" in body, (
        "queue overflow past the cap must collapse behind a <details>, not dump"
    )
    assert "de plus" in body, "overflow summary must read '+N de plus'"


def test_inbox_fragment_renders_grouped_content_items_with_css_hook(tmp_path, monkeypatch):
    """#547 fix 2 — end-to-end: 3 idea_item/linkedin items render as one
    'idea_item · linkedin ×3' row carrying the desk-queue-item--grouped
    styling hook, not three flat rows."""
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    repo = _make_queue_repo(tmp_path)

    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        (repo / "queues" / "research" / f"grp-render-{i}.yaml").write_text(
            yaml.safe_dump({
                "id": f"grp-render-{i}", "kind": "idea_item", "channel": "linkedin",
                "created_at": fresh, "title": f"idea render {i}",
            }),
            encoding="utf-8",
        )

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/dept/qtest/inbox-fragment")
    assert r.status_code == 200
    body = r.text
    assert "idea_item · linkedin ×3" in body, (
        f"expected the grouped label in the rendered fragment, body snippet missing it"
    )
    assert "desk-queue-item--grouped" in body, (
        "grouped rows must carry the desk-queue-item--grouped styling hook"
    )
    # The 3 individual item titles must not appear as separate flat rows.
    assert "idea render 0" not in body
