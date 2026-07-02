"""
test_mgmt_note_state.py — card #459: dept inbox respects .consumed.json /
.last-mgmt-scan for queues/management/*.yaml notes.

Root cause under test: console.services.github_reader.list_layer_queues()
used to list EVERY queues/management/*.yaml file as pending, ignoring the
mgmt-note protocol's consumption state (which lives in `.consumed.json` +
`.last-mgmt-scan`, never in the note files themselves — see
scripts/lib/dispatch_helpers.py). Live evidence: Tony's page showed 30
identical "morning_brief: morning_brief" rows, all consumed weeks ago.

Covers:
  - scan_mgmt_inbox() unit tests: consumed-first rule (#198), fail-open
    created_at handling, dotfile/.processed exclusion, mission_id grouping.
  - list_layer_queues() integration: L2 items come from the consumption-aware
    path, with a trailing "_mgmt_consumed_summary" synthetic item.
  - Fixture from the card's own evaluation bar: 30 consumed + 2 pending →
    2 pending rows + 1 collapsed "N notes traitées" line.
  - Inbox-fragment HTTP rendering of the collapsed line + grouped label.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


def _mgmt_dir(repo: Path) -> Path:
    d = repo / "queues" / "management"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_note(d: Path, name: str, **fields) -> None:
    (d / f"{name}.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")


def _minimal_dept_repo(root: Path, slug: str = "qtest459") -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "management", "mandate": "test"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": "QTest459",
            "owner": "operator", "created_at": "2026-01-01T00:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-06-01T00:00:00Z",
            "commits": [],
        }),
        encoding="utf-8",
    )
    return repo


# ─── Unit tests: scan_mgmt_inbox() ───────────────────────────────────────────


def test_consumed_note_excluded_regardless_of_timestamp():
    """#198 rule: an id present in .consumed.json is never pending, even with
    a fresh created_at (bad/missing created_at on a consumed note must not
    resurrect it)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".consumed.json").write_text(json.dumps({"morning_brief": {}}), encoding="utf-8")
        (mgmt / ".last-mgmt-scan").write_text("2026-01-01T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "morning_brief-1", id="morning_brief",
                    mission_id="morning_brief", kind="management_note",
                    created_at=datetime.now(timezone.utc).isoformat())

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert state.pending_rows == []
        assert state.consumed_count == 1


def test_pending_note_after_watermark():
    """A note whose id is NOT consumed and whose created_at is after the
    watermark is genuinely pending."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text("2026-01-01T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "fresh", id="fresh-note", mission_id="rebalance_check",
                    kind="management_note", created_at="2026-06-15T09:00:00+00:00",
                    title="Rebalance check")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        ids = [i.id for row in state.pending_rows for i in row.items]
        assert "fresh-note" in ids
        assert state.consumed_count == 0


def test_note_before_watermark_and_not_consumed_is_hidden():
    """A note NOT in .consumed.json but with created_at <= watermark is
    treated as already-seen (hidden, counted in consumed_count) — the
    watermark alone is sufficient, matching _scan_mgmt_notes semantics."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text("2026-06-20T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "old", id="old-note", mission_id="morning_brief",
                    kind="management_note", created_at="2026-06-10T09:00:00+00:00")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert state.pending_rows == []
        assert state.consumed_count == 1


def test_missing_created_at_fails_open_to_pending():
    """A note with no created_at and not consumed must fail-open (pending),
    mirroring _scan_mgmt_notes's fail-open rule — never silently swallow
    potentially-real work."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text("2026-06-20T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "no-ts", id="no-ts-note", mission_id="strategic_question",
                    kind="management_note", title="Needs an answer")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        ids = [i.id for row in state.pending_rows for i in row.items]
        assert "no-ts-note" in ids


def test_unparseable_created_at_fails_open_to_pending():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text("2026-06-20T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "bad-ts", id="bad-ts-note", mission_id="morning_brief",
                    kind="management_note", created_at="not-a-date")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        ids = [i.id for row in state.pending_rows for i in row.items]
        assert "bad-ts-note" in ids


def test_no_watermark_means_all_unconsumed_are_pending():
    """Watermark absent (.last-mgmt-scan never written) → any non-consumed
    note is pending, regardless of created_at."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        _write_note(mgmt, "old", id="old-note", mission_id="morning_brief",
                    kind="management_note", created_at="2020-01-01T00:00:00+00:00")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        ids = [i.id for row in state.pending_rows for i in row.items]
        assert "old-note" in ids


def test_dotfiles_and_processed_subdir_excluded():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".consumed.json").write_text("{}", encoding="utf-8")
        (mgmt / ".gitkeep").write_text("", encoding="utf-8")
        (mgmt / ".processed").mkdir()
        _write_note(mgmt / ".processed", "old", id="processed-note",
                    mission_id="morning_brief", kind="management_note",
                    created_at="2026-06-01T00:00:00+00:00")
        _write_note(mgmt, "fresh", id="fresh-note", mission_id="morning_brief",
                    kind="management_note",
                    created_at=datetime.now(timezone.utc).isoformat())

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        ids = [i.id for row in state.pending_rows for i in row.items]
        assert "processed-note" not in ids
        assert "fresh-note" in ids


def test_no_mgmt_dir_returns_empty_state():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)  # no queues/management/ created
        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert state.pending_rows == []
        assert state.consumed_count == 0


def test_grouping_by_mission_id_when_multiple_pending():
    """>1 genuinely-pending notes sharing a mission_id collapse into one row
    '<mission_id> ×N (latest created_at)'."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text("2020-01-01T00:00:00+00:00", encoding="utf-8")
        _write_note(mgmt, "a", id="a", mission_id="rebalance_check",
                    kind="management_note", created_at="2026-06-10T09:00:00+00:00")
        _write_note(mgmt, "b", id="b", mission_id="rebalance_check",
                    kind="management_note", created_at="2026-06-15T09:00:00+00:00")

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert len(state.pending_rows) == 1
        row = state.pending_rows[0]
        assert row.mission_id == "rebalance_check"
        assert row.count == 2
        assert "×2" in row.label
        assert "2026-06-15T09:00:00+00:00" in row.label


def test_single_pending_note_not_grouped_label():
    """A single pending note keeps its own title, no '×1' grouping noise."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        _write_note(mgmt, "solo", id="solo", mission_id="strategic_question",
                    kind="management_note", title="Should we widen access?",
                    created_at=datetime.now(timezone.utc).isoformat())

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert len(state.pending_rows) == 1
        assert "×" not in state.pending_rows[0].label


# ─── The card's own evaluation fixture: 30 consumed + 2 pending ────────────


def _build_30_consumed_2_pending(root: Path) -> Path:
    """Fixture matching the card's evaluation bar exactly: 30 identical
    'morning_brief' notes consumed weeks ago (in .consumed.json, timestamps
    <= watermark) + 2 genuinely-pending notes (different mission_ids, not
    consumed, created_at > watermark)."""
    mgmt = _mgmt_dir(root)
    watermark = datetime(2026, 6, 25, 8, 0, 0, tzinfo=timezone.utc)
    (mgmt / ".last-mgmt-scan").write_text(watermark.isoformat(), encoding="utf-8")

    consumed_ids = []
    base = datetime(2026, 6, 2, 7, 0, 0, tzinfo=timezone.utc)
    for i in range(30):
        note_id = f"morning_brief-{i:02d}"
        consumed_ids.append(note_id)
        _write_note(
            mgmt, f"morning_brief-{i:02d}", id=note_id, mission_id="morning_brief",
            kind="management_note", title="morning_brief",
            created_at=(base + timedelta(days=i)).isoformat(),
        )
    (mgmt / ".consumed.json").write_text(json.dumps({cid: {} for cid in consumed_ids}), encoding="utf-8")

    # 2 genuinely pending: different mission_ids, fresh, not in .consumed.json
    _write_note(mgmt, "rebalance-1", id="rebalance-1", mission_id="rebalance_check",
                kind="management_note", title="Rebalance check needed",
                created_at="2026-06-30T09:00:00+00:00")
    _write_note(mgmt, "strategic-1", id="strategic-1", mission_id="strategic_question",
                kind="management_note", title="Widen autonomy?",
                created_at="2026-07-01T09:00:00+00:00")
    return root


def test_30_consumed_2_pending_fixture_scan_mgmt_inbox():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_30_consumed_2_pending(root)

        from console.services.mgmt_note_state import scan_mgmt_inbox
        state = scan_mgmt_inbox(root)
        assert len(state.pending_rows) == 2, (
            f"expected 2 pending rows, got {len(state.pending_rows)}: {state.pending_rows}"
        )
        assert state.consumed_count == 30


def test_30_consumed_2_pending_fixture_via_list_layer_queues(tmp_path, monkeypatch):
    """Integration through list_layer_queues(): 2 pending L2 items + 1
    trailing synthetic consumed-summary item."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    repo = _minimal_dept_repo(tmp_path)
    _build_30_consumed_2_pending(repo)

    from console.services.github_reader import list_layer_queues, MGMT_CONSUMED_SUMMARY_KIND
    result = list_layer_queues("qtest459")
    l2 = result[2]

    summary_items = [i for i in l2 if i["kind"] == MGMT_CONSUMED_SUMMARY_KIND]
    pending_items = [i for i in l2 if i["kind"] != MGMT_CONSUMED_SUMMARY_KIND]

    assert len(pending_items) == 2, f"expected 2 pending L2 rows, got {pending_items}"
    assert len(summary_items) == 1, "expected exactly one collapsed consumed-summary row"
    assert "30" in summary_items[0]["title"]
    assert "traitée" in summary_items[0]["title"]


def test_30_consumed_2_pending_fixture_via_inbox_fragment_http(tmp_path, monkeypatch):
    """Full HTTP render: /dept/<slug>/inbox-fragment shows 2 pending rows +
    1 collapsed 'N notes traitées' line, per the card's evaluation bar."""
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    repo = _minimal_dept_repo(tmp_path)
    _build_30_consumed_2_pending(repo)

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from console.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/dept/qtest459/inbox-fragment")
    assert r.status_code == 200
    body = r.text

    # The 30 consumed notes must NOT show as 30 separate rows.
    assert body.count("morning_brief") <= 1, (
        "consumed morning_brief notes must not be listed individually; "
        f"body contains 'morning_brief' {body.count('morning_brief')} times"
    )
    # The collapsed summary line.
    assert "30 notes traitées" in body
    # The 2 genuinely-pending items.
    assert "Rebalance check needed" in body or "rebalance_check" in body
    assert "Widen autonomy" in body or "strategic_question" in body
    # Consumed-summary row must carry the muted class, not the human badge.
    assert "desk-queue-item--consumed-summary" in body


def test_grouped_mission_row_renders_multiplication_label(tmp_path, monkeypatch):
    """>1 pending notes sharing a mission_id render as one 'mission_id ×N
    (latest date)' row in the actual HTML, per card #459's grouping rule."""
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    repo = _minimal_dept_repo(tmp_path)
    mgmt = _mgmt_dir(repo)
    _write_note(mgmt, "a", id="a", mission_id="rebalance_check",
                kind="management_note", created_at="2026-06-10T09:00:00+00:00")
    _write_note(mgmt, "b", id="b", mission_id="rebalance_check",
                kind="management_note", created_at="2026-06-15T09:00:00+00:00")

    import sys
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]

    from console.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/dept/qtest459/inbox-fragment")
    assert r.status_code == 200
    assert "rebalance_check ×2" in r.text
