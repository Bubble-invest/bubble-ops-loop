"""
test_whiteboard_notes.py — the Tableau de bord "Notes" card (card #507).

The dept page rendered `whiteboard.notes` directly with `{{ whiteboard.notes
}}`. That field is documented as a free-text string, but Ben's whiteboard.yaml
populates it as a YAML LIST of dated decision-log entries — Jinja renders a
list via Python repr(), producing an unreadable `['2026-07-03 L4 WEEKLY
REVIEW...', ...]` wall on /dept/ben.

Fix: `github_reader.load_whiteboard()` normalizes `notes` (str OR list) into
`notes_list` (always list[str]); the route sanitizes each entry the same way
as the free-space whiteboard (markdown_render.render_markdown_safe — nh3
allowlist, XSS-safe); the template renders N readable entries inside a
collapsed-by-default <details> (mirrors the "N derniers passages" backup
idiom), never a raw list repr.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console import settings
from console.services import github_reader


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _repo(root: Path, slug: str = "demo") -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir(parents=True)
    return repo


def _write_whiteboard(repo: Path, notes) -> None:
    (repo / "whiteboard.yaml").write_text(
        yaml.safe_dump({"title": "Tableau de bord", "notes": notes}, sort_keys=False),
        encoding="utf-8",
    )


# ─── Reader: load_whiteboard() notes_list normalization ────────────────────

def test_notes_str_normalizes_to_single_item_list(disk_root):
    repo = _repo(disk_root)
    _write_whiteboard(repo, "Une note libre du manager.")
    wb = github_reader.load_whiteboard("demo")
    assert wb["notes"] == "Une note libre du manager."
    assert wb["notes_list"] == ["Une note libre du manager."]


def test_notes_list_normalizes_verbatim(disk_root):
    repo = _repo(disk_root)
    entries = [
        "2026-07-03 L4 WEEKLY REVIEW — allocation rebalanced.",
        "2026-07-02 L3 ROTATION — swapped GLD for IAU.",
    ]
    _write_whiteboard(repo, entries)
    wb = github_reader.load_whiteboard("demo")
    assert wb["notes"] == entries
    assert wb["notes_list"] == entries


def test_notes_absent_is_empty_list(disk_root):
    repo = _repo(disk_root)
    (repo / "whiteboard.yaml").write_text(
        yaml.safe_dump({"title": "Tableau de bord"}, sort_keys=False),
        encoding="utf-8",
    )
    wb = github_reader.load_whiteboard("demo")
    assert wb["notes_list"] == []


def test_notes_blank_entries_are_dropped(disk_root):
    repo = _repo(disk_root)
    _write_whiteboard(repo, ["first entry", "   ", "", "second entry"])
    wb = github_reader.load_whiteboard("demo")
    assert wb["notes_list"] == ["first entry", "second entry"]


# ─── Dept page rendering ────────────────────────────────────────────────────

def _set_ben_notes(fixture_root: Path, notes) -> None:
    """Reuse the shared 'fixture' dept from conftest's fixture_root and give
    it a whiteboard.yaml with the notes shape under test."""
    repo = fixture_root / "bubble-ops-fixture"
    _write_whiteboard(repo, notes)


def test_list_notes_render_as_readable_entries_not_raw_repr(client, fixture_root):
    entries = [
        "2026-07-03 L4 WEEKLY REVIEW — allocation rebalanced toward defensives.",
        "2026-07-02 L3 ROTATION — swapped GLD for IAU, lower expense ratio.",
        "2026-07-01 L2 REBALANCE — trimmed NVDA overweight.",
    ]
    _set_ben_notes(fixture_root, entries)

    r = client.get("/dept/fixture")
    assert r.status_code == 200

    # Never the raw Python list repr.
    assert "['2026-07-03" not in r.text
    assert "\", \"2026-07-02" not in r.text

    # Each entry appears as its own readable item.
    for entry in entries:
        assert entry in r.text

    # Inside a <ul> of individual items, not one dumped blob.
    assert 'class="whiteboard-notes-list"' in r.text
    assert r.text.count('class="whiteboard-notes-item') == len(entries)


def test_str_notes_still_render(client, fixture_root):
    _set_ben_notes(fixture_root, "Une note libre du manager, en une seule chaîne.")

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Une note libre du manager, en une seule chaîne." in r.text
    # Single string -> the single-note div path, not the <ul> multi-item path.
    assert 'class="whiteboard-notes-list"' not in r.text


def test_notes_wrapped_in_collapsed_details(client, fixture_root):
    """Card #507 add-on: collapsible, collapsed by default (no `open`),
    mirroring the existing 'N derniers passages' <details> idiom."""
    _set_ben_notes(fixture_root, ["only entry here"])

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert 'class="whiteboard-notes-details"' in r.text
    # Collapsed by default: no bare `open` attribute on this specific <details>.
    assert '<details class="whiteboard-notes-details">' in r.text
    assert '<details class="whiteboard-notes-details" open>' not in r.text
    assert "📓 Notes (1 entrée)" in r.text


def test_notes_summary_pluralizes_count(client, fixture_root):
    _set_ben_notes(fixture_root, ["first", "second", "third"])

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "📓 Notes (3 entrées)" in r.text


def test_notes_absent_renders_no_details_block(client, fixture_root):
    """No whiteboard.yaml at all for the fixture dept -> no notes card."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert 'class="whiteboard-notes-details"' not in r.text


def test_script_note_is_escaped_not_executed(client, fixture_root):
    """XSS safety: a note containing a <script> tag must never reach the
    page unescaped — same nh3 sanitization as the free-space whiteboard."""
    malicious = "Normal text <script>alert('xss')</script> more text"
    _set_ben_notes(fixture_root, [malicious])

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "<script>alert" not in r.text
    assert "Normal text" in r.text
