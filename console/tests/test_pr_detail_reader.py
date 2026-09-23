"""Tests for the per-PR deep-link page's data fetch (board #1432/#1462).

Board #1462 security review follow-up: `fetch_pr_detail`'s files listing must
fully paginate (not trust a single `per_page=100` page) and must flag a
renamed-out-of-a-structural-path file as structural too. This is a UI hint
(file list + "structural" badge on the deep-link page), not the enforced
gate (`structural_status.py` is), but the reviewer asked for the same fix
here for consistency. `_get_json` is monkeypatched — no real GitHub call.
"""
from __future__ import annotations

import urllib.parse

from console.services import pr_detail_reader as pdr

_SHA = "f" * 40


def _page_num(url: str) -> int:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return int(qs.get("page", ["1"])[0])


def _stub(monkeypatch, *, files_pages: list[list[dict]], token: str = "fake-board-token"):
    monkeypatch.setattr(pdr, "_read_board_token", lambda: token)

    def fake_get_json(url, tok):
        if "/files" in url:
            page = _page_num(url)
            return files_pages[page - 1] if page <= len(files_pages) else []
        if "/check-runs" in url:
            return {"check_runs": []}
        # the bare PR fetch
        return {
            "title": "fix: example", "html_url": "https://github.com/o/r/pull/1",
            "state": "open", "head": {"sha": _SHA}, "base": {"ref": "main"},
        }
    monkeypatch.setattr(pdr, "_get_json", fake_get_json)


def test_structural_file_on_page_two_is_flagged(monkeypatch):
    page1 = [{"filename": f"docs/note_{i}.md", "status": "modified"} for i in range(100)]
    page2 = [{"filename": ".claude/agents/rnd.md", "status": "modified"}]
    _stub(monkeypatch, files_pages=[page1, page2])

    detail = pdr.fetch_pr_detail("Bubble-invest", "bubble-ops-loop", 1)
    assert detail is not None
    assert detail["structural"] is True
    assert len(detail["files"]) == 101
    assert any(f["path"] == ".claude/agents/rnd.md" and f["structural"] for f in detail["files"])


def test_rename_out_of_structural_path_is_flagged(monkeypatch):
    renamed = [{
        "filename": "docs/moved_policy.py",
        "previous_filename": "token-broker/src/policy.py",
        "status": "renamed",
    }]
    _stub(monkeypatch, files_pages=[renamed])

    detail = pdr.fetch_pr_detail("Bubble-invest", "bubble-ops-loop", 2)
    assert detail is not None
    assert detail["structural"] is True
    assert detail["files"][0]["structural"] is True


def test_pagination_error_shows_error_not_partial_list(monkeypatch):
    """A fetch failure on page 2 must not leave page 1's files displayed
    alongside the error — full success or a clean error, never a silent
    partial list (which could under-report a structural file)."""
    monkeypatch.setattr(pdr, "_read_board_token", lambda: "fake-board-token")
    page1 = [{"filename": f"docs/note_{i}.md", "status": "modified"} for i in range(100)]

    def fake_get_json(url, token):
        if "/files" in url:
            if _page_num(url) == 1:
                return page1
            raise RuntimeError("network blip")
        if "/check-runs" in url:
            return {"check_runs": []}
        return {
            "title": "fix: example", "html_url": "https://github.com/o/r/pull/1",
            "state": "open", "head": {"sha": _SHA}, "base": {"ref": "main"},
        }
    monkeypatch.setattr(pdr, "_get_json", fake_get_json)

    detail = pdr.fetch_pr_detail("Bubble-invest", "bubble-ops-loop", 3)
    assert detail is not None
    assert detail["files"] == []
    assert detail["files_error"]
