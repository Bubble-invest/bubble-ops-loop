#!/usr/bin/env python3
"""Fail-closed fleet idle compaction (Python 3.9+, stdlib only)."""

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


METADATA = {"mode", "permission-mode", "atis-latch", "last-prompt", "pr-link",
            "cost-state", "file-history-snapshot", "file-history-delta"}
# Machine-generated user turns: channel/command wrappers, background-task results and
# harness notices (seen in Rick's real transcript 2026-10-02) are not human activity.
WRAPPERS = ("<channel", "<local-command", "<command-name", "<command-message",
            "<command-args", "<task-notification", "<system-reminder", "[SYSTEM NOTIFICATION")
MACHINE_WAKE_PATTERNS = (
    r"^Resume\s+.+?\s+OODA\s+loop\b", r"\bDUE_MISSIONS=",
    r"\bMEETING POLL\b", r"^\[session-rotate,\s*automated maintenance\b",
)


class Unsafe(Exception):
    """A fixed, content-free reason to skip."""


@dataclass
class Config:
    tmux_session: str
    transcript_dir: Path
    ledger: Path
    state: Path
    log: Path
    idle_min: float = 55
    min_context: int = 200000
    quiet_sec: float = 120
    dry_run: bool = False
    human_user_ids: tuple = ("6532205130", "7470271615")
    recheck_delay: float = 1.5
    tmux_bin: str = "tmux"
    meeting_marker: Path = None
    harness_selector: Path = None
    machine_wake_patterns: tuple = ()  # Per-agent additions to built-in recognisers.


def check_declarations(config, now):
    if not isinstance(config.machine_wake_patterns, (list, tuple)):
        raise Unsafe("invalid_machine_wake_patterns")
    for pattern in config.machine_wake_patterns:
        try:
            if not isinstance(pattern, str) or not pattern.strip() or re.search(pattern, "", re.IGNORECASE):
                raise Unsafe("invalid_machine_wake_patterns")
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            raise Unsafe("invalid_machine_wake_patterns") from None
    if config.harness_selector and os.path.lexists(config.harness_selector):
        harness = config.harness_selector.read_text().strip() or "claude"
        if harness == "hermes":
            raise Unsafe("hermes_harness_skipped")
        if harness != "claude":
            raise Unsafe("unknown_harness")
    if config.meeting_marker and os.path.lexists(config.meeting_marker):
        raise Unsafe("meeting_poll_declared")


def check_clients(config, runner, now):
    # client_activity is tmux's last activity timestamp, including unsent keystrokes.
    # Unknown/unsupported formats fail closed; output activity can only delay us.
    raw = runner([config.tmux_bin, "list-clients", "-t", "=" + config.tmux_session,
                  "-F", "#{client_activity}"])
    for line in raw.splitlines():
        if not line.isdigit() or int(line) > now or int(line) <= 0:
            raise Unsafe("invalid_client_activity")
        if now - int(line) < config.idle_min * 60:
            raise Unsafe("attached_client_recent")


def target_pane(config, runner):
    # Exact session lookup and one pane only: never send into an arbitrary selected pane.
    try:
        raw = runner([config.tmux_bin, "list-panes", "-s", "-t", "=" + config.tmux_session,
                      "-F", "#{pane_id} #{pane_current_command}"])
    except subprocess.CalledProcessError as exc:
        error = exc.stderr or ""
        if re.search(r"can't find session:|no server running on|error connecting to .*No such file", error):
            raise Unsafe("no_tmux_session") from None
        raise
    lines = raw.splitlines()
    if not lines:
        raise Unsafe("no_tmux_session")
    if len(lines) != 1 or not re.fullmatch(r"%[0-9]+ (?:claude|\d+\.\d+\.\d+)", lines[0]):
        raise Unsafe("no_dedicated_claude_pane")
    return lines[0].split()[0]


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value, now):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        result = parsed.timestamp()
        if not math.isfinite(result) or result > now:
            raise ValueError()
        return result
    except (ValueError, OverflowError):
        raise Unsafe("invalid_or_future_timestamp") from None


def fingerprint(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def rows(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise Unsafe("malformed_jsonl") from None
            if not isinstance(row, dict):
                raise Unsafe("invalid_jsonl_record")
            yield row


def human_text(row, extra_patterns=()):
    message = row.get("message")
    if not isinstance(message, dict):
        raise Unsafe("invalid_user_message")
    if row.get("isCompactSummary") is True:
        return False
    for obj in (row, message):
        source = obj.get("source", "")
        if not isinstance(source, str):
            raise Unsafe("invalid_message_source")
        if "bubble-inject" in source.lower() or "ops-loop-boot-rearm" in source.lower():
            return False
    content = message.get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                raise Unsafe("invalid_content_block")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise Unsafe("invalid_text_block")
                texts.append(block["text"])
            elif block.get("type") != "tool_result":
                # Images alone cannot establish whether a terminal user was active.
                raise Unsafe("unknown_user_content")
    else:
        raise Unsafe("invalid_user_content")
    def machine(text):
        return (text.lstrip().lower().startswith(tuple(prefix.lower() for prefix in WRAPPERS)) or any(
            re.search(pattern, text.lstrip(), re.IGNORECASE)
            for pattern in (*MACHINE_WAKE_PATTERNS, *extra_patterns)))
    joined = "".join(texts)
    # Bounded envelopes may span text blocks. Preserve human text outside them.
    outside = re.sub(r"<(channel|task-notification|system-reminder|local-command[\w-]*|command[\w-]*)\b[^>]*>.*?</\1\s*>",
                     "", joined, flags=re.IGNORECASE | re.DOTALL)
    if outside != joined:
        return bool(outside.strip()) and not machine(outside)
    # Separate human text blocks still reset idle even beside a machine notice.
    # Joined text recognises wrappers split across blocks.
    candidates = [text for text in texts if text.strip() and not machine(text)]
    return bool(candidates) and not machine("".join(candidates))


def check_meeting_record(row, now):
    message = row.get("message", {})
    if "MEETING POLL" not in json.dumps(message):
        return
    content = message.get("content", []) if isinstance(message, dict) else []
    if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use" and
            block.get("name") == "CronCreate" and "MEETING POLL" in json.dumps(block.get("input", {}))
            for block in content):
        # A creation declaration is not evidence of its later deletion. Without
        # a proven CronDelete result format, keep this transcript blocked.
        raise Unsafe("meeting_poll_declared")
    if now - timestamp(row.get("timestamp"), now) < 10800:
        raise Unsafe("meeting_poll_declared")


def inspect_inputs(config, now, previous_send=None):
    files = list(config.transcript_dir.glob("*.jsonl"))
    if not files:
        raise Unsafe("no_transcript")
    stamps = {path: fingerprint(path) for path in files}
    latest_mtime = max(stamp[3] for stamp in stamps.values())
    newest = [path for path in files if stamps[path][3] == latest_mtime]
    if len(newest) != 1:
        raise Unsafe("ambiguous_newest_transcript")
    latest = newest[0]
    ledger_stamp = fingerprint(config.ledger)
    human = None
    for row in rows(config.ledger):
        uid = row.get("user_id")
        if not isinstance(uid, (str, int)) or isinstance(uid, bool):
            raise Unsafe("invalid_ledger_user_id")
        if str(uid) in config.human_user_ids:
            # Any row from an allowlisted human counts, whatever its kind (text, caption, voice...).
            activity = timestamp(row.get("ts", row.get("timestamp")), now)
            human = activity if human is None else max(human, activity)
    assistant = None
    assistant_index = -1
    boundary_index = -1
    boundary_time = None
    meaningful = None
    regrowth_baseline = False
    for path in files:
        if path != latest:
            # Older transcripts only matter for recent terminal-typed human text. Skip files
            # untouched for a day; on any unreadable row, count the file's mtime as human
            # activity (conservative: it can only delay a compaction, never cause one).
            if now - stamps[path][3] / 1e9 > 86400:
                continue
            try:
                for row in rows(path):
                    check_meeting_record(row, now)
                    if row.get("type") == "user" and human_text(row, config.machine_wake_patterns):
                        activity = timestamp(row.get("timestamp"), now)
                        human = activity if human is None else max(human, activity)
            except Unsafe as exc:
                # A meeting declaration is a gate, not a malformed older row.
                # It must propagate even when the file mtime is older than 55m.
                if str(exc) == "meeting_poll_declared":
                    raise
                activity = stamps[path][3] / 1e9
                human = activity if human is None else max(human, activity)
            continue
        for index, row in enumerate(rows(path)):
            kind = row.get("type")
            if not isinstance(kind, str):
                raise Unsafe("invalid_transcript_type")
            check_meeting_record(row, now)
            if kind == "user" and human_text(row, config.machine_wake_patterns):
                activity = timestamp(row.get("timestamp"), now)
                human = activity if human is None else max(human, activity)
            boundary = (row.get("isCompactSummary") is True or
                        (kind == "system" and row.get("subtype") == "compact_boundary"))
            if boundary:
                boundary_index = index
                boundary_time = timestamp(row.get("timestamp"), now)
                if previous_send is not None and boundary_time >= previous_send:
                    regrowth_baseline = True
            if kind == "assistant":
                assistant = row
                assistant_index = index
                if previous_send is not None and timestamp(row.get("timestamp"), now) > previous_send:
                    prior_message = row.get("message")
                    usage = prior_message.get("usage", {}) if isinstance(prior_message, dict) else {}
                    counts = [usage.get(key) for key in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")] if isinstance(usage, dict) else []
                    if len(counts) == 3 and all(type(value) is int and value >= 0 for value in counts):
                        if sum(counts) < config.min_context:
                            regrowth_baseline = True
            if (kind not in METADATA and not
                    (kind == "system" and row.get("subtype") == "turn_duration")):
                meaningful = row
    if human is None:
        raise Unsafe("no_known_human_activity")
    if assistant is None:
        raise Unsafe("no_assistant_usage")
    assistant_time = timestamp(assistant.get("timestamp"), now)
    message = assistant.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        raise Unsafe("invalid_assistant_usage")
    tokens = []
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = message["usage"].get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise Unsafe("invalid_token_count")
        tokens.append(value)
    context = sum(tokens)
    if boundary_index > assistant_index or (boundary_time is not None and boundary_time > assistant_time):
        context = 0
    snapshot = (stamps, ledger_stamp)
    if not unchanged(config, snapshot):
        raise Unsafe("inputs_changed_during_read")
    regrown = regrowth_baseline and previous_send is not None and assistant_time > previous_send
    return human, context, latest_mtime / 1e9, meaningful, snapshot, regrown


def unchanged(config, snapshot):
    stamps, ledger_stamp = snapshot
    return (set(config.transcript_dir.glob("*.jsonl")) == set(stamps) and
            all(fingerprint(path) == stamp for path, stamp in stamps.items()) and
            fingerprint(config.ledger) == ledger_stamp)


SGR = re.compile(r"\x1b\[[0-9;]*m")
# Claude Code shows a prompt *suggestion* in the empty input box, drawn dim (SGR 2),
# e.g. "\x1b[39m❯\xa0\x1b[2mfix it and switch it live\x1b[0m" (Rick, 2026-10-03).
# Dim text on the input line is a placeholder; typed text is drawn at normal intensity.
SGR_TOKEN = re.compile(r"\x1b\[([0-9;]*)m")
PLACEHOLDERS = ("[Pasted text", "[Image #", "[Pasted image")


def visible_input(raw):
    """Characters of the input line drawn while dim is OFF (SGR state tracked per code)."""
    out, dim, pos = [], False, 0
    for match in SGR_TOKEN.finditer(raw):
        if not dim:
            out.append(raw[pos:match.start()])
        codes = (match.group(1) or "0").split(";")
        i = 0
        while i < len(codes):
            code = codes[i] or "0"
            if code in ("38", "48", "58"):  # extended colour: skip its arguments
                kind = codes[i + 1] if i + 1 < len(codes) else ""
                i += 3 if kind == "5" else 5 if kind == "2" else 2
                continue
            if code.lstrip("0") == "":      # 0 / 00 / empty = full reset
                dim = False
            elif code == "2":
                dim = True
            elif code == "22":              # normal intensity
                dim = False
            i += 1
        pos = match.end()
    if not dim:
        out.append(raw[pos:])
    return "".join(out)


def plain_pane(pane):
    """Strip SGR styling; on the input line drop dim (placeholder) text; refuse other escapes."""
    lines = []
    for raw in pane.splitlines():
        line = SGR.sub("", raw)
        if line.lstrip().startswith("❯"):
            # Paste/image placeholders may be drawn dim: never treat them as an empty prompt.
            if any(marker in line for marker in PLACEHOLDERS):
                raise Unsafe("draft_present")
            # Only the input line loses dim text; elsewhere dim text can be a busy hint
            # (e.g. a dim "esc to interrupt") that must stay visible to the busy checks.
            line = visible_input(raw)
        if "\x1b" in line:
            raise Unsafe("unreadable_pane")
        lines.append(line)
    return "\n".join(lines)


def check_pane(pane):
    if not isinstance(pane, str):
        raise Unsafe("unreadable_pane")
    pane = plain_pane(pane)
    if "esc to interrupt" in pane.lower():
        raise Unsafe("pane_busy")
    lines = pane.splitlines()
    # The supplied busy sample has a spinner even without the interrupt hint.
    if any(re.match(r"^\s*[✶✻✽✢✳✷✸✹✺✼✾✿]\s*\S.*(?:…|\.\.\.)", line) for line in lines):
        raise Unsafe("pane_working_indicator")
    prompts = [i for i, line in enumerate(lines) if line.lstrip().startswith("❯")]
    if not prompts:
        raise Unsafe("no_prompt_box")
    current = prompts[-1]
    separators = [i for i, line in enumerate(lines)
                  if re.fullmatch(r"\s*─{3,}\s*", line)]
    above = [i for i in separators if i < current]
    below = [i for i in separators if i > current]
    if not above or not below:
        raise Unsafe("unrecognized_prompt_box")
    top, bottom = above[-1], below[0]
    if any(i > bottom for i in separators):
        raise Unsafe("ambiguous_prompt_box")
    if any(i != current and lines[i].strip() for i in range(top + 1, bottom)):
        raise Unsafe("draft_or_ambiguous_prompt")
    if lines[current].lstrip()[1:].strip():
        raise Unsafe("draft_present")
    # An exited Claude can leave its empty prompt in scrollback above a shell.
    # Require Rick's sampled live status/footer, with no unknown trailing rows.
    footer = [line.strip() for line in lines[bottom + 1:] if line.strip()]
    # Board #1707: a narrow pane truncates the status line with "…" (e.g.
    # "... | ctx 57% left | 5h 6% …"), so the usage segments after the ctx
    # anchor may be cut anywhere. The "| ctx N% left" anchor is what proves a
    # live Claude footer; if the cut falls before it, the line still skips.
    status_pattern = (r".+\|\s*(?:Opus|Sonnet|Haiku)\b[^|]+\|\s*ctx\s+\d+(?:\.\d+)?%\s+left"
                      r"(?:\s*\|\s*(?:5h|7d)\s+\d+(?:\.\d+)?%)*"
                      r"(?:\s*(?:\|\s*(?:5h|7d)\b[^|…]*)?…)?")
    # Mode lines vary; require the model/context status line and allow only known
    # additional footer lines, so a shell prompt under the box still skips.
    mode = r"⏵⏵ .*permissions.*"
    if not footer:
        raise Unsafe("unrecognized_pane_footer")
    for line in footer:
        if not (re.fullmatch(mode, line) or re.fullmatch(status_pattern, line)):
            raise Unsafe("unrecognized_pane_footer")
    if not any(re.fullmatch(status_pattern, line) for line in footer):
        raise Unsafe("unrecognized_pane_footer")


def run_tmux(args):
    # No shell and a bounded wait; never include subprocess output in the log.
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=5)
    return result.stdout


def read_state(path, now):
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        try:
            state = json.load(handle)
        except ValueError:
            raise Unsafe("invalid_state") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise Unsafe("invalid_state")
    compact = timestamp(state.get("last_compact_at"), now)
    human = timestamp(state.get("human_activity_at"), now)
    if human > compact or state.get("status") not in ("pending", "sent"):
        raise Unsafe("invalid_state")
    if state["status"] == "pending":
        raise Unsafe("previous_send_uncertain_check_state")
    return compact, human


def write_state(path, state):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check(config, runner=run_tmux, clock=time.time):
    """One invocation, one content-free log line; runner is injectable for tests."""
    now = clock()
    idle = None
    context = None
    decision, reason = "SKIP", "unknown"
    try:
        # Prove the log writable before any tmux action.
        config.log.parent.mkdir(parents=True, exist_ok=True)
        with config.log.open("a", encoding="utf-8") as log:
            try:
                config.state.parent.mkdir(parents=True, exist_ok=True)
                with config.state.with_name(config.state.name + ".lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise Unsafe("another_check_running") from None
                    state = read_state(config.state, now)
                    check_declarations(config, now)
                    pane = target_pane(config, runner)
                    human, context, mtime, last, snapshot, regrown = inspect_inputs(
                        config, now, state[0] if state else None)
                    idle = (now - human) / 60
                    if idle < config.idle_min:
                        raise Unsafe("human_recent")
                    if context < config.min_context:
                        raise Unsafe("context_small")
                    if now - mtime < config.quiet_sec:
                        raise Unsafe("transcript_not_quiet")
                    last_message = last.get("message") if isinstance(last, dict) else None
                    if (last is None or last.get("type") != "assistant" or
                            not isinstance(last_message, dict) or
                            last_message.get("stop_reason") != "end_turn"):
                        raise Unsafe("turn_not_finished")
                    if state:
                        if now - state[0] < config.idle_min * 60:
                            raise Unsafe("compact_cooldown")
                        if not regrown:
                            raise Unsafe("context_not_regrown_since_send")
                    check_clients(config, runner, now)
                    capture_args = [config.tmux_bin, "capture-pane", "-p", "-e", "-t", pane]
                    check_pane(runner(capture_args))
                    # Narrow the unavoidable capture/send race with a second capture a moment later.
                    time.sleep(config.recheck_delay)
                    check_pane(runner(capture_args))
                    check_declarations(config, clock())
                    check_clients(config, runner, clock())
                    if target_pane(config, runner) != pane:
                        raise Unsafe("pane_changed_before_send")
                    if not unchanged(config, snapshot):
                        raise Unsafe("inputs_changed_before_send")
                    if config.dry_run:
                        decision, reason = "WOULD COMPACT", "all_conditions_met"
                    else:
                        # Reserve before sending: a partial/uncertain send must not retry.
                        saved = {"version": 1, "last_compact_at": iso(now),
                                 "human_activity_at": iso(human), "status": "pending"}
                        write_state(config.state, saved)
                        runner([config.tmux_bin, "send-keys", "-t", pane, "-l", "/compact"])
                        runner([config.tmux_bin, "send-keys", "-t", pane, "Enter"])
                        saved["status"] = "sent"
                        write_state(config.state, saved)
                        decision, reason = "COMPACT_SENT", "all_conditions_met"
            except Unsafe as exc:
                reason = str(exc)
            except (OSError, ValueError, TypeError, RecursionError, OverflowError,
                    UnicodeError, subprocess.SubprocessError):
                reason = "io_parse_or_tmux_error"
            log.write("{} decision={} reason={} idle_minutes={} context_tokens={}\n".format(
                iso(now), decision, reason, "unknown" if idle is None else "{:.2f}".format(idle),
                "unknown" if context is None else context))
    except OSError:
        # No action if logging cannot be opened. A fixed message contains no payload.
        print("SKIP reason=log_unavailable")
        return "SKIP", "log_unavailable"
    return decision, reason


def fleet_status(directory, runner=None, threshold=200000, known_slugs=()):
    """Read-only JSONL status, one record per installed configuration, including disabled jobs."""
    runner = runner or subprocess.run
    present = {path.stem for path in directory.glob("*.json")}
    for slug in sorted(set(known_slugs) - present):
        print(json.dumps(dict(slug=slug, config_installed=False, timer_installed=None,
                              timer_enabled=None, agent_installed=None, agent_active=None, last_decision=None,
                              last_compaction_time=None, reason="idle_compact_not_configured")))
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            runtime = data["runtime"]
            state_path, log_path = Path(runtime["state"]), Path(runtime["log"])
            last, high_since, context, last_sent = None, None, None, None
            if log_path.exists():
                with log_path.open() as handle:
                    for line in handle:
                        match = re.fullmatch(r"(\S+) decision=(.*?) reason=(\S+) idle_minutes=(\S+) context_tokens=(\S+)\n?", line)
                        if not match:
                            continue
                        last = {"time": match[1], "decision": match[2], "reason": match[3]}
                        if match[2] == "COMPACT_SENT":
                            last_sent = match[1]
                        context = int(match[5]) if match[5].isdigit() else None
                        if context is not None:
                            high_since = (high_since or match[1]) if context >= threshold else None
            saved = json.loads(state_path.read_text()) if state_path.exists() else {}
            if data["platform"] == "mac":
                probe = runner(["launchctl", "print", data["service"]], capture_output=True, timeout=5)
                installed = Path(data["unit_path"]).is_file()
                enabled = probe.returncode == 0
                agent = runner(["launchctl", "print", "gui/{}/com.bubble.ops-loop-{}".format(
                    os.getuid(), data["slug"])], capture_output=True, timeout=5).returncode == 0
                agent_installed = agent or (Path(data.get("home", str(Path.home()))) /
                    "Library/LaunchAgents" / ("com.bubble.ops-loop-" + data["slug"] + ".plist")).is_file()
            else:
                def probe(*args):
                    return runner(["systemctl", *args], capture_output=True, text=True, timeout=5)
                installed = probe("show", data["service"], "-p", "LoadState", "--value").stdout.strip() == "loaded"
                enabled = probe("is-enabled", data["service"]).returncode == 0
                agent_installed = probe("show", "bubble-agent@" + data["slug"] + ".service", "-p", "LoadState", "--value").stdout.strip() == "loaded"
                agent = probe("is-active", "bubble-agent@" + data["slug"] + ".service").returncode == 0
            print(json.dumps(dict(slug=data["slug"], config_installed=True, timer_installed=installed, timer_enabled=enabled,
                                  agent_installed=agent_installed, agent_active=agent, last_decision=last,
                                  last_compaction_time=saved.get("last_compact_at") if saved.get("status") == "sent" else last_sent,
                                  send_status=saved.get("status"), context_tokens=context,
                                  high_context_since=high_since)))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print(json.dumps(dict(slug=path.stem, status_error="unreadable_config_state_or_service")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--agents-root", type=Path, default=Path("/srv/agents"))
    parser.add_argument("--tmux-session", default=argparse.SUPPRESS)
    parser.add_argument("--tmux-bin", default=argparse.SUPPRESS)
    for name in ("transcript-dir", "ledger", "state", "log", "meeting-marker", "harness-selector"):
        parser.add_argument("--" + name, default=argparse.SUPPRESS)
    parser.add_argument("--idle-min", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--min-context", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--quiet-sec", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--human-user-ids", default=argparse.SUPPRESS)
    args = vars(parser.parse_args())
    status, directory, config_path = args.pop("status"), args.pop("config_dir"), args.pop("config")
    agents_root = args.pop("agents_root")
    if status:
        directory = directory or (Path.home() / ".local/state/idle-compact/configs" if os.uname().sysname == "Darwin"
                                  else Path("/etc/bubble-idle-compact"))
        if os.uname().sysname == "Darwin":
            known = [p.stem.removeprefix("com.bubble.ops-loop-") for p in
                     (Path.home() / "Library/LaunchAgents").glob("com.bubble.ops-loop-*.plist")]
        else:
            known = [p.name for p in agents_root.iterdir() if p.is_dir()] if agents_root.exists() else []
        fleet_status(directory.expanduser(), threshold=args.get("min_context", 200000), known_slugs=known)
        return
    values = {}
    if config_path:
        try:
            values = json.loads(config_path.expanduser().read_text())["runtime"]
        except (OSError, ValueError, KeyError):
            parser.error("unreadable config")
    values.update(args)
    for name in ("transcript_dir", "ledger", "state", "log", "meeting_marker", "harness_selector"):
        if values.get(name) is not None:
            values[name] = Path(values[name]).expanduser()
    if isinstance(values.get("human_user_ids"), str):
        values["human_user_ids"] = tuple(part.strip() for part in values["human_user_ids"].split(","))
    try:
        config = Config(**values)
    except TypeError:
        parser.error("supply --config or all of --tmux-session --transcript-dir --ledger --state --log")
    if (not math.isfinite(config.idle_min) or config.idle_min <= 0 or config.min_context <= 0 or
            not math.isfinite(config.quiet_sec) or config.quiet_sec <= 0 or
            not config.tmux_session.strip() or not all(str(uid).isdigit() for uid in config.human_user_ids)):
        parser.error("thresholds must be positive, session nonempty, and human IDs numeric")
    # Launchd PATH includes the three fleet locations; explicit --tmux-bin wins.
    config.tmux_bin = shutil.which(config.tmux_bin) or config.tmux_bin
    check(config)


if __name__ == "__main__":
    main()
