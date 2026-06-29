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
