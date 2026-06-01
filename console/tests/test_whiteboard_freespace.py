"""
test_whiteboard_freespace.py — the free-space "Whiteboard" card.

Joris msg 1174 (2026-06-01): the Tableau de bord section needs a real blank
canvas card the dept manager fills with anything (different per dept —
e.g. Maya adds department-specific data). The framework must exist for
every current + future dept as an empty free space; content comes from
<repo>/whiteboard.md, rendered verbatim.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


# ─── Reader ─────────────────────────────────────────────────────────────

def test_freeform_absent_is_none(disk_root):
    _repo(disk_root)
    assert github_reader.load_whiteboard_freeform("demo") is None


def test_freeform_blank_file_is_none(disk_root):
    repo = _repo(disk_root)
    (repo / "whiteboard.md").write_text("   \n\t\n", encoding="utf-8")
    assert github_reader.load_whiteboard_freeform("demo") is None


def test_freeform_returns_verbatim(disk_root):
    repo = _repo(disk_root)
    body = "# Suivi Maya\n\n- pipeline: 3 deals\n- NPS: 72\n"
    (repo / "whiteboard.md").write_text(body, encoding="utf-8")
    assert github_reader.load_whiteboard_freeform("demo") == body


def test_freeform_unknown_dept_is_none(disk_root):
    assert github_reader.load_whiteboard_freeform("nope") is None


# ─── Dept page rendering ────────────────────────────────────────────────

def test_card_always_present_even_when_empty(client):
    """The blank canvas is the framework — present for a dept with no
    whiteboard.md yet, showing the empty-state hint."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "whiteboard-free" in r.text
    assert ">Whiteboard<" in r.text                 # the card title
    assert "espace libre de Fixture" in r.text       # uses dept display name
    assert "whiteboard.md" in r.text                 # the how-to hint


def test_card_renders_manager_content(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "whiteboard.md").write_text(
        "Objectif trimestre : 10 clients\nNote libre du manager.",
        encoding="utf-8",
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Objectif trimestre : 10 clients" in r.text
    assert "Note libre du manager." in r.text
    # When filled, the empty-state hint is gone.
    assert "Rien pour l'instant" not in r.text


def test_tableau_de_bord_section_always_renders(client):
    """Section 1 must render even with no KPIs/graphs, so the free space
    is always reachable (was previously hidden when both were empty)."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert 'id="dept-whiteboard-heading"' in r.text
