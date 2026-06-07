"""
test_cost_tracker.py — per-agent / per-job token & cost panel (Joris msg 3994/4003).

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
    assert opus == pytest.approx(15.0, abs=0.01)   # $15/1M input opus


def test_unknown_model_priced_not_zero():
    pr = cost_tracker._load_pricing()
    c = cost_tracker._cost_of({"some-future-model": {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_create": 0}}, pr)
    assert c > 0  # never silently free


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
