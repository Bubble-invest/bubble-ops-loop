"""
test_home_clickable_tiles.py — board #532.

Each dept-widget stat-tile on the home page must link to the REAL destination,
not just /dept:
  · "décisions en attente" → the single pending gate /gate/<slug>/<id> when
    there is exactly one gate; else the dept decisions section.
  · "sujets ouverts"       → the kind batch /gate/<slug>/kind/<kind> when a
    single gate-group; else the dept decisions section.
  · "cartes kanban"        → the by-département kanban view anchored to this
    dept (/kanban?view=dept#dept-<slug>).

Also: the status head must contain NO nested <a> — the avatar/name link and
each tile are siblings, not one <a> wrapping others (invalid HTML).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


# ── on-disk fixture builders (mirror test_home_gate_grouping.py) ──────────────
def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "depts"
    root.mkdir()
    return root


def _make_live_dept(root: Path, slug: str, display: str) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops", "mandate": f"do {slug}"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": display,
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "queues" / "gates").mkdir(parents=True)
    return repo


def _write_gate(repo: Path, gate_id: str, kind: str) -> None:
    (repo / "queues" / "gates" / f"{gate_id}.yaml").write_text(
        yaml.safe_dump({
            "id": gate_id, "kind": kind, "source_layer": 2,
            "target_layer": 3, "risk_level": "low", "requires_human": True,
            "current_mode": "manual_required", "gate_policy_id": kind,
            "actions": ["approve", "reject", "modify", "defer"],
        }, sort_keys=False),
        encoding="utf-8",
    )


def _build_client(monkeypatch, root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def _status_head(body: str) -> str:
    """Return the live dept's status-head block: from <div class="ddw-status">
    through the end of its stat-strip. Scoped tightly so tile-href assertions
    don't bleed into the decision ROWS below (which legitimately link to
    individual gates)."""
    start = body.index('class="ddw-status"')
    # The decision rows (ddw-rows) or the "rien à décider" note (ddw-nothing)
    # follow the status head; cut there so tile hrefs never overlap gate rows.
    ends = [body.index(m, start) for m in ('ddw-rows', 'ddw-nothing') if m in body[start:]]
    end = min(ends) if ends else start + 1200
    return body[start:end]


# ── décisions en attente tile ────────────────────────────────────────────────
def test_single_gate_decisions_tile_links_straight_to_the_gate(monkeypatch, tmp_path):
    """Exactly one pending gate → the 'décisions en attente' tile deep-links to
    /gate/<slug>/<id>, not /dept."""
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "echo-42", "echo_action")

    body = _build_client(monkeypatch, root).get("/").text
    assert 'href="/gate/fixture/echo-42"' in body, \
        "single-gate decisions tile must link to /gate/<slug>/<id>"


def test_multi_gate_decisions_tile_links_to_dept_decisions(monkeypatch, tmp_path):
    """Multiple pending gates → the decisions tile falls back to the dept
    decisions section (no single-gate deep-link)."""
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "g-a", "research_decision")
    _write_gate(repo, "g-b", "trade_order")

    body = _build_client(monkeypatch, root).get("/").text
    head = _status_head(body)
    # The decisions tile (first stat-tile <a>) points at the dept decisions
    # section, and NOT at any single gate id.
    assert 'href="/dept/fixture#dept-decisions-heading"' in head
    assert 'href="/gate/fixture/g-a"' not in head
    assert 'href="/gate/fixture/g-b"' not in head


# ── sujets ouverts tile ───────────────────────────────────────────────────────
def test_single_group_topics_tile_links_to_kind_batch(monkeypatch, tmp_path):
    """One gate-group (one kind) → the 'sujets ouverts' tile links to the kind
    batch view /gate/<slug>/kind/<kind>."""
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "echo-1", "echo_action")

    head = _status_head(_build_client(monkeypatch, root).get("/").text)
    assert 'href="/gate/fixture/kind/echo_action"' in head, \
        "single-kind topics tile must link to /gate/<slug>/kind/<kind>"


# ── cartes kanban tile ────────────────────────────────────────────────────────
def test_kanban_tile_links_to_dept_scoped_kanban(monkeypatch, tmp_path):
    """The 'cartes kanban' tile links to the by-département kanban view anchored
    to this dept (/kanban?view=dept#dept-<slug>)."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "fixture", "Fixture")

    head = _status_head(_build_client(monkeypatch, root).get("/").text)
    assert 'href="/kanban?view=dept#dept-fixture"' in head, \
        "kanban tile must deep-link to the dept-scoped by-département view"


def test_kanban_page_has_view_switcher_query_param_support(monkeypatch, tmp_path):
    """The kanban page's view switcher must honour the ?view= query param the
    home tile relies on, and expose the hash-scroll behaviour."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "fixture", "Fixture")

    r = _build_client(monkeypatch, root).get("/kanban")
    assert r.status_code == 200
    assert "URLSearchParams" in r.text and "'view'" in r.text
    assert "scrollIntoView" in r.text and "window.location.hash" in r.text


def test_kanban_dept_view_renders_deep_link_anchor(monkeypatch, tmp_path):
    """When a board card is tagged to a dept, the by-département view must emit
    the id=dept-<slug> anchor that the home 'cartes kanban' tile targets."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "fixture", "Fixture")
    c = _build_client(monkeypatch, root)

    # Stub the board fetch: one open card owned by the 'fixture' dept.
    from console.routes import kanban as kanban_mod
    fake_issue = {
        "number": 999, "title": "test card", "body": "Job: do the thing",
        "labels": [{"name": "dept:fixture"}, {"name": "status:investigating"}],
        "url": "https://github.com/Bubble-invest/bubble-ops-board/issues/999",
        "updatedAt": "2026-07-03T10:00:00Z", "state": "open",
    }
    monkeypatch.setattr(kanban_mod, "_fetch_issues", lambda: ([fake_issue], None))

    r = c.get("/kanban")
    assert r.status_code == 200
    assert 'id="dept-fixture"' in r.text, \
        "dept view must emit id=dept-<slug> anchor for the home tile deep-link"
    assert 'class="dept-anchor"' in r.text


# ── no nested anchors in the status head ──────────────────────────────────────
def test_status_head_has_no_nested_anchors(monkeypatch, tmp_path):
    """The live dept status head must not nest <a> inside <a> (invalid HTML):
    the avatar/name link and the 3 tiles are siblings under a plain <div>."""
    root = _make_root(tmp_path)
    repo = _make_live_dept(root, "fixture", "Fixture")
    _write_gate(repo, "echo-1", "echo_action")

    head = _status_head(_build_client(monkeypatch, root).get("/").text)
    # Walk the anchor opens/closes in the head; depth must never exceed 1.
    depth = 0
    for tok in re.findall(r"<a\b|</a>", head):
        if tok == "</a>":
            depth -= 1
        else:
            depth += 1
            assert depth <= 1, "nested <a> detected in dept status head"
        assert depth >= 0
    # Sanity: the head does contain the tile links (4 anchors: name + 3 tiles).
    assert head.count("<a ") >= 4


def test_status_head_container_is_not_an_anchor(monkeypatch, tmp_path):
    """The .ddw-status wrapper must be a <div>, not the old wrapping <a>."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "fixture", "Fixture")
    body = _build_client(monkeypatch, root).get("/").text
    assert '<div class="ddw-status">' in body
    assert '<a class="ddw-status"' not in body  # old wrapping anchor is gone
