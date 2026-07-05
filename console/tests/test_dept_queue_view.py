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
