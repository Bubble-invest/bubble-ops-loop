"""loop_tick_watchdog.py — re-kick a dept loop whose TICK died mid-flight (#724).

Incident (Miranda/M1, 2026-07-21): a self-paced /loop tick hit a transient
Anthropic API error ("Server is temporarily limiting requests"). Claude Code
does NOT retry — the turn ENDS on a synthetic assistant message (verified on
every live transcript in the fleet: an ``assistant`` line with
``isApiErrorMessage: true`` / ``model: "<synthetic>"`` immediately followed by
a ``system`` ``turn_duration`` line, then NOTHING for 51–125 min). Because the
dept arms its OWN next wake with a CronCreate at the END of the tick, a tick
that dies mid-flight never arms one: the session is alive + idle at the
prompt, systemd/launchd KeepAlive sees a healthy process, and the loop is
silently dead until a human or the (hours-later) layer floor intervenes.

This module is the PURE decision (mirrors loop_backup.py / auto_restart.py):
transcript observation → a single ``Decision``; the runner
(scripts/loop-tick-watchdog.py) does the side effects (inject / restart /
notify) and is host-aware (VPS systemd vs Mac launchd).

## How stalled ≠ idle ≠ crash-loop

* **STALLED (act):** the LAST activity in the newest transcript is an
  assistant API-error line (``isApiErrorMessage: true``), the transcript has
  been idle ≥ ``stall_idle_sec`` (default 10 min), no subagent transcript is
  being written, no tool call is in flight, and the SAME turn did not already
  arm a CronCreate. The error text classifies as TRANSIENT (overloaded / 5xx /
  rate-limit / network / timeout) or as a USAGE LIMIT whose reset time has
  already passed.
* **HEALTHY IDLE (never touch):** the last activity is a normal assistant
  turn (the tick finished and armed its wake) — however long ago. A self-paced
  loop legitimately idles for hours (until tomorrow 08:03 Paris). We NEVER
  key on "idle too long" alone — that is exactly the false-kick Rick's triage
  warned about. Also healthy: a synthetic "No response requested." (NOT an
  error — ``isApiErrorMessage: false``), an error that is still fresh (< the
  idle threshold; a recurring cron may re-fire on its own), a busy turn
  (subagent activity / tool call in flight), an error in a PREVIOUS turn
  followed by later healthy activity.
* **CRASH-LOOP / NOT-KICKABLE (never made worse):** an AUTH failure ("Not
  logged in", "Login expired", 401), a usage LIMIT whose reset is still ahead
  (or unparseable), or a context overflow ("Prompt is too long") — a re-kick
  cannot fix any of these, so we ALERT ONCE (deduped per cooldown) and do NOT
  kick. For kickable stalls, a per-dept COOLDOWN (default 30 min) plus a
  rolling-window GUARDRAIL (default 3 kicks / 6 h) bound a stall that keeps
  re-erroring: after the cap we stop kicking and escalate to a human.

## The re-kick ladder (why it cannot double-launch)

Level 1 — INJECT: append ONE line to the dept's telegram-plugin ``inject``
file (the bubble-inject patch delivers it into the RUNNING session as a turn,
same path as bubble-loop-now / loop-backup.sh's inject_live_loop). No new
process, no second poller, no context loss. This is the only action a stall
gets by default.

Level 2 — RESTART: ONLY if the previous kick was an inject for the SAME error
event and the transcript did not advance at all since (the session is deaf to
the inject — a wedged bridge), the runner may restart the runtime (VPS:
``systemctl stop → start``; Mac: ``tmux kill-session`` → KeepAlive relaunch),
and only when the runtime resumes context (``--continue`` present). A restart
is one process replacing itself under the SAME supervisor — never a second
concurrent session. The runner also honours the VPS telegram-watchdog's own
cooldown mark so the two watchdogs never both restart the same dept.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── Tunables (defaults; the runner overrides from env) ───────────────────────
DEFAULT_STALL_IDLE_SEC = 600          # error must be ≥10 min old before we act
DEFAULT_BUSY_GRACE_SEC = 900          # subagent/tool activity within 15 min = busy
DEFAULT_HANG_SEC = 45 * 60            # tool_use with no result for 45 min = hang (alert-only)
DEFAULT_COOLDOWN_SEC = 1800           # ≥30 min between kicks of the same dept
DEFAULT_MAX_KICKS = 3                 # kicks allowed per rolling window …
DEFAULT_WINDOW_SEC = 6 * 3600         # … of 6 h; the next one escalates instead
DEFAULT_TAIL_BYTES = 1 << 20          # scan the last 1 MiB of the transcript

# ── Actions (the runner switches on these) ───────────────────────────────────
OK_NO_TRANSCRIPT = "ok-no-transcript"
OK_IDLE = "ok-idle"                   # healthy: last turn ended normally
OK_FRESH = "ok-fresh"                 # error too recent — give it a chance
OK_BUSY = "ok-busy"                   # a turn is in flight (subagent/tool)
OK_ARMED = "ok-armed"                 # same turn already armed a CronCreate
KICK_INJECT = "kick-inject"           # level 1
KICK_RESTART = "kick-restart"         # level 2 (inject proved deaf)
HOLD_COOLDOWN = "hold-cooldown"
HOLD_GUARDRAIL = "hold-guardrail"     # cap reached → escalate to a human
ALERT_NONTRANSIENT = "alert-nontransient"   # auth / context — a kick can't fix
ALERT_LIMIT_WAIT = "alert-limit-wait"       # usage limit, reset still ahead
ALERT_INJECT_FAILED = "alert-inject-failed" # deaf to inject, restart not allowed
ALERT_HANG = "alert-hang"             # tool call in flight far too long

KICK_ACTIONS = frozenset({KICK_INJECT, KICK_RESTART})
ALERT_ACTIONS = frozenset({HOLD_GUARDRAIL, ALERT_NONTRANSIENT, ALERT_LIMIT_WAIT,
                           ALERT_INJECT_FAILED, ALERT_HANG})

# ── Error classification ─────────────────────────────────────────────────────
CLS_TRANSIENT = "transient"
CLS_AUTH = "auth"
CLS_LIMIT = "limit"
CLS_CONTEXT = "context"

# Verified against real fleet transcripts (VPS ben/maya/tony, M1 content,
# M5 accountant, M4 rnd — 2026-07..09). Order matters: auth/limit/context are
# checked BEFORE the transient fallback so "Please run /login · API Error: 401"
# is auth, not transient.
_AUTH_RE = re.compile(
    r"not logged in|please run /login|please run `/login`|login expired|"
    r"\b401\b|oauth access token|authentication credentials|authentication_error",
    re.I,
)
_LIMIT_RE = re.compile(
    r"hit your (?:weekly|monthly|daily|session)?\s*(?:spend\s*)?limit|usage limit|rate_limit_error",
    re.I,
)
_CONTEXT_RE = re.compile(r"prompt is too long|context window|too many tokens", re.I)
# "resets 10pm (UTC)" / "resets 12am (Europe/Paris)" / "resets 5:10pm (UTC)"
_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([A-Za-z_/]+)\)", re.I
)

# Transcript line types that are pure bookkeeping — never "activity".
_BOOKKEEPING_TYPES = frozenset({
    "system", "attachment", "last-prompt", "mode", "permission-mode",
    "atis-latch", "frame-link", "pr-link", "summary", "file-history-snapshot",
})


def projects_dir_name(cwd: str) -> str:
    """Claude Code's ``~/.claude/projects/<name>`` encoding of a cwd: every
    non-alphanumeric char → '-'. Verified: ``/Users/joris/claude-workspaces/Rick_RnD``
    → ``-Users-joris-claude-workspaces-Rick-RnD``; ``/Users/joris/.hermes-scripts``
    → ``-Users-joris--hermes-scripts``; ``/home/claude/agents/bubble-ops-ben`` →
    ``-home-claude-agents-bubble-ops-ben``."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _parse_iso(ts: str) -> Optional[float]:
    try:
        raw = ts.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def now_iso(epoch: Optional[float] = None) -> str:
    dt = _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc) if epoch is not None \
        else _dt.datetime.now(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Usage-limit reset parsing ────────────────────────────────────────────────
def parse_limit_reset_epoch(text: str, now_epoch: float) -> Optional[float]:
    """Epoch of the "resets <H[:MM]><am|pm> (<zone>)" occurrence NEAREST to
    now (nearest-occurrence rule, mirrors the VPS telegram-watchdog Signal 7
    wrap-around fix: near midnight the naive "today" parse is ~24h off).
    None when absent/unparseable — the caller treats that as "reset still
    ahead" (conservative: never kick into a still-active limit)."""
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour, minute, ampm, zone = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower(), m.group(4)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    try:
        if zone.upper() == "UTC":
            tz = _dt.timezone.utc
        else:
            from zoneinfo import ZoneInfo  # py3.9+; system tzdata on Mac/Linux
            tz = ZoneInfo(zone)
    except Exception:
        return None
    now_local = _dt.datetime.fromtimestamp(now_epoch, tz)
    cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Nearest occurrence: shift by a day when more than 12h off.
    if cand.timestamp() - now_epoch > 12 * 3600:
        cand -= _dt.timedelta(days=1)
    elif now_epoch - cand.timestamp() > 12 * 3600:
        cand += _dt.timedelta(days=1)
    return cand.timestamp()


def classify_api_error(text: str, now_epoch: float) -> Tuple[str, Optional[float]]:
    """Classify a synthetic API-error assistant text.

    Returns (class, reset_epoch). ``reset_epoch`` is only set for CLS_LIMIT
    when a "resets …" time parsed. Everything that is not auth / limit /
    context is TRANSIENT (529 Overloaded, 5xx, "temporarily limiting",
    ENOTFOUND, ConnectionRefused, ECONNRESET, "Request timed out", "Connection
    closed mid-response", "went to sleep mid-response", "Server exited
    unexpectedly", unknown) — the class this card exists for. An unknown text
    is deliberately treated as transient: a kick is cheap and bounded by the
    cooldown + guardrail, and the alert carries the text for a human.
    """
    t = text or ""
    if _AUTH_RE.search(t):
        return CLS_AUTH, None
    if _LIMIT_RE.search(t):
        return CLS_LIMIT, parse_limit_reset_epoch(t, now_epoch)
    if _CONTEXT_RE.search(t):
        return CLS_CONTEXT, None
    return CLS_TRANSIENT, None


# ── Transcript observation ───────────────────────────────────────────────────
@dataclass
class Observation:
    slug: str
    transcript: Optional[str] = None
    transcript_mtime: Optional[float] = None
    last_activity_ts: Optional[float] = None
    # The stall signal: last activity is an assistant API-error line.
    error_text: Optional[str] = None
    error_ts: Optional[float] = None
    error_line_uuid: Optional[str] = None
    # Same-turn CronCreate already succeeded (tick armed its wake before dying).
    armed_in_turn: bool = False
    # A tool_use with no matching tool_result yet (turn in flight / hang).
    pending_tool: Optional[str] = None
    pending_tool_ts: Optional[float] = None
    # Newest subagent transcript mtime under <session>/subagents/.
    subagent_mtime: Optional[float] = None
    # Inject file (if known) still holds an undrained line.
    inject_pending: bool = False
    parse_errors: int = 0


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            x.get("text", "") for x in content
            if isinstance(x, dict) and x.get("type") == "text"
        )
    return ""


def _read_tail_lines(path: str, tail_bytes: int) -> List[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()  # drop the partial first line
        data = fh.read()
    return data.decode("utf-8", errors="replace").splitlines()


def newest_transcript(session_dir: str) -> Optional[str]:
    files = glob.glob(os.path.join(session_dir, "*.jsonl"))
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except OSError:
        return None


def newest_subagent_mtime(transcript: str) -> Optional[float]:
    sub_dir = os.path.join(transcript[:-len(".jsonl")], "subagents") \
        if transcript.endswith(".jsonl") else None
    if not sub_dir or not os.path.isdir(sub_dir):
        return None
    best: Optional[float] = None
    for f in glob.glob(os.path.join(sub_dir, "*.jsonl")):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def observe_transcript(slug: str, session_dir: str, *,
                       tail_bytes: int = DEFAULT_TAIL_BYTES,
                       inject_file: Optional[str] = None) -> Observation:
    """Build an Observation from the newest transcript in ``session_dir``.

    Only the tail is parsed (a live transcript is 10s of MB). Bookkeeping
    lines (system/attachment/…) are ignored for "activity". A turn starts at
    a ``user`` line whose content is a plain string or has no tool_result
    (a cron-fired / injected / human prompt); tool_result user lines belong
    to the running turn.
    """
    obs = Observation(slug=slug)
    tx = newest_transcript(session_dir)
    if tx is None:
        return obs
    obs.transcript = tx
    try:
        obs.transcript_mtime = os.path.getmtime(tx)
        lines = _read_tail_lines(tx, tail_bytes)
    except OSError:
        obs.transcript = None
        return obs
    obs.subagent_mtime = newest_subagent_mtime(tx)
    if inject_file:
        try:
            obs.inject_pending = os.path.getsize(inject_file) > 0
        except OSError:
            obs.inject_pending = False

    rows: List[dict] = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        try:
            d = json.loads(l)
        except ValueError:
            obs.parse_errors += 1
            continue
        if isinstance(d, dict):
            rows.append(d)

    # Index of the last activity row and of the current turn's start.
    last_idx: Optional[int] = None
    for i in range(len(rows) - 1, -1, -1):
        t = rows[i].get("type")
        if t in _BOOKKEEPING_TYPES or t is None:
            continue
        if t == "queue-operation":
            # An enqueue is a pending inbound (activity); a dequeue is not.
            if rows[i].get("operation") == "enqueue":
                last_idx = i
                break
            continue
        if t in ("user", "assistant"):
            last_idx = i
            break
    if last_idx is None:
        return obs
    last = rows[last_idx]
    obs.last_activity_ts = _parse_iso(last.get("timestamp", "")) or obs.transcript_mtime

    msg = last.get("message") or {}
    content = msg.get("content")
    if last.get("type") == "assistant" and last.get("isApiErrorMessage") is True:
        obs.error_text = _text_of(content).strip()
        obs.error_ts = obs.last_activity_ts
        obs.error_line_uuid = last.get("uuid")

    # Turn boundary: walk back to the last prompt-style user line.
    turn_start = 0
    for i in range(last_idx, -1, -1):
        r = rows[i]
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        is_tool_result = isinstance(c, list) and any(
            isinstance(x, dict) and x.get("type") == "tool_result" for x in c)
        if not is_tool_result:
            turn_start = i
            break

    # Within the turn: successful CronCreate? pending tool_use?
    tool_uses = {}   # id -> (name, ts)
    results = set()
    for r in rows[turn_start:last_idx + 1]:
        c = (r.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for x in c:
            if not isinstance(x, dict):
                continue
            if r.get("type") == "assistant" and x.get("type") == "tool_use":
                tool_uses[x.get("id")] = (x.get("name", ""), _parse_iso(r.get("timestamp", "")))
            elif r.get("type") == "user" and x.get("type") == "tool_result":
                if not x.get("is_error"):
                    results.add(x.get("tool_use_id"))
    for tid, (name, ts) in tool_uses.items():
        if name == "CronCreate" and tid in results:
            obs.armed_in_turn = True
    # The LAST activity being an assistant tool_use with no result = in flight.
    if last.get("type") == "assistant" and isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("id") not in results:
                obs.pending_tool = x.get("name", "?")
                obs.pending_tool_ts = obs.last_activity_ts
    return obs


# ── History (append-only JSONL, mirrors auto_restart.py) ────────────────────
def format_event(slug: str, action: str, reason: str, *, err_ts: Optional[float] = None,
                 err_text: str = "", level: int = 0, ts: Optional[str] = None,
                 extra: Optional[dict] = None) -> dict:
    ev = {
        "ts": ts or now_iso(),
        "slug": slug,
        "action": action,
        "reason": reason[:400],
        "level": level,
    }
    if err_ts is not None:
        ev["err_ts"] = now_iso(err_ts)
    if err_text:
        ev["err_text"] = err_text[:160]
    if extra:
        ev.update(extra)
    return ev


def append_event(path: str, event: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        return []
    return out


def _events_for(history: List[dict], slug: str, actions, now_epoch: float,
                window_sec: Optional[int] = None) -> List[Tuple[float, dict]]:
    out = []
    for ev in history:
        if ev.get("slug") != slug or ev.get("action") not in actions:
            continue
        ep = _parse_iso(ev.get("ts", ""))
        if ep is None or ep > now_epoch:
            continue
        if window_sec is not None and ep < now_epoch - window_sec:
            continue
        out.append((ep, ev))
    out.sort(key=lambda p: p[0])
    return out


# ── The decision ─────────────────────────────────────────────────────────────
@dataclass
class Config:
    stall_idle_sec: int = DEFAULT_STALL_IDLE_SEC
    busy_grace_sec: int = DEFAULT_BUSY_GRACE_SEC
    hang_sec: int = DEFAULT_HANG_SEC
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    max_kicks: int = DEFAULT_MAX_KICKS
    window_sec: int = DEFAULT_WINDOW_SEC
    restart_allowed: bool = False     # runtime resumes context + not a concierge
    hang_restart: bool = False        # hang → restart instead of alert (opt-in)
    external_restart_epoch: Optional[float] = None  # e.g. telegram-watchdog's mark


@dataclass
class Decision:
    action: str
    reason: str
    level: int = 0
    err_class: Optional[str] = None
    notify: bool = False              # runner should tell a human
    dedupe_key: str = ""              # alerts with the same key are sent once per cooldown
    facts: dict = field(default_factory=dict)

    @property
    def is_kick(self) -> bool:
        return self.action in KICK_ACTIONS


def decide(obs: Observation, history: List[dict], now_epoch: float, cfg: Config) -> Decision:
    """Pure decision. See the module docstring for the three-way split."""
    slug = obs.slug
    if obs.transcript is None or obs.last_activity_ts is None:
        return Decision(OK_NO_TRANSCRIPT, "no transcript activity found — nothing to judge")

    mtime = obs.transcript_mtime or obs.last_activity_ts
    idle = int(now_epoch - max(mtime, obs.last_activity_ts))
    facts = {"idle_sec": idle, "transcript": os.path.basename(obs.transcript)}

    # Busy: a subagent transcript is being written, or the turn is in flight.
    if obs.subagent_mtime is not None and now_epoch - obs.subagent_mtime <= cfg.busy_grace_sec:
        return Decision(OK_BUSY, f"subagent transcript active {int(now_epoch - obs.subagent_mtime)}s ago — turn in flight", facts=facts)

    # ── No error at the tail ────────────────────────────────────────────────
    if obs.error_text is None:
        if obs.pending_tool is not None:
            if idle <= cfg.busy_grace_sec:
                return Decision(OK_BUSY, f"tool_use {obs.pending_tool} in flight ({idle}s)", facts=facts)
            if idle >= cfg.hang_sec:
                act = KICK_RESTART if (cfg.hang_restart and cfg.restart_allowed) else ALERT_HANG
                d = Decision(act, f"tool_use {obs.pending_tool} has had no result for {idle}s (≥{cfg.hang_sec}s) — hung turn",
                             level=2 if act == KICK_RESTART else 0, notify=True,
                             dedupe_key=f"hang:{obs.last_activity_ts}", facts=facts)
                if act == KICK_RESTART:
                    return _apply_kick_guards(d, obs, history, now_epoch, cfg, err_ts=obs.last_activity_ts)
                return d
            return Decision(OK_BUSY, f"tool_use {obs.pending_tool} pending {idle}s (< hang threshold)", facts=facts)
        if obs.inject_pending and idle >= cfg.stall_idle_sec:
            # Someone (maybe us, last tick) injected and the plugin never drained it.
            d = Decision(ALERT_INJECT_FAILED, f"inject file still undrained after {idle}s idle — poller not delivering",
                         notify=True, dedupe_key=f"undrained:{obs.last_activity_ts}", facts=facts)
            return d
        return Decision(OK_IDLE, f"last turn ended normally; idle {idle}s is fine for a self-paced loop", facts=facts)

    # ── Tail IS an API error ────────────────────────────────────────────────
    err_ts = obs.error_ts or obs.last_activity_ts
    cls, reset_epoch = classify_api_error(obs.error_text, now_epoch)
    facts.update({"err_text": obs.error_text[:160], "err_class": cls, "err_age_sec": int(now_epoch - err_ts)})
    if idle < cfg.stall_idle_sec:
        return Decision(OK_FRESH, f"API error {idle}s ago (< {cfg.stall_idle_sec}s) — giving the loop/cron a chance", err_class=cls, facts=facts)
    if obs.armed_in_turn:
        return Decision(OK_ARMED, "the same turn already armed a CronCreate before the error — the loop will wake itself", err_class=cls, facts=facts)

    if cls in (CLS_AUTH, CLS_CONTEXT):
        return Decision(ALERT_NONTRANSIENT, f"{cls} failure ({obs.error_text[:80]!r}) — a re-kick cannot fix this; human needed",
                        err_class=cls, notify=True, dedupe_key=f"{cls}:{err_ts}", facts=facts)
    if cls == CLS_LIMIT:
        if reset_epoch is None or reset_epoch > now_epoch:
            when = now_iso(reset_epoch) if reset_epoch else "unparseable"
            return Decision(ALERT_LIMIT_WAIT, f"usage limit ({obs.error_text[:80]!r}); reset {when} still ahead — waiting, not kicking",
                            err_class=cls, notify=True, dedupe_key=f"limit:{err_ts}", facts=facts)
        facts["limit_reset_passed"] = now_iso(reset_epoch)

    # Kickable stall. Ladder: inject; if the previous inject for THIS error
    # landed nothing (transcript unchanged, same error line), the session is
    # deaf → restart (when allowed) else alert.
    prior = _events_for(history, slug, KICK_ACTIONS, now_epoch)
    last_kick = prior[-1][1] if prior else None
    same_err_inject = (
        last_kick is not None and last_kick.get("action") == KICK_INJECT
        and last_kick.get("err_ts") == now_iso(err_ts)
    )
    if same_err_inject:
        if cfg.restart_allowed:
            d = Decision(KICK_RESTART, f"inject at {last_kick.get('ts')} was never processed (same error line still last) — session deaf; restarting runtime (resumes context)",
                         level=2, err_class=cls, notify=True, facts=facts)
        else:
            return Decision(ALERT_INJECT_FAILED, f"inject at {last_kick.get('ts')} was never processed and restart is not allowed for {slug} — human needed",
                            err_class=cls, notify=True, dedupe_key=f"inject-failed:{err_ts}", facts=facts)
    else:
        d = Decision(KICK_INJECT, f"tick died on a {cls} API error {int(now_epoch - err_ts)}s ago and no wake was armed — injecting a re-arm turn",
                     level=1, err_class=cls, notify=True, facts=facts)
    return _apply_kick_guards(d, obs, history, now_epoch, cfg, err_ts=err_ts)


def _apply_kick_guards(d: Decision, obs: Observation, history: List[dict], now_epoch: float,
                       cfg: Config, *, err_ts: float) -> Decision:
    """Cooldown + rolling guardrail + external-restart mark, applied to any kick."""
    slug = obs.slug
    kicks = _events_for(history, slug, KICK_ACTIONS, now_epoch)
    last_ts = kicks[-1][0] if kicks else None
    if cfg.external_restart_epoch is not None and cfg.external_restart_epoch <= now_epoch:
        last_ts = max(last_ts or 0, cfg.external_restart_epoch)
    if last_ts is not None and now_epoch - last_ts < cfg.cooldown_sec:
        return Decision(HOLD_COOLDOWN, f"would {d.action} but last kick/restart was {int(now_epoch - last_ts)}s ago (< {cfg.cooldown_sec}s cooldown)",
                        err_class=d.err_class, facts=d.facts)
    in_window = _events_for(history, slug, KICK_ACTIONS, now_epoch, cfg.window_sec)
    if len(in_window) >= cfg.max_kicks:
        return Decision(HOLD_GUARDRAIL, f"{len(in_window)} kicks in the last {cfg.window_sec // 3600}h (cap {cfg.max_kicks}) — stall keeps recurring; NOT kicking again, human needed",
                        err_class=d.err_class, notify=True, dedupe_key=f"guardrail:{err_ts}", facts=d.facts)
    d.facts["kicks_in_window"] = len(in_window)
    return d


def already_alerted(history: List[dict], slug: str, dedupe_key: str, now_epoch: float,
                    cooldown_sec: int) -> bool:
    """True when an alert with this dedupe key went out within the cooldown."""
    for ep, ev in _events_for(history, slug, ALERT_ACTIONS, now_epoch, cooldown_sec):
        if ev.get("dedupe_key") == dedupe_key:
            return True
    return False


# ── The re-arm turn (single line — the inject drain splits on '\n') ─────────
def rearm_turn(slug: str, err_text: str, err_ts: float) -> str:
    """The ONE-LINE turn appended to <TELEGRAM_STATE_DIR>/inject. Must never
    contain a newline (each line is delivered as a separate turn) and must
    never start with '/' (a bare slash-command is eaten as Unknown command).
    Wording mirrors boot_rearm.ts / loop-backup.sh inject_live_loop."""
    err = " ".join((err_text or "").split())[:120]
    text = (
        f"[tick-watchdog] Your previous loop tick died at {now_iso(err_ts)} on a transient API error "
        f"({err}) and no next wake was armed since. This is a system signal, not an operator instruction. "
        "Run ONE normal tick now (STEP A-F per CLAUDE.md), then arm your OWN next wake with a single CronCreate "
        "(run CronList first and delete any stale/duplicate loop task so you never stack two). "
        "The box clock is UTC, not Paris: for any Paris-anchored target derive the box-UTC cron via "
        "scripts/arm-wake-cron.sh Paris-HH:MM [daily|one-shot]. The CronCreate prompt must be your full tick "
        "protocol (STEP A-F), never a bare slash-command. Do not reply to a human; just resume cadence."
    )
    return text.replace("\n", " ")
