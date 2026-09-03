"""test_loop_tick_watchdog.py — board #724: re-kick a dept loop whose tick
died mid-flight, WITHOUT disturbing a healthy-idle loop or worsening a
crash-loop.

Fixtures are built to the REAL transcript line shapes observed live on the
fleet (VPS ben/maya/tony, M1 content, M5 accountant, 2026-07..09):

  * a prompt turn      → {"type":"user", "message":{"content":"<str>"}}
  * an assistant text  → {"type":"assistant", "message":{"model":"claude-opus-4-8","content":[{"type":"text",...}]}}
  * a tool call        → assistant content [{"type":"tool_use","id":..,"name":..}] then
                          {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":..}]}}
  * an API error       → {"type":"assistant","isApiErrorMessage":true,
                          "message":{"model":"<synthetic>","content":[{"type":"text","text":"API Error: 529 Overloaded..."}]}}
                          followed by {"type":"system","subtype":"turn_duration"}
  * "No response requested." → synthetic assistant with isApiErrorMessage:false (NOT an error)

Three required scenarios (per the card): stalled → kicked; healthy idle →
untouched; crash-looping → not made worse. Plus every guard has a
RED-if-removed assertion.

Run: python3 -m pytest scripts/lib/tests/test_loop_tick_watchdog.py -q
"""
from __future__ import annotations

import datetime as _dt
import json
import os

import pytest

from scripts.lib import loop_tick_watchdog as wd

NOW = _dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
H = 3600
MIN = 60


def iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── transcript line builders ─────────────────────────────────────────────────
def prompt(ts, text="Run my full /loop tick now (STEP A-F per CLAUDE.md), not a chat reply."):
    return {"type": "user", "timestamp": iso(ts), "uuid": f"u{ts}", "message": {"role": "user", "content": text}}


def text(ts, s="Tick done. Next wake armed for 08:03 Paris."):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": f"a{ts}",
            "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [{"type": "text", "text": s}]}}


def tool_use(ts, name, tid, inp=None):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": f"t{ts}",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp or {}}]}}


def tool_result(ts, tid, is_error=False):
    return {"type": "user", "timestamp": iso(ts), "uuid": f"r{ts}",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "ok"}]}}


def api_error(ts, s="API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment."):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": f"e{ts}", "isApiErrorMessage": True,
            "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": s}]}}


def no_response(ts):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": f"n{ts}", "isApiErrorMessage": False,
            "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]}}


def system_turn(ts):
    return {"type": "system", "subtype": "turn_duration", "durationMs": 205, "isMeta": False, "timestamp": iso(ts)}


def attachment(ts):
    return {"type": "attachment", "timestamp": iso(ts), "attachment": {"type": "queued_command"}}


def enqueue(ts):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(ts), "content": "hi"}


def dequeue(ts):
    return {"type": "queue-operation", "operation": "dequeue", "timestamp": iso(ts)}


# ── fixture helpers ──────────────────────────────────────────────────────────
@pytest.fixture
def session(tmp_path):
    """Returns write(lines, mtime=None, subagent_mtime=None) → session_dir."""
    sdir = tmp_path / "projects" / "-home-claude-agents-bubble-ops-x"
    sdir.mkdir(parents=True)

    def write(lines, mtime=None, subagent_mtime=None, name="s1.jsonl"):
        p = sdir / name
        with open(p, "w", encoding="utf-8") as fh:
            for l in lines:
                fh.write(json.dumps(l) + "\n")
        last_ts = max((_dt.datetime.fromisoformat(l["timestamp"].replace("Z", "+00:00")).timestamp()
                       for l in lines if l.get("timestamp")), default=NOW - H)
        m = mtime if mtime is not None else last_ts
        os.utime(p, (m, m))
        if subagent_mtime is not None:
            sub = sdir / name[:-len(".jsonl")] / "subagents"
            sub.mkdir(parents=True, exist_ok=True)
            sp = sub / "agent-1.jsonl"
            sp.write_text("{}\n")
            os.utime(sp, (subagent_mtime, subagent_mtime))
        return str(sdir)
    return write


def healthy_tick(t0):
    """A complete healthy tick that armed its wake and ended normally."""
    return [
        prompt(t0), tool_use(t0 + 5, "Bash", "b1"), tool_result(t0 + 6, "b1"),
        tool_use(t0 + 30, "CronCreate", "c1", {"cron": "3 6 4 9 *"}), tool_result(t0 + 31, "c1"),
        text(t0 + 40), system_turn(t0 + 40),
    ]


def stalled_tick(t0, err=None):
    """A tick that died mid-flight: prompt → some work → API error → turn ends."""
    lines = [prompt(t0), tool_use(t0 + 5, "Bash", "b1"), tool_result(t0 + 6, "b1")]
    lines.append(api_error(t0 + 20, err) if err else api_error(t0 + 20))
    lines.append(system_turn(t0 + 20))
    return lines


def cfg(**over):
    c = wd.Config(restart_allowed=True)
    for k, v in over.items():
        setattr(c, k, v)
    return c


def run(session_dir, history=(), now=NOW, **over):
    obs = wd.observe_transcript("x", session_dir)
    return wd.decide(obs, list(history), now, cfg(**over))


def kick_event(ts, action=wd.KICK_INJECT, err_ts=None):
    return wd.format_event("x", action, "test", err_ts=err_ts, ts=wd.now_iso(ts), level=1)


# ═════════════════════════════════════════════════════════════════════════════
# 1. A genuinely STALLED loop gets re-kicked
# ═════════════════════════════════════════════════════════════════════════════
class TestStalledGetsKicked:
    def test_transient_error_idle_15min_is_kicked_by_inject(self, session):
        s = session(stalled_tick(NOW - 15 * MIN))
        d = run(s)
        assert d.action == wd.KICK_INJECT, d
        assert d.level == 1 and d.err_class == wd.CLS_TRANSIENT and d.notify

    @pytest.mark.parametrize("err", [
        "Server is temporarily limiting requests",                       # the #724 incident text
        "API Error: 529 Overloaded. This is a server-side issue",        # M1 2026-08-18 / 09-03
        "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)",
        "API Error: Unable to connect to API (ConnectionRefused)",
        "API Error: Connection closed mid-response",                     # M5 2026-07-13
        "Request timed out",
        "API Error: Your computer went to sleep mid-response. The response above may be incomplete.",
        "API Error: 500 Internal server error",
        "something entirely new we have never seen",                     # unknown → treated transient (bounded by guards)
    ])
    def test_every_transient_signature_is_kickable(self, session, err):
        s = session(stalled_tick(NOW - 20 * MIN, err))
        assert run(s).action == wd.KICK_INJECT

    def test_stalled_after_hours_still_kicked_once(self, session):
        """A stall from this morning (6h ago) is still a stall — the loop never
        re-armed. (Contrast: a healthy-idle loop 6h old is NOT touched.)"""
        s = session(stalled_tick(NOW - 6 * H))
        assert run(s).action == wd.KICK_INJECT

    def test_weekly_limit_whose_reset_has_passed_is_kicked(self, session):
        # NOW is 12:00 UTC; "resets 10am (UTC)" → 2h ago → passed → kickable.
        s = session(stalled_tick(NOW - 3 * H, "You've hit your weekly limit · resets 10am (UTC)"))
        d = run(s)
        assert d.action == wd.KICK_INJECT and d.err_class == wd.CLS_LIMIT
        assert "limit_reset_passed" in d.facts

    def test_inject_that_landed_nothing_escalates_to_restart(self, session):
        """Level 2: the previous kick was an inject for THIS SAME error line
        and the transcript never advanced → the session is deaf → restart
        (only because restart_allowed=True: --continue present, not a concierge)."""
        t_err = NOW - 2 * H + 20
        s = session(stalled_tick(NOW - 2 * H))
        hist = [kick_event(NOW - 40 * MIN, wd.KICK_INJECT, err_ts=t_err)]
        d = run(s, hist)
        assert d.action == wd.KICK_RESTART and d.level == 2, d

    def test_inject_that_landed_nothing_but_restart_forbidden_alerts(self, session):
        t_err = NOW - 2 * H + 20
        s = session(stalled_tick(NOW - 2 * H))
        hist = [kick_event(NOW - 40 * MIN, wd.KICK_INJECT, err_ts=t_err)]
        d = run(s, hist, restart_allowed=False)
        assert d.action == wd.ALERT_INJECT_FAILED and d.notify

    def test_new_error_after_inject_is_a_new_inject_not_a_restart(self, session):
        """The inject DID land (agent ticked, hit the error again → new error
        line). That is not deafness → inject again (after cooldown), never restart."""
        s = session(stalled_tick(NOW - 35 * MIN))        # err_ts = NOW-35min+20s
        hist = [kick_event(NOW - 90 * MIN, wd.KICK_INJECT, err_ts=NOW - 3 * H)]  # older error
        assert run(s, hist).action == wd.KICK_INJECT


# ═════════════════════════════════════════════════════════════════════════════
# 2. A HEALTHY-IDLE loop is NOT disturbed
# ═════════════════════════════════════════════════════════════════════════════
class TestHealthyIdleUntouched:
    def test_normal_end_of_tick_idle_6h_is_ok(self, session):
        """The self-paced loop armed tomorrow 08:03 and idles for hours. NEVER kick."""
        s = session(healthy_tick(NOW - 6 * H))
        d = run(s)
        assert d.action == wd.OK_IDLE and not d.is_kick and not d.notify

    def test_normal_end_of_tick_idle_20h_is_still_ok(self, session):
        s = session(healthy_tick(NOW - 20 * H))
        assert run(s).action == wd.OK_IDLE

    def test_historical_error_followed_by_healthy_activity_is_ok(self, session):
        """An error 3 turns ago must not haunt the verdict (tail-only, last activity wins)."""
        lines = stalled_tick(NOW - 5 * H) + healthy_tick(NOW - 3 * H)
        s = session(lines)
        assert run(s).action == wd.OK_IDLE

    def test_no_response_requested_is_not_an_error(self, session):
        """Synthetic but isApiErrorMessage:false — a normal quiet turn end."""
        s = session([prompt(NOW - 2 * H), no_response(NOW - 2 * H + 3), system_turn(NOW - 2 * H + 3)])
        assert run(s).action == wd.OK_IDLE

    def test_fresh_error_is_left_alone(self, session):
        """An error 3 min ago: a recurring cron/human may re-fire; wait ≥ stall_idle_sec."""
        s = session(stalled_tick(NOW - 3 * MIN))
        d = run(s)
        assert d.action == wd.OK_FRESH and not d.is_kick

    def test_busy_subagent_is_left_alone(self, session):
        """A long Opus turn writes NOTHING to the main transcript for many
        minutes (the 2026-06-03 Signal-6 false-positive lesson) — an active
        subagent transcript means the turn is progressing, even if the main
        tail happens to be an error line."""
        s = session(stalled_tick(NOW - 20 * MIN), subagent_mtime=NOW - 2 * MIN)
        assert run(s).action == wd.OK_BUSY

    def test_tool_call_in_flight_is_busy_not_hang(self, session):
        s = session([prompt(NOW - 5 * MIN), tool_use(NOW - 4 * MIN, "Agent", "ag1")])
        assert run(s).action == wd.OK_BUSY

    def test_same_turn_already_armed_cron_before_error_is_ok(self, session):
        """CronCreate succeeded earlier in the SAME turn, then the error hit —
        the loop WILL wake itself; a kick would only double-tick."""
        t0 = NOW - 2 * H
        lines = [prompt(t0), tool_use(t0 + 5, "CronCreate", "c1", {"cron": "0 */2 * * *"}), tool_result(t0 + 6, "c1"),
                 api_error(t0 + 30), system_turn(t0 + 30)]
        s = session(lines)
        assert run(s).action == wd.OK_ARMED

    def test_failed_croncreate_in_turn_does_not_count_as_armed(self, session):
        t0 = NOW - 2 * H
        lines = [prompt(t0), tool_use(t0 + 5, "CronCreate", "c1"), tool_result(t0 + 6, "c1", is_error=True),
                 api_error(t0 + 30), system_turn(t0 + 30)]
        s = session(lines)
        assert run(s).action == wd.KICK_INJECT

    def test_bookkeeping_lines_after_error_do_not_hide_it(self, session):
        """system/attachment/dequeue lines are not activity — the error is still the tail."""
        t0 = NOW - 2 * H
        lines = stalled_tick(t0) + [attachment(t0 + 21), dequeue(t0 + 22),
                                    {"type": "last-prompt"}, {"type": "mode"}]
        s = session(lines)
        assert run(s).action == wd.KICK_INJECT

    def test_missing_transcript_is_a_quiet_noop(self, tmp_path):
        d = wd.decide(wd.observe_transcript("x", str(tmp_path / "nope")), [], NOW, cfg())
        assert d.action == wd.OK_NO_TRANSCRIPT and not d.notify

    def test_pending_inbound_enqueue_is_activity_not_error(self, session):
        """An unanswered inbound (deaf bridge) is the telegram-watchdog's job,
        never a tick-stall kick."""
        s = session(stalled_tick(NOW - 2 * H) + [enqueue(NOW - 30 * MIN)])
        d = run(s)
        assert not d.is_kick


# ═════════════════════════════════════════════════════════════════════════════
# 3. A CRASH-LOOPING / non-kickable loop is NOT made worse
# ═════════════════════════════════════════════════════════════════════════════
class TestCrashLoopNotWorsened:
    def test_cooldown_blocks_a_second_kick(self, session):
        s = session(stalled_tick(NOW - 35 * MIN))
        hist = [kick_event(NOW - 10 * MIN, err_ts=NOW - 3 * H)]
        d = run(s, hist)
        assert d.action == wd.HOLD_COOLDOWN and not d.is_kick

    def test_guardrail_caps_kicks_per_window_then_escalates(self, session):
        """3 kicks in 6h already → the 4th is NOT a kick; it escalates once."""
        s = session(stalled_tick(NOW - 35 * MIN))
        hist = [kick_event(NOW - 5 * H, err_ts=NOW - 6 * H),
                kick_event(NOW - 3 * H, err_ts=NOW - 4 * H),
                kick_event(NOW - 1 * H, err_ts=NOW - 2 * H)]
        d = run(s, hist)
        assert d.action == wd.HOLD_GUARDRAIL and d.notify and not d.is_kick

    def test_guardrail_window_slides(self, session):
        s = session(stalled_tick(NOW - 35 * MIN))
        hist = [kick_event(NOW - 7 * H), kick_event(NOW - 6.5 * H), kick_event(NOW - 6.2 * H)]  # all outside 6h
        assert run(s, hist).action == wd.KICK_INJECT

    def test_restart_counts_toward_the_guardrail(self, session):
        s = session(stalled_tick(NOW - 35 * MIN))
        hist = [kick_event(NOW - 5 * H, wd.KICK_RESTART), kick_event(NOW - 3 * H, wd.KICK_RESTART),
                kick_event(NOW - 1 * H, wd.KICK_INJECT)]
        assert run(s, hist).action == wd.HOLD_GUARDRAIL

    def test_external_watchdog_restart_mark_is_honoured(self, session):
        """The VPS telegram-watchdog restarted this dept 4 min ago → we hold
        (never two supervisors restarting the same dept in one window)."""
        s = session(stalled_tick(NOW - 35 * MIN))
        d = run(s, [], external_restart_epoch=NOW - 4 * MIN)
        assert d.action == wd.HOLD_COOLDOWN

    @pytest.mark.parametrize("err", [
        "Not logged in · Please run /login",
        "Login expired · Please run /login",
        "Please run /login · API Error: 401 OAuth access token has been revoked.",
        "401 Invalid authentication credentials",
    ])
    def test_auth_failure_is_alert_only(self, session, err):
        s = session(stalled_tick(NOW - 20 * MIN, err))
        d = run(s)
        assert d.action == wd.ALERT_NONTRANSIENT and d.err_class == wd.CLS_AUTH and not d.is_kick

    def test_context_overflow_is_alert_only(self, session):
        s = session(stalled_tick(NOW - 20 * MIN, "Prompt is too long"))
        d = run(s)
        assert d.action == wd.ALERT_NONTRANSIENT and d.err_class == wd.CLS_CONTEXT

    @pytest.mark.parametrize("err", [
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",   # no reset → wait
        "You've hit your weekly limit · resets 10pm (UTC)",                             # NOW=12:00 → 10h ahead
        "You've hit your weekly limit · resets 12am (Europe/Paris)",                    # 22:00 UTC → ahead
    ])
    def test_usage_limit_still_active_is_alert_only(self, session, err):
        s = session(stalled_tick(NOW - 20 * MIN, err))
        d = run(s)
        assert d.action == wd.ALERT_LIMIT_WAIT and not d.is_kick

    def test_alert_dedupe_within_cooldown(self):
        hist = [wd.format_event("x", wd.ALERT_NONTRANSIENT, "r", ts=wd.now_iso(NOW - 5 * MIN),
                                extra={"dedupe_key": "auth:1"})]
        assert wd.already_alerted(hist, "x", "auth:1", NOW, 1800)
        assert not wd.already_alerted(hist, "x", "auth:2", NOW, 1800)
        assert not wd.already_alerted(hist, "x", "auth:1", NOW + 2 * H, 1800)

    def test_hang_is_alert_only_by_default(self, session):
        s = session([prompt(NOW - 2 * H), tool_use(NOW - 50 * MIN, "Bash", "b9")])
        d = run(s)
        assert d.action == wd.ALERT_HANG and not d.is_kick

    def test_hang_restart_is_opt_in_and_guarded(self, session):
        s = session([prompt(NOW - 2 * H), tool_use(NOW - 50 * MIN, "Bash", "b9")])
        assert run(s, hang_restart=True).action == wd.KICK_RESTART
        assert run(s, hang_restart=True, restart_allowed=False).action == wd.ALERT_HANG
        assert run(s, [kick_event(NOW - 5 * MIN)], hang_restart=True).action == wd.HOLD_COOLDOWN


# ═════════════════════════════════════════════════════════════════════════════
# 4. Building blocks
# ═════════════════════════════════════════════════════════════════════════════
class TestBuildingBlocks:
    @pytest.mark.parametrize("cwd,expected", [
        ("/Users/joris/claude-workspaces/Rick_RnD", "-Users-joris-claude-workspaces-Rick-RnD"),
        ("/Users/joris/.hermes-scripts", "-Users-joris--hermes-scripts"),
        ("/home/claude/agents/bubble-ops-ben", "-home-claude-agents-bubble-ops-ben"),
        ("/Users/jade-thi-viet-lanhoang/claude-workspaces/bubble-ops-accountant",
         "-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-accountant"),
    ])
    def test_projects_dir_encoding_matches_claude_code(self, cwd, expected):
        assert wd.projects_dir_name(cwd) == expected

    def test_limit_reset_parse_nearest_occurrence(self):
        # NOW = 12:00 UTC
        assert wd.parse_limit_reset_epoch("resets 10am (UTC)", NOW) == NOW - 2 * H
        assert wd.parse_limit_reset_epoch("resets 10pm (UTC)", NOW) == NOW + 10 * H
        assert wd.parse_limit_reset_epoch("resets 5:10pm (UTC)", NOW) == NOW + 5 * H + 10 * MIN
        # Near-midnight wrap: now=23:50 UTC, "12:05am" is 15 min AHEAD, not 24h behind.
        late = NOW + 11 * H + 50 * MIN
        assert wd.parse_limit_reset_epoch("resets 12:05am (UTC)", late) == late + 15 * MIN
        # Paris zone: 12am Europe/Paris on 2026-09-03 (CEST) = 22:00 UTC → 10h ahead of NOW.
        assert wd.parse_limit_reset_epoch("resets 12am (Europe/Paris)", NOW) == NOW + 10 * H
        assert wd.parse_limit_reset_epoch("no reset here", NOW) is None
        assert wd.parse_limit_reset_epoch("resets 9pm (Mars/Olympus)", NOW) is None

    def test_classify_precedence(self):
        assert wd.classify_api_error("Please run /login · API Error: 401 OAuth", NOW)[0] == wd.CLS_AUTH
        assert wd.classify_api_error("You've hit your session limit · resets 5pm (UTC)", NOW)[0] == wd.CLS_LIMIT
        assert wd.classify_api_error("Prompt is too long", NOW)[0] == wd.CLS_CONTEXT
        assert wd.classify_api_error("API Error: 529 Overloaded", NOW)[0] == wd.CLS_TRANSIENT
        assert wd.classify_api_error("Server is temporarily limiting requests", NOW)[0] == wd.CLS_TRANSIENT

    def test_rearm_turn_is_single_line_and_not_a_slash_command(self):
        t = wd.rearm_turn("x", "API Error: 529\nOverloaded\nline3", NOW)
        assert "\n" not in t
        assert not t.startswith("/")
        assert "CronCreate" in t and "STEP A-F" in t and "tick-watchdog" in t

    def test_history_roundtrip_and_corrupt_lines(self, tmp_path):
        p = str(tmp_path / "state" / "wd.jsonl")
        wd.append_event(p, wd.format_event("x", wd.KICK_INJECT, "r", err_ts=NOW, level=1))
        with open(p, "a") as fh:
            fh.write("{not json\n\n")
        evs = wd.read_events(p)
        assert len(evs) == 1 and evs[0]["action"] == wd.KICK_INJECT and evs[0]["err_ts"] == wd.now_iso(NOW)

    def test_observe_reads_only_the_tail_of_a_huge_transcript(self, session):
        pad = [text(NOW - 30 * H, "x" * 5000) for _ in range(300)]   # ~1.5 MB of old junk
        s = session(pad + stalled_tick(NOW - 20 * MIN))
        obs = wd.observe_transcript("x", s, tail_bytes=64 * 1024)
        assert obs.error_text and obs.error_text.startswith("API Error: 529")

    def test_newest_transcript_wins(self, session):
        s = session(stalled_tick(NOW - 5 * H), name="old.jsonl")
        session(healthy_tick(NOW - 1 * H), name="new.jsonl")
        assert run(s).action == wd.OK_IDLE
