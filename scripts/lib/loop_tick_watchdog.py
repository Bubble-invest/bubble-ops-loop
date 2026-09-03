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
  logged in", "Login expired", 401), an explicit usage/QUOTA limit ("hit your
  weekly limit", "usage limit" — with a reset still ahead or no reset at all),
  or a context overflow ("Prompt is too long") — a re-kick cannot fix any of
  these, so we ALERT ONCE PER ERROR EVENT (dedupe key embeds the error
  timestamp; a 13-hour limit-wait is ONE ping, not 26) and do NOT kick. A
  BARE ``rate_limit_error`` / HTTP 429 with no parseable reset time is NOT a
  quota wait — it is the transient throttle of the #724 incident class and
  goes down the kick path (bounded like every other transient). For kickable
  stalls, a per-dept COOLDOWN (default 30 min) plus a rolling-window
  GUARDRAIL (default 3 kicks / 6 h) bound a stall that keeps re-erroring:
  after the cap we stop kicking and escalate to a human.

## Bounds on a permanently-broken dept (read before widening any tunable)

The guardrail window is ROLLING: 3 kicks / 6 h means a dept that errors on
EVERY kick would get up to 12 kicks/day for ever. On top of it sits a HARD
STOP: after ``max_consecutive`` kicks (default 6 = two full windows) with no
healthy turn observed in between, the dept is parked (``hold-hardstop``,
alerted once) until either the watchdog observes it healthy again (which
writes a ``recovered`` marker and resets the count) or a human clears the
state file. So the worst case is 6 kicks total per broken episode, not 12/day.

## Known limitation (FP-A, undecidable from disk, bounded to one tick)

A human CHAT turn (not a tick) that dies on a transient API error while the
loop's CronCreate from an EARLIER tick is still armed in-session looks, on
disk, exactly like a dead tick: the last activity is an error line and THAT
turn armed nothing. The watchdog injects one re-arm tick; the still-armed
cron fires later as well → one extra tick. The re-arm turn tells the dept to
CronList + delete duplicates, and the cooldown/cap bound it to a single
extra tick per event. We accept this over the alternative (never kicking
when any cron might be armed), which would re-open the #724 hole.

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
DEFAULT_MAX_CONSECUTIVE = 6           # hard stop: kicks since the last healthy observation
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
HOLD_HARDSTOP = "hold-hardstop"       # too many kicks with no recovery → parked
HOLD_INJECT_QUEUED = "hold-inject-queued"   # someone's inject line is still queued (fresh)
ALERT_NONTRANSIENT = "alert-nontransient"   # auth / context — a kick can't fix
ALERT_LIMIT_WAIT = "alert-limit-wait"       # usage limit, reset still ahead
ALERT_INJECT_FAILED = "alert-inject-failed" # deaf to inject, restart not allowed
ALERT_HANG = "alert-hang"             # tool call in flight far too long

# History-only markers written by the runner (never a Decision.action):
KICK_FAILED = "kick-failed"           # a recorded kick's side effect did not land
RECOVERED = "recovered"               # dept observed healthy after a kick/hold episode

KICK_ACTIONS = frozenset({KICK_INJECT, KICK_RESTART})
ALERT_ACTIONS = frozenset({HOLD_GUARDRAIL, HOLD_HARDSTOP, ALERT_NONTRANSIENT, ALERT_LIMIT_WAIT,
                           ALERT_INJECT_FAILED, ALERT_HANG})
HEALTHY_ACTIONS = frozenset({OK_IDLE, OK_BUSY, OK_ARMED})   # resets the hard-stop count

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
# An EXPLICIT quota/usage limit — Claude Code's human-readable "You've hit
# your weekly limit · resets 10pm (UTC)" family. Waiting is the only fix.
_QUOTA_RE = re.compile(
    r"hit your (?:weekly|monthly|daily|session)?\s*(?:spend\s*)?limit|usage limit|"
    r"\b(?:weekly|monthly|daily) limit\b",
    re.I,
)
# A BARE rate limit: the raw API 429 ("API Error: 429 {…"rate_limit_error"…}",
# "rate limit exceeded"). Recoverable in minutes → transient UNLESS the text
# also carries a parseable reset time (then it is a quota wait after all).
_RATE_RE = re.compile(r"rate_limit_error|\b429\b|rate[ _-]?limit", re.I)
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

    LIMIT is only the EXPLICIT quota family ("hit your weekly limit",
    "usage limit") or a rate-limit text that carries a parseable reset time.
    A bare ``rate_limit_error`` / 429 with no reset time is TRANSIENT — that
    IS the #724 incident class (a throttle that clears in minutes), and
    treating it as a quota wait would mean never re-kicking it.
    """
    t = text or ""
    if _AUTH_RE.search(t):
        return CLS_AUTH, None
    if _QUOTA_RE.search(t):
        return CLS_LIMIT, parse_limit_reset_epoch(t, now_epoch)
    if _RATE_RE.search(t):
        reset = parse_limit_reset_epoch(t, now_epoch)
        if reset is not None:
            return CLS_LIMIT, reset
        return CLS_TRANSIENT, None
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
    # Inject file (if known) still holds an undrained line (+ its mtime).
    inject_pending: bool = False
    inject_mtime: Optional[float] = None
    # How the transcript was chosen: "pinned" (live session file) | "mtime".
    transcript_pick: str = ""
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
    """Fallback pick: newest top-level ``*.jsonl`` by mtime. On a shared Mac an
    ad-hoc ``claude`` opened in the dept cwd writes here too, so prefer
    :func:`pinned_transcript` and use this only when no live session pins one."""
    files = glob.glob(os.path.join(session_dir, "*.jsonl"))
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _pid_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a).rstrip("/") == os.path.realpath(b).rstrip("/")
    except (OSError, TypeError, ValueError):
        return False


def pinned_transcript(session_dir: str, sessions_dir: str, cwd: str, *,
                      tmux_session: str = "", unit: str = "",
                      pid_alive=_pid_alive, cgroup_of=_pid_cgroup) -> Optional[str]:
    """The transcript of the LIVE dept session, from Claude Code's
    ``~/.claude/sessions/<pid>.json`` registry (``{pid, sessionId, cwd, tmux,
    …}`` on both hosts — verified M4 + VPS 2026-09-03). Candidates: alive pid
    + same cwd as the dept. Tie-break when several sessions share the cwd
    (the reviewer's case — an ad-hoc ``claude`` opened in the dept dir):
    Mac → the one whose ``tmux`` target is the dept's tmux session; VPS → the
    one whose pid sits in the dept's systemd unit cgroup. Among the rest, the
    most recently updated wins. None when nothing pins (caller falls back to
    mtime)."""
    if not sessions_dir or not os.path.isdir(sessions_dir) or not cwd:
        return None
    cands = []
    for f in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        sid, pid, scwd = d.get("sessionId"), d.get("pid"), d.get("cwd") or ""
        if not sid or not isinstance(pid, int) or not _same_dir(scwd, cwd):
            continue
        if not pid_alive(pid):
            continue
        tx = os.path.join(session_dir, f"{sid}.jsonl")
        if not os.path.isfile(tx):
            continue
        cands.append((d, pid, tx))
    if not cands:
        return None
    if tmux_session:
        # Mac: the dept ALWAYS runs inside its own tmux session; a candidate
        # that is not in it is an ad-hoc `claude` — never pin that one.
        cands = [c for c in cands if str(c[0].get("tmux") or "").startswith(f"{tmux_session}:")]
    if unit:
        # VPS: the dept's pid lives in its unit's cgroup. Only filter when
        # /proc is readable for at least one candidate (skipped on macOS).
        known = [(c, cgroup_of(c[1])) for c in cands]
        if any(cg for _, cg in known):
            cands = [c for c, cg in known if unit in cg]
    if not cands:
        return None

    def _stamp(c):
        d = c[0]
        return (d.get("updatedAt") or d.get("startedAt") or 0)
    cands.sort(key=_stamp, reverse=True)
    return cands[0][2]


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
                       inject_file: Optional[str] = None,
                       transcript: Optional[str] = None) -> Observation:
    """Build an Observation from the dept's transcript in ``session_dir``:
    ``transcript`` when given (the runner pins it to the live session via
    :func:`pinned_transcript`), else the newest ``*.jsonl`` by mtime.

    Only the tail is parsed (a live transcript is 10s of MB). Bookkeeping
    lines (system/attachment/…) are ignored for "activity". A turn starts at
    a ``user`` line whose content is a plain string or has no tool_result
    (a cron-fired / injected / human prompt); tool_result user lines belong
    to the running turn.
    """
    obs = Observation(slug=slug)
    if transcript and os.path.isfile(transcript):
        tx: Optional[str] = transcript
        obs.transcript_pick = "pinned"
    else:
        tx = newest_transcript(session_dir)
        obs.transcript_pick = "mtime"
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
            obs.inject_mtime = os.path.getmtime(inject_file) if obs.inject_pending else None
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
    """Append one history line. RAISES (OSError) when the path is not
    writable — the runner relies on that to fail CLOSED: a kick is recorded
    BEFORE its side effect, so an unwritable state file means no kick rather
    than an unrecorded (uncounted, uncooled) one."""
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
    max_consecutive: int = DEFAULT_MAX_CONSECUTIVE   # hard stop (0 = disabled)
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
    dedupe_key: str = ""              # alerts with the same key are sent ONCE (key embeds the error ts)
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
    # deaf → restart (when allowed) else alert. A kick whose side effect
    # FAILED (kick-failed marker after it) proves nothing about deafness.
    prior = _events_for(history, slug, KICK_ACTIONS, now_epoch)
    last_kick = prior[-1][1] if prior else None
    failed = _events_for(history, slug, {KICK_FAILED}, now_epoch)
    last_kick_failed = bool(prior and failed and failed[-1][0] >= prior[-1][0])
    same_err_inject = (
        last_kick is not None and last_kick.get("action") == KICK_INJECT
        and last_kick.get("err_ts") == now_iso(err_ts) and not last_kick_failed
    )
    if same_err_inject:
        if cfg.restart_allowed:
            d = Decision(KICK_RESTART, f"inject at {last_kick.get('ts')} was never processed (same error line still last) — session deaf; restarting runtime (resumes context)",
                         level=2, err_class=cls, notify=True, facts=facts)
        else:
            return Decision(ALERT_INJECT_FAILED, f"inject at {last_kick.get('ts')} was never processed and restart is not allowed for {slug} — human needed",
                            err_class=cls, notify=True, dedupe_key=f"inject-failed:{err_ts}", facts=facts)
    elif obs.inject_pending:
        # Never stack a second line behind an undrained one: the plugin drains
        # the whole file on its next read/startup → two back-to-back ticks.
        # A fresh line (e.g. loop-backup's inject_live_loop seconds ago) is
        # simply someone else's kick in flight; a stale one means the poller
        # is not delivering (same signal as the no-error branch).
        inj_age = int(now_epoch - obs.inject_mtime) if obs.inject_mtime else None
        if inj_age is not None and inj_age < cfg.stall_idle_sec:
            return Decision(HOLD_INJECT_QUEUED, f"an inject line is already queued ({inj_age}s old) — not stacking a second tick",
                            err_class=cls, facts=facts)
        return Decision(ALERT_INJECT_FAILED, f"inject file still undrained ({inj_age}s) with the loop stalled — poller not delivering; human needed",
                        err_class=cls, notify=True, dedupe_key=f"undrained:{err_ts}", facts=facts)
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
    # Hard stop (see module docstring "Bounds"): the rolling window alone
    # allows 12 kicks/day for ever on a permanently-broken dept.
    consecutive = kicks_since_recovery(history, slug, now_epoch)
    if cfg.max_consecutive and consecutive >= cfg.max_consecutive:
        return Decision(HOLD_HARDSTOP, f"{consecutive} kicks since the last healthy observation (hard stop {cfg.max_consecutive}) — parked until the dept is seen healthy or the state file is cleared; human needed",
                        err_class=d.err_class, notify=True, dedupe_key=f"hardstop:{kicks[-1][1].get('ts') if kicks else err_ts}", facts=d.facts)
    in_window = _events_for(history, slug, KICK_ACTIONS, now_epoch, cfg.window_sec)
    if len(in_window) >= cfg.max_kicks:
        return Decision(HOLD_GUARDRAIL, f"{len(in_window)} kicks in the last {cfg.window_sec // 3600}h (cap {cfg.max_kicks}) — stall keeps recurring; NOT kicking again, human needed",
                        err_class=d.err_class, notify=True, dedupe_key=f"guardrail:{err_ts}", facts=d.facts)
    d.facts["kicks_in_window"] = len(in_window)
    d.facts["kicks_since_recovery"] = consecutive
    return d


def kicks_since_recovery(history: List[dict], slug: str, now_epoch: float) -> int:
    """Number of kicks for ``slug`` after its most recent ``recovered`` marker
    (all kicks when there is none)."""
    rec = _events_for(history, slug, {RECOVERED}, now_epoch)
    since = rec[-1][0] if rec else None
    return sum(1 for ep, _ in _events_for(history, slug, KICK_ACTIONS, now_epoch)
               if since is None or ep > since)


def needs_recovery_marker(history: List[dict], slug: str, action: str, now_epoch: float) -> bool:
    """True when the dept is observed HEALTHY and its last recorded history
    event is a kick / hold (an episode is open) — the runner then appends one
    ``recovered`` marker, which resets the hard-stop count. Idempotent: a
    second healthy pass sees ``recovered`` as the last event and writes nothing."""
    if action not in HEALTHY_ACTIONS:
        return False
    evs = _events_for(history, slug, KICK_ACTIONS | ALERT_ACTIONS | {RECOVERED, KICK_FAILED}, now_epoch)
    if not evs:
        return False
    return evs[-1][1].get("action") != RECOVERED


def already_alerted(history: List[dict], slug: str, dedupe_key: str, now_epoch: float,
                    cooldown_sec: Optional[int] = None) -> bool:
    """True when an alert with this dedupe key has EVER gone out (the key
    embeds the underlying error timestamp, so it self-resets when the error
    line changes). Alerting once per cooldown instead re-pinged every 30 min
    for the whole of a 13-hour weekly-limit wait (fleet 2026-08-27: ~26
    pings × 3 depts). ``cooldown_sec`` is accepted for API compatibility and
    ignored."""
    for ep, ev in _events_for(history, slug, ALERT_ACTIONS, now_epoch):
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
