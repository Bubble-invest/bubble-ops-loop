"""
test_concierge_projects.py — working projects on a concierge page.

{{OPERATOR}} msg 1193: show what the concierge is building, read from
<workspace>/workspace/projects/*/STATUS.md.
"""
from __future__ import annotations

from console.services import concierge_reader


def _make_project(root, name, slug, status_md):
    d = root / name / "workspace" / "projects" / slug
    d.mkdir(parents=True)
    (d / "STATUS.md").write_text(status_md, encoding="utf-8")
    return d


def test_lists_projects_with_status(tmp_path):
    _make_project(tmp_path, "morty", "gefineo-anonymizer",
                  "# STATUS — gefineo-anonymizer\n\n**État : Étape 1 livrée.**\nDétails…")
    cards = concierge_reader.list_projects("morty", agents_root=str(tmp_path))
    assert len(cards) == 1
    c = cards[0]
    assert c.slug == "gefineo-anonymizer"
    assert c.title == "gefineo-anonymizer"          # "STATUS —" stripped
    assert "Étape 1 livrée" in c.status_line


def test_ignores_dirs_without_status(tmp_path):
    # a bare mirror dir (no STATUS.md) must not show up
    (tmp_path / "morty" / "workspace" / "projects" / "bubble-x-workspace").mkdir(parents=True)
    _make_project(tmp_path, "morty", "real-project", "# Real\n**État : en cours**")
    cards = concierge_reader.list_projects("morty", agents_root=str(tmp_path))
    assert [c.slug for c in cards] == ["real-project"]


def test_no_projects_dir_is_empty(tmp_path):
    (tmp_path / "claudette").mkdir()
    assert concierge_reader.list_projects("claudette", agents_root=str(tmp_path)) == []


def test_newest_first(tmp_path):
    import os, time
    a = _make_project(tmp_path, "morty", "older", "# Older\n**État : x**")
    b = _make_project(tmp_path, "morty", "newer", "# Newer\n**État : y**")
    os.utime(a / "STATUS.md", (1000, 1000))
    os.utime(b / "STATUS.md", (2000, 2000))
    cards = concierge_reader.list_projects("morty", agents_root=str(tmp_path))
    assert [c.slug for c in cards] == ["newer", "older"]


def test_status_line_truncated(tmp_path):
    _make_project(tmp_path, "morty", "verbose", "# V\n**" + "x" * 300 + "**")
    c = concierge_reader.list_projects("morty", agents_root=str(tmp_path))[0]
    assert len(c.status_line) <= 160 and c.status_line.endswith("…")
