"""
test_cost_tracker.py — per-agent / per-job token & cost panel ({{OPERATOR}} msg 3994/4003).

The cockpit must show what each agent + each `claude -p` cron spends (today/7d),
priced from the token usage in the session JSONLs (works for both -p crons and
interactive dept loops). Estimate, not billing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from console.services import cost_tracker


def _session(path: Path, *, model: str, turns: list[dict], first_user: str = "") -> None:
    """Write a minimal session JSONL: one user msg + N assistant usage msgs."""
    lines = []
    if first_user:
        lines.append({"type": "user", "message": {"role": "user", "content": first_user}})
    for u in turns:
        lines.append({"type": "assistant", "message": {"role": "assistant", "model": model, "usage": u}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")


@pytest.fixture
def fake_projects(monkeypatch, tmp_path):
    proj = tmp_path / "projects"
    cache = tmp_path / "cache"
    monkeypatch.setattr(cost_tracker, "PROJECTS_DIR", proj)
    monkeypatch.setattr(cost_tracker, "CACHE_DIR", cache)
    monkeypatch.setattr(cost_tracker, "CACHE_FILE", cache / "c.json")
    return proj


def test_prices_by_model_haiku_cheaper_than_opus():
    pr = cost_tracker._load_pricing()
    usage = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_create": 0}
    haiku = cost_tracker._cost_of({"claude-haiku-4-5": usage}, pr)
    opus = cost_tracker._cost_of({"claude-opus-4-8": usage}, pr)
    assert haiku < opus
    assert haiku == pytest.approx(1.0, abs=0.01)   # $1/1M input haiku
    assert opus == pytest.approx(5.0, abs=0.01)    # $5/1M input opus (Opus-4, current)


def test_unknown_model_priced_zero():
    """Unknown / non-Anthropic models (deepseek etc.) price at $0 rather than
    being silently over-billed at an Anthropic rate — we'd rather under-count an
    unpriced model than attribute phantom Anthropic dollars to it."""
    pr = cost_tracker._load_pricing()
    c = cost_tracker._cost_of({"some-future-model": {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_create": 0}}, pr)
    assert c == 0.0


def test_attributes_vps_dept_by_dir(fake_projects):
    d = fake_projects / "-home-claude-agents-bubble-ops-ben"
    _session(d / "s1.jsonl", model="claude-sonnet-4-6",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    rep = cost_tracker.build_report(refresh=True)
    assert "ben" in rep["agents"]
    assert rep["agents"]["ben"]["week"]["runs"] == 1


def test_detects_p_cron_job_wiki_compile(fake_projects):
    d = fake_projects / "-home-claude"
    _session(d / "c1.jsonl", model="claude-haiku-4-5",
             turns=[{"input_tokens": 10, "output_tokens": 10,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}],
             first_user="Run the cloud-wiki-compile skill in COMPILE mode: mine today's transcripts")
    rep = cost_tracker.build_report(refresh=True)
    assert "wiki-compile" in rep["agents"]


def test_synthetic_model_excluded_from_breakdown(fake_projects):
    d = fake_projects / "-home-claude-agents-bubble-ops-maya"
    _session(d / "s.jsonl", model="<synthetic>",
             turns=[{"input_tokens": 0, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    rep = cost_tracker.build_report(refresh=True)
    # synthetic zero-token session contributes no model row
    if "maya" in rep["agents"]:
        assert "<synthetic>" not in rep["agents"]["maya"]["week"]["by_model"]


def test_totals_sum_agents(fake_projects):
    _session(fake_projects / "-home-claude-agents-bubble-ops-tony" / "s.jsonl",
             model="claude-sonnet-4-6",
             turns=[{"input_tokens": 1_000_000, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    _session(fake_projects / "-home-claude-agents-bubble-ops-maya" / "s.jsonl",
             model="claude-haiku-4-5",
             turns=[{"input_tokens": 1_000_000, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    rep = cost_tracker.build_report(refresh=True)
    assert rep["totals"]["week"]["cost"] == pytest.approx(
        rep["agents"]["tony"]["week"]["cost"] + rep["agents"]["maya"]["week"]["cost"], abs=0.01
    )
    assert rep["totals"]["week"]["runs"] == 2


# ─── Mac-workspace attribution: current dept/concierge workspaces ──────
# Regression for the cost-attribution bug: the Jade-Mac dept/concierge
# workspaces were renamed to bubble-ops-content (Miranda), bubble-ops-accountant
# (Geraldine) and ellie (Ellie concierge). Those keys were missing from
# classify()'s Mac-cache map, so classify() returned None and their sessions
# were silently dropped from /costs (Miranda undercounted by ~99.9%:
# 1,676 msgs / 388M tokens dropped on 2026-07-02). Relates to #496.

def test_classify_mac_current_content_workspace_is_miranda():
    # current Jade-Mac content workspace → miranda (jade-mac), NOT None
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-content"
    ) == "miranda (jade-mac)"


def test_classify_mac_legacy_miranda_socials_still_miranda():
    # legacy workspace must still attribute to miranda (both aggregate under miranda)
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-Miranda-Socials"
    ) == "miranda (jade-mac)"


def test_classify_mac_accountant_workspace():
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-accountant"
    ) == "accountant"


def test_classify_mac_ellie_workspace():
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-ellie"
    ) == "ellie"


def test_classify_mac_rick_unchanged():
    # regression: existing Rick mapping must be untouched
    assert cost_tracker.classify(
        "_mac-joris/-Users-joris-claude-workspaces-Rick-RnD"
    ) == "rick"


def test_classify_vps_ben_unchanged():
    # regression: VPS-live dept branch must be untouched
    assert cost_tracker.classify("-home-claude-agents-bubble-ops-ben") == "ben"


def test_classify_mac_unknown_workspace_still_none():
    # truly-unknown workspace must still be dropped (None)
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-some-random-workspace"
    ) is None


# ─── Report-level TTL cache (board #450) ───────────────────────────────

@pytest.fixture(autouse=True)
def _reset_report_cache():
    """The report TTL cache is module-level state shared across tests —
    reset it before/after each test so tests don't leak into each other."""
    cost_tracker._report_cache["report"] = None
    cost_tracker._report_cache["built_at"] = 0.0
    yield
    cost_tracker._report_cache["report"] = None
    cost_tracker._report_cache["built_at"] = 0.0


def test_report_ttl_cache_serves_cached_report_within_window(fake_projects, monkeypatch):
    _session(fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
             model="claude-sonnet-4-6",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    first = cost_tracker.build_report(refresh=True)  # populates the TTL cache too
    assert "ben" in first["agents"]

    # Add a NEW agent's session after the first build. A plain build_report()
    # (refresh=False) within the TTL window must NOT see it — it should serve
    # the cached report rather than re-walking the tree.
    _session(fake_projects / "-home-claude-agents-bubble-ops-maya" / "s1.jsonl",
             model="claude-haiku-4-5",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    cached = cost_tracker.build_report(refresh=False)
    assert cached is first  # same object — served straight from the cache
    assert "maya" not in cached["agents"]


def test_report_ttl_cache_expires_after_window(fake_projects, monkeypatch):
    _session(fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
             model="claude-sonnet-4-6",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    cost_tracker.build_report(refresh=True)

    _session(fake_projects / "-home-claude-agents-bubble-ops-maya" / "s1.jsonl",
             model="claude-haiku-4-5",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])

    # Simulate the TTL having elapsed by rewinding the cached built_at.
    cost_tracker._report_cache["built_at"] -= (cost_tracker._REPORT_TTL_SECONDS + 1)

    rep = cost_tracker.build_report(refresh=False)
    assert "maya" in rep["agents"]  # rebuilt — TTL had expired


def test_report_refresh_true_always_bypasses_ttl_cache(fake_projects):
    _session(fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
             model="claude-sonnet-4-6",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    cost_tracker.build_report(refresh=True)

    _session(fake_projects / "-home-claude-agents-bubble-ops-maya" / "s1.jsonl",
             model="claude-haiku-4-5",
             turns=[{"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])

    # refresh=True must see the new session even though the TTL window is
    # still open — the explicit-refresh escape hatch must keep working.
    rep = cost_tracker.build_report(refresh=True)
    assert "maya" in rep["agents"]
