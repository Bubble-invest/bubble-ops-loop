"""test_collect.py — unit tests for scripts/cockpit_health/collect.py.

No real console app, no network — `collect()` is called with an injected
fake client (or `client=None, client_error=...` for the no-client path) and
a fixture list of check functions, entirely on-disk via tmp_path.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cockpit_health import collect  # noqa: E402
from scripts.cockpit_health import checks as checks_mod  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def json(self):
        return self._json_body


class FakeClient:
    def __init__(self, routes, raise_on=None):
        self._routes = routes
        self._raise_on = raise_on or {}

    def get(self, path):
        if path in self._raise_on:
            raise self._raise_on[path]
        return self._routes.get(path, FakeResponse(404))


TEST_PAGES = [{"id": "home", "path": "/", "description": "Home"},
              {"id": "costs", "path": "/costs", "description": "Costs"}]


def test_render_pages_records_http_facts():
    client = FakeClient({"/": FakeResponse(200), "/costs": FakeResponse(500)})
    result = collect.render_pages(client, pages=TEST_PAGES)
    assert result["home"]["http"]["ok"] is True
    assert result["home"]["http"]["status_code"] == 200
    assert result["costs"]["http"]["ok"] is False
    assert result["costs"]["http"]["status_code"] == 500
    assert result["home"]["checks"] == []


def test_render_pages_degrades_when_no_client():
    result = collect.render_pages(None, pages=TEST_PAGES)
    assert result["home"]["http"]["ok"] is False
    assert result["home"]["http"]["error"] == "no console client"


def test_render_pages_survives_a_raising_get():
    client = FakeClient({}, raise_on={"/": ConnectionError("boom")})
    result = collect.render_pages(client, pages=[TEST_PAGES[0]])
    assert result["home"]["http"]["ok"] is False
    assert "boom" in result["home"]["http"]["error"]


def _ok_check(client, ctx):
    return {"id": "fake_ok", "page": "home", "description": "", "observed": {},
            "source": {}, "consistent": True, "hard_inconsistency": False,
            "reason": "fine", "error": None}


def _hard_check(client, ctx):
    return {"id": "fake_hard", "page": "costs", "description": "", "observed": {},
            "source": {}, "consistent": False, "hard_inconsistency": True,
            "reason": "broken", "error": None}


def _list_check(client, ctx):
    return [
        {"id": "fake_list_1", "page": "home", "description": "", "observed": {},
         "source": {}, "consistent": True, "hard_inconsistency": False,
         "reason": "ok", "error": None},
    ]


def _raising_check(client, ctx):
    raise RuntimeError("check exploded")


def _unassigned_page_check(client, ctx):
    return {"id": "fake_unassigned", "page": "no-such-page", "description": "",
            "observed": {}, "source": {}, "consistent": True,
            "hard_inconsistency": False, "reason": "", "error": None}


def test_run_checks_files_results_under_declared_page():
    pages = collect.render_pages(None, pages=TEST_PAGES)
    pages = collect.run_checks(None, pages, ctx=checks_mod.Ctx(), check_fns=[_ok_check, _hard_check])
    assert len(pages["home"]["checks"]) == 1
    assert len(pages["costs"]["checks"]) == 1
    assert pages["costs"]["checks"][0]["hard_inconsistency"] is True


def test_run_checks_flattens_list_results():
    pages = collect.render_pages(None, pages=TEST_PAGES)
    pages = collect.run_checks(None, pages, ctx=checks_mod.Ctx(), check_fns=[_list_check])
    assert len(pages["home"]["checks"]) == 1
    assert pages["home"]["checks"][0]["id"] == "fake_list_1"


def test_run_checks_never_crashes_the_run_on_a_raising_check():
    pages = collect.render_pages(None, pages=TEST_PAGES)
    pages = collect.run_checks(None, pages, ctx=checks_mod.Ctx(),
                                check_fns=[_ok_check, _raising_check])
    assert len(pages["home"]["checks"]) == 1
    assert "_unassigned" in pages
    assert "check raised" in pages["_unassigned"]["checks"][0]["reason"]


def test_run_checks_buckets_unknown_page_ids_as_unassigned():
    pages = collect.render_pages(None, pages=TEST_PAGES)
    pages = collect.run_checks(None, pages, ctx=checks_mod.Ctx(),
                                check_fns=[_unassigned_page_check])
    assert "_unassigned" in pages
    assert pages["_unassigned"]["checks"][0]["id"] == "fake_unassigned"


def test_collect_writes_evidence_and_latest_copy(tmp_path):
    client = FakeClient({"/": FakeResponse(200), "/costs": FakeResponse(200)})
    fixed_now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    bundle, out_path = collect.collect(
        out_dir=tmp_path, keep_last=48, client=client, client_error=None, now=fixed_now,
    )
    assert out_path.exists()
    latest = tmp_path / "evidence_latest.json"
    assert latest.exists()
    on_disk = json.loads(latest.read_text())
    assert on_disk["run_at"] == "2026-09-28T12:00:00Z"
    assert on_disk == bundle


def test_collect_rotates_old_runs(tmp_path):
    client = FakeClient({"/": FakeResponse(200), "/costs": FakeResponse(200)})
    for i in range(5):
        collect.collect(
            out_dir=tmp_path, keep_last=2, client=client, client_error=None,
            now=datetime(2026, 9, 28, 12, i, 0, tzinfo=timezone.utc),
        )
    remaining = sorted(p.name for p in tmp_path.glob("evidence_*.json") if p.name != "evidence_latest.json")
    assert len(remaining) == 2


def test_collect_records_console_client_error(tmp_path):
    bundle, _ = collect.collect(
        out_dir=tmp_path, keep_last=48, client=None,
        client_error="CONSOLE_BEARER_TOKEN is not set",
        now=datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert bundle["console_client_error"] == "CONSOLE_BEARER_TOKEN is not set"


def test_count_hard_counts_across_all_pages():
    bundle = {"pages": {
        "a": {"checks": [{"hard_inconsistency": True}, {"hard_inconsistency": False}]},
        "b": {"checks": [{"hard_inconsistency": True}]},
    }}
    assert collect._count_hard(bundle) == 2
