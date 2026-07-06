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


def _session_with_ts(path: Path, *, model: str, turns: list[dict], first_user: str = "") -> None:
    """Like _session, but each turn dict may carry a "ts" key (ISO string,
    e.g. "2026-07-02T09:00:00.000Z") written out as the JSONL entry's
    "timestamp" field — for the day= (per-message-timestamp) bucketing tests.
    "usage" is the rest of the turn dict (everything except "ts")."""
    lines = []
    if first_user:
        lines.append({"type": "user", "message": {"role": "user", "content": first_user}})
    for t in turns:
        t = dict(t)
        ts = t.pop("ts", None)
        entry = {"type": "assistant", "message": {"role": "assistant", "model": model, "usage": t}}
        if ts is not None:
            entry["timestamp"] = ts
        lines.append(entry)
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


# ─── Convention-driven attribution (board #500, follows #211) ──────────
# The durable fix: derive the label from the `bubble-ops-<slug>` naming
# convention (same one dept_registry.list_departments() uses), so a NEW or
# RENAMED dept is attributed automatically instead of silently dropping to
# None. Plus a logged warning when a real _mac workspace can't be attributed,
# so a future drop is visible, not invisible.

def test_classify_mac_convention_content_is_miranda():
    # convention core: bubble-ops-content resolves via the alias table → miranda
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-content"
    ) == "miranda (jade-mac)"


def test_classify_mac_convention_accountant_is_slug():
    # convention core: no alias → the slug itself (accountant), not mis-caught
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-accountant"
    ) == "accountant"


def test_classify_mac_convention_future_rename_auto_resolves():
    # THE WHOLE POINT: a future bubble-ops-ben rename auto-resolves to "ben".
    # This FAILS on the pre-#500 code (bubble-ops-ben was not in the #211 list,
    # so classify() returned None and ben's sessions would silently drop).
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-ben"
    ) == "ben"


def test_classify_vps_ben_convention_unchanged():
    # regression: the VPS-live -home-claude-agents-bubble-ops-* branch is untouched
    assert cost_tracker.classify("-home-claude-agents-bubble-ops-ben") == "ben"


def test_classify_mac_genuinely_unknown_workspace_is_none():
    # a genuinely-unknown workspace (matches neither convention nor legacy map)
    # is still dropped (None) — we do not start counting random dirs.
    assert cost_tracker.classify(
        "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-totally-unknown-xyz"
    ) is None


def test_classify_mac_unattributed_real_workspace_warns(caplog):
    # a real agent workspace (contains claude-workspaces) that can't be
    # attributed must emit a WARNING so the drop is VISIBLE, not silent.
    with caplog.at_level("WARNING", logger="console.services.cost_tracker"):
        assert cost_tracker.classify(
            "_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-totally-unknown-xyz"
        ) is None
    assert any(
        rec.levelname == "WARNING" and "unattributed" in rec.getMessage()
        for rec in caplog.records
    )


def test_classify_mac_noise_dir_does_not_warn(caplog):
    # a noise / sub-path dir WITHOUT claude-workspaces must NOT warn (no spam).
    with caplog.at_level("WARNING", logger="console.services.cost_tracker"):
        assert cost_tracker.classify("_mac-jade/some-random-noise-dir") is None
    assert not any(rec.levelname == "WARNING" for rec in caplog.records)


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


# ─── day= per-message-timestamp bucketing (board #499 residual #2) ─────────
# build_report's default (day=None) path buckets by FILE MTIME. That's fast
# but wrong for an exceptional-day audit: editing/touching an old session
# file today makes it look like today's spend under mtime bucketing, and you
# can't isolate one specific past calendar day at all. day="YYYY-MM-DD" adds
# a "day" span computed from each message's OWN timestamp instead.

def test_day_none_path_unchanged_by_new_param(fake_projects):
    """Back-compat: calling build_report with no day= arg at all produces the
    exact same shape (no "day" key anywhere) as before this feature existed —
    proves the additive param doesn't touch the default path."""
    _session(fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
              model="claude-sonnet-4-6",
              turns=[{"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
    rep = cost_tracker.build_report(refresh=True)
    assert "day_requested" not in rep
    assert "day" not in rep["totals"]
    assert "day" not in rep["agents"]["ben"]


def test_day_filter_isolates_one_calendar_day_by_message_ts(fake_projects):
    """A session file has messages on TWO different days (by timestamp). With
    day="2026-07-02", only that day's usage is counted in the "day" span —
    proving isolation works even when both days' messages live in the SAME
    file (so file mtime alone could never separate them)."""
    _session_with_ts(
        fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
        model="claude-sonnet-4-6",
        turns=[
            {"ts": "2026-07-02T09:00:00.000Z", "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"ts": "2026-07-03T09:00:00.000Z", "input_tokens": 2_000_000, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ],
    )
    rep = cost_tracker.build_report(refresh=True, day="2026-07-02")
    assert rep["day_requested"] == "2026-07-02"
    day_span = rep["agents"]["ben"]["day"]
    # only the 07-02 message's 1M input tokens at $3/1M sonnet == $3.00 —
    # NOT the combined 3M tokens from both days.
    assert day_span["cost"] == pytest.approx(3.0, abs=0.01)
    assert day_span["tokens"] == 1_000_000
    assert day_span["runs"] == 1


def test_day_filter_touched_old_file_not_miscounted_as_today(fake_projects, monkeypatch):
    """The motivating bug: a session file's mtime says "today" (because it was
    edited/touched today) but its actual messages are timestamped on an OLDER
    day. Under the default mtime-based "today" bucket this file wrongly counts
    as today's spend. Under day=<that older day>, it correctly isolates to
    that day and does NOT show up in the (separate) mtime "today" bucket
    confusion — i.e. the day= figure reflects message content, not mtime."""
    f = fake_projects / "-home-claude-agents-bubble-ops-ben" / "old.jsonl"
    _session_with_ts(
        f, model="claude-sonnet-4-6",
        turns=[{"ts": "2026-07-02T09:00:00.000Z", "input_tokens": 500_000, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}],
    )
    # Simulate the file being touched/edited "today" (mtime = now) even though
    # its one message is timestamped 2026-07-02.
    now = time.time()
    import os
    os.utime(f, (now, now))

    rep = cost_tracker.build_report(refresh=True, day="2026-07-02")
    day_span = rep["agents"]["ben"]["day"]
    assert day_span["tokens"] == 500_000
    assert day_span["cost"] == pytest.approx(1.5, abs=0.01)  # 0.5M x $3/1M sonnet


def test_day_filter_no_matching_messages_is_empty_not_error(fake_projects):
    """A day with zero matching messages must not raise and must not appear
    under any agent (parse_session_for_day returns None → skipped)."""
    _session_with_ts(
        fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
        model="claude-sonnet-4-6",
        turns=[{"ts": "2026-07-02T09:00:00.000Z", "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}],
    )
    rep = cost_tracker.build_report(refresh=True, day="1999-01-01")
    assert rep["day_requested"] == "1999-01-01"
    # ben still appears (from the today/week mtime pass) but its "day" span is blank
    assert rep["agents"]["ben"]["day"] == {
        "cost": 0.0, "cache_cost": 0.0, "tokens": 0, "runs": 0, "by_model": {}
    }


def test_day_filter_deepseek_still_zero_priced_in_day_span(fake_projects):
    """The day= path must still flow through the SAME pricing table — an
    unknown/non-Anthropic model on the isolated day still prices at $0, not
    some Anthropic default (ties residual #1 and #2 together end-to-end)."""
    _session_with_ts(
        fake_projects / "-home-claude-agents-bubble-ops-ben" / "s1.jsonl",
        model="deepseek-v4-pro",
        turns=[{"ts": "2026-07-02T09:00:00.000Z", "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}],
    )
    rep = cost_tracker.build_report(refresh=True, day="2026-07-02")
    day_span = rep["agents"]["ben"]["day"]
    assert day_span["cost"] == 0.0


def test_parse_session_for_day_unit(tmp_path):
    """Unit-level check on parse_session_for_day directly (bypassing
    build_report): only entries whose timestamp[:10] == day are accumulated."""
    f = tmp_path / "s.jsonl"
    _session_with_ts(
        f, model="claude-haiku-4-5",
        turns=[
            {"ts": "2026-07-01T00:00:00.000Z", "input_tokens": 10, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"ts": "2026-07-02T23:59:59.999Z", "input_tokens": 20, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"ts": "2026-07-03T00:00:00.000Z", "input_tokens": 40, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ],
    )
    parsed = cost_tracker.parse_session_for_day(f, "2026-07-02")
    assert parsed is not None
    assert parsed["model_usage"]["claude-haiku-4-5"]["input"] == 20
    assert parsed["n_turns"] == 1


def test_parse_session_for_day_missing_file_returns_none(tmp_path):
    assert cost_tracker.parse_session_for_day(tmp_path / "nope.jsonl", "2026-07-02") is None


def test_parse_session_for_day_no_match_returns_none(tmp_path):
    f = tmp_path / "s.jsonl"
    _session_with_ts(
        f, model="claude-sonnet-4-6",
        turns=[{"ts": "2026-07-01T00:00:00.000Z", "input_tokens": 10, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}],
    )
    assert cost_tracker.parse_session_for_day(f, "2026-07-02") is None
