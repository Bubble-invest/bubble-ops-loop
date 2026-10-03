#!/usr/bin/env python3
"""Fail-closed fleet idle compaction (Python 3.9+, stdlib only)."""

import argparse
import fcntl
import json
import math
import os
import pwd
import re
import shutil
import stat
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
    r"^Resume\s+.+?\s+OODA\s+loop\b", r"^DUE_MISSIONS=",
    r"^MEETING POLL\b", r"^\[session-rotate,\s*automated maintenance\b",
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
    transport: str = "tmux"
    slug: str = ""
    dtach_socket: Path = None
    dtach_bin: str = "/usr/bin/dtach"
    confirm_min: float = 10

    def __post_init__(self):
        if self.transport == "dtach":
            if not self.slug:
                owner = pwd.getpwuid(os.getuid()).pw_name
                if owner.startswith("agent-"):
                    self.slug = owner[6:]
            if self.dtach_socket is None:
                self.dtach_socket = Path("/run/bubble-agent-" + self.slug + "/dtach.sock")


def own_processes(proc_root=Path("/proc")):
    """Read own-UID process identity, parent, liveness and systemd membership."""
    processes = {}
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            fields = dict(line.split(":", 1) for line in (path / "status").read_text().splitlines()
                          if ":" in line)
            if any(int(uid) != os.getuid() for uid in fields["Uid"].split()):
                continue
            if fields["State"].strip().split()[0] in ("Z", "X"):
                continue
            processes[int(path.name)] = dict(
                parent=int(fields["PPid"]), name=fields["Name"].strip(),
                argv=(path / "cmdline").read_bytes().decode().rstrip("\0").split("\0"),
                cgroup=(path / "cgroup").read_text())
        except (FileNotFoundError, ProcessLookupError):
            continue  # An exiting process cannot establish liveness.
        except (KeyError, IndexError, ValueError, UnicodeError):
            raise Unsafe("invalid_dtach_process_record") from None
    return processes


def target_dtach(config):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", config.slug):
        raise Unsafe("invalid_dtach_slug")
    try:
        info = config.dtach_socket.lstat()
    except FileNotFoundError:
        raise Unsafe("dtach_socket_missing") from None
    if not stat.S_ISSOCK(info.st_mode):
        raise Unsafe("dtach_not_socket")
    if info.st_uid != os.getuid():
        raise Unsafe("dtach_socket_foreign_owner")
    processes = own_processes()
    masters = []
    for pid, process in processes.items():
        argv = process["argv"]
        unit = "bubble-agent@" + config.slug + ".service"
        in_unit = any(unit in line.split(":", 2)[-1].split("/")
                      for line in process["cgroup"].splitlines())
        if (len(argv) >= 3 and Path(argv[0]).name == "dtach" and
                argv[1:3] == ["-N", str(config.dtach_socket)] and in_unit):
            masters.append(pid)
    if len(masters) != 1:
        raise Unsafe("dtach_master_not_alive")
    master = masters[0]
    if not any(process["parent"] == master and process["argv"] and
               (process["name"] == "claude" or Path(process["argv"][0]).name == "claude")
               for process in processes.values()):
        raise Unsafe("dtach_claude_child_not_alive")
    return info.st_dev, info.st_ino, master


def subagent_stamps(config, latest):
    if config.transport != "dtach":
        return {}
    # Claude Code uses <session-id>/subagents/; older layouts use agent-*.jsonl.
    files = set(config.transcript_dir.glob("agent-*.jsonl"))
    for directory in (latest.with_suffix("") / "subagents", config.transcript_dir / "subagents"):
        files.update(directory.rglob("*.jsonl"))
    return {path: fingerprint(path) for path in files}


def check_subagents(stamps, now, quiet_sec):
    if any(now - stamp[3] / 1e9 < quiet_sec for stamp in stamps.values()):
        raise Unsafe("subagent_not_quiet")


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


def check_meeting(config, declared=False):
    if declared or (config.meeting_marker and os.path.lexists(config.meeting_marker)):
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


def rows(path, complete=False):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if complete and not line.endswith("\n"):
                raise Unsafe("partial_transcript_write")
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
        if source.lower() in ("bubble-inject", "ops-loop-boot-rearm"):
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
            re.match(pattern, text.lstrip(), re.IGNORECASE)
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


def cron_job_id(value):
    """Read an explicit job-id field, never search arbitrary result text for ids."""
    if isinstance(value, dict):
        for key in ("id", "job_id", "jobId"):
            job_id = value.get(key)
            if isinstance(job_id, str) and job_id.strip():
                return job_id
    return None


def cron_result_id(content):
    if isinstance(content, list):
        # Tool results can wrap their payload in text blocks.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                job_id = cron_result_id(block.get("text"))
                if job_id:
                    return job_id
    if isinstance(content, str):
        try:
            job_id = cron_job_id(json.loads(content))
        except ValueError:
            job_id = None
        if job_id:
            return job_id
        match = re.match(r"^\s*(?:Successfully )?Created (?:cron )?job "
                         r"(?:with ID:\s*)?([\w-]+)(?=\s|[.(]|$)", content)
        return match[1] if match else None
    return cron_job_id(content)


class MeetingPolls:
    """Replay only structured cron events in the selected transcript/session."""

    def __init__(self, now):
        self.now = now
        self.session = None
        self.active = {}  # tool_use id -> (declaration timestamp, returned job id)

    def observe(self, row):
        session = row.get("sessionId")
        if isinstance(session, str):
            if self.session is not None and session != self.session:
                self.active.clear()
            self.session = session
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return
        role = message.get("role")
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if row.get("type") == "assistant" and role == "assistant" and block.get("type") == "tool_use":
                inputs = block.get("input")
                if not isinstance(inputs, dict):
                    continue
                prompt = inputs.get("prompt")
                if (block.get("name") == "CronCreate" and isinstance(prompt, str) and
                        prompt.lstrip().startswith("MEETING POLL")):
                    declared = timestamp(row.get("timestamp"), self.now)
                    # Missing call ids still block until expiry, but cannot correlate a result.
                    call_id = block.get("id")
                    key = call_id if isinstance(call_id, str) else object()
                    self.active[key] = (declared, None)
                elif block.get("name") == "CronDelete":
                    job_id = cron_job_id(inputs)
                    if job_id:
                        self.active = {key: value for key, value in self.active.items()
                                       if value[1] != job_id}
            elif (row.get("type") == "user" and role == "user" and
                  block.get("type") == "tool_result" and not block.get("is_error")):
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str) and call_id in self.active:
                    declared, _ = self.active[call_id]
                    self.active[call_id] = (declared, cron_result_id(block.get("content")))

    def declared(self):
        return any(self.now - declared < 3 * 3600 for declared, _ in self.active.values())


def inspect_inputs(config, now, previous_send=None):
    files = list(config.transcript_dir.glob("*.jsonl"))
    if config.transport == "dtach":
        files = [path for path in files if not path.name.startswith("agent-")]
    if not files:
        raise Unsafe("no_transcript")
    stamps = {path: fingerprint(path) for path in files}
    latest_mtime = max(stamp[3] for stamp in stamps.values())
    newest = [path for path in files if stamps[path][3] == latest_mtime]
    if len(newest) != 1:
        raise Unsafe("ambiguous_newest_transcript")
    latest = newest[0]
    children = subagent_stamps(config, latest)
    ledger_stamp = fingerprint(config.ledger)
    human = None
    for row in rows(config.ledger, complete=config.transport == "dtach"):
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
    meetings = MeetingPolls(now)
    for path in files:
        if path != latest:
            # Older transcripts only matter for recent terminal-typed human text. Skip files
            # untouched for a day; on any unreadable row, count the file's mtime as human
            # activity (conservative: it can only delay a compaction, never cause one).
            if now - stamps[path][3] / 1e9 > 86400:
                continue
            try:
                for row in rows(path):
                    if row.get("type") == "user" and human_text(row, config.machine_wake_patterns):
                        activity = timestamp(row.get("timestamp"), now)
                        human = activity if human is None else max(human, activity)
            except Unsafe:
                activity = stamps[path][3] / 1e9
                human = activity if human is None else max(human, activity)
            continue
        for index, row in enumerate(rows(path, complete=config.transport == "dtach")):
            kind = row.get("type")
            if not isinstance(kind, str):
                raise Unsafe("invalid_transcript_type")
            meetings.observe(row)
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
    snapshot = (stamps, ledger_stamp, latest, children)
    if not unchanged(config, snapshot):
        raise Unsafe("inputs_changed_during_read")
    regrown = regrowth_baseline and previous_send is not None and assistant_time > previous_send
    return human, context, latest_mtime / 1e9, meaningful, snapshot, regrown, meetings.declared()


def unchanged(config, snapshot):
    stamps, ledger_stamp, latest, children = snapshot
    files = set(config.transcript_dir.glob("*.jsonl"))
    if config.transport == "dtach":
        files = {path for path in files if not path.name.startswith("agent-")}
    return (files == set(stamps) and
            all(fingerprint(path) == stamp for path, stamp in stamps.items()) and
            fingerprint(config.ledger) == ledger_stamp and
            subagent_stamps(config, latest) == children)


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
    # Other sampled Mac status lines put the model first, or bracket it after
    # the repository/branch. Keep each grammar anchored to both model and ctx.
    model = r"(?:Opus|Sonnet|Haiku)\s+\d+(?:\.\d+)+(?:\s+\(\d+(?:\.\d+)?[MK] context\))?"
    percent = r"\d+(?:\.\d+)?%"
    status_patterns = (
        status_pattern,
        model + r"\s+\S+\s+\([^()\n]+\)\s+ctx:\s*" + percent
        + r"(?:\s+(?:5h|7d):\s*" + percent + r")*",
        r"\S+\s+\([^()\n]+\)\s+\[" + model + r"\]\s*\|\s*ctx:\s*" + percent
        + r"\s*\|\s*session:\s*\d+(?:…)?",
    )
    # Mode lines vary; require the model/context status line and allow only known
    # additional footer lines, so a shell prompt under the box still skips.
    mode = r"⏵⏵ .*permissions.*"
    if not footer:
        raise Unsafe("unrecognized_pane_footer")
    for line in footer:
        if not (re.fullmatch(mode, line) or any(re.fullmatch(pattern, line) for pattern in status_patterns)):
            raise Unsafe("unrecognized_pane_footer")
    if not any(re.fullmatch(pattern, line) for line in footer for pattern in status_patterns):
        raise Unsafe("unrecognized_pane_footer")


def run_tmux(args):
    # No shell and a bounded wait; never include subprocess output in the log.
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=5)
    return result.stdout


def write_dtach(config, payload):
    # Only the two fixed command fragments may reach stdin. Never invoke a shell.
    if payload not in (b"/compact", b"\r"):
        raise Unsafe("invalid_dtach_payload")
    subprocess.run([config.dtach_bin, "-p", str(config.dtach_socket)], input=payload,
                   capture_output=True, check=True, timeout=5)


def verify_dtach(config, now):
    """Confirm the reserved session on later polls, even if a different session is newest.

    Return True only once when the confirmation deadline expires. Repeat eligibility
    still comes exclusively from inspect_inputs and the common cooldown/regrowth gates.
    """
    if config.dry_run or not config.state.exists():
        return False
    saved = json.loads(config.state.read_text())
    if saved.get("transport") != "dtach":
        return False
    identity, size, transcript = (saved.get("transcript_identity"), saved.get("transcript_size"),
                                  saved.get("transcript_path"))
    if (not isinstance(transcript, str) or not Path(transcript).is_absolute() or
            type(size) is not int or size < 0 or not isinstance(identity, list) or
            len(identity) != 2 or any(type(value) is not int or value < 0 for value in identity) or
            type(saved.get("unconfirmed_logged", False)) is not bool):
        raise Unsafe("invalid_state")
    if saved.get("compact_confirmed_at"):
        timestamp(saved["compact_confirmed_at"], now)
        return False
    sent = timestamp(saved["last_compact_at"], now)
    path = Path(transcript)
    confirmed = None
    try:
        stamp = fingerprint(path)
        if (list(stamp[:2]) != saved["transcript_identity"] or stamp[2] < saved["transcript_size"]):
            raise Unsafe("confirmation_transcript_replaced")
        # Check appended rows only; an old boundary with an equal timestamp is not proof.
        with path.open("rb") as handle:
            handle.seek(saved["transcript_size"])
            for line in handle:
                if not line.endswith(b"\n"):
                    break  # In-progress writes are retried on the next timer poll.
                row = json.loads(line)
                if (isinstance(row, dict) and row.get("type") == "system" and
                        row.get("subtype") == "compact_boundary"):
                    boundary = timestamp(row.get("timestamp"), now)
                    if sent <= boundary <= sent + config.confirm_min * 60:
                        confirmed = boundary
                        break
        if fingerprint(path) != stamp:
            confirmed = None
    except (OSError, Unsafe, ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        confirmed = None  # Missing, replaced, partial or malformed data is not proof.
    if confirmed is not None:
        saved["compact_confirmed_at"] = iso(confirmed)
        write_state(config.state, saved)
    elif now - sent >= config.confirm_min * 60 and not saved.get("unconfirmed_logged"):
        saved["unconfirmed_logged"] = True
        write_state(config.state, saved)
        return True
    return False


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
        # Prove the log writable before any terminal action.
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
                    if config.transport not in ("tmux", "dtach"):
                        raise Unsafe("invalid_transport")
                    if verify_dtach(config, now):
                        decision = "COMPACT_UNCONFIRMED"
                        raise Unsafe("compact_boundary_timeout")
                    target = target_dtach(config) if config.transport == "dtach" else target_pane(config, runner)
                    human, context, mtime, last, snapshot, regrown, meeting = inspect_inputs(
                        config, now, state[0] if state else None)
                    idle = (now - human) / 60
                    check_meeting(config, meeting)
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
                    if config.transport == "dtach":
                        content = last_message.get("content")
                        if (last_message.get("role") != "assistant" or not isinstance(content, list) or
                                any(not isinstance(block, dict) or block.get("type") == "tool_use"
                                    for block in content)):
                            raise Unsafe("turn_not_finished")
                        if now - timestamp(last.get("timestamp"), now) < config.quiet_sec:
                            raise Unsafe("transcript_row_not_quiet")
                        check_subagents(snapshot[3], now, config.quiet_sec)
                    if state:
                        if now - state[0] < config.idle_min * 60:
                            raise Unsafe("compact_cooldown")
                        if not regrown:
                            raise Unsafe("context_not_regrown_since_send")
                    if config.transport == "tmux":
                        check_clients(config, runner, now)
                        capture_args = [config.tmux_bin, "capture-pane", "-p", "-e", "-t", target]
                        check_pane(runner(capture_args))
                    # Recheck after a pause: a second capture on tmux, fingerprints on dtach.
                    time.sleep(config.recheck_delay)
                    if config.transport == "tmux":
                        check_pane(runner(capture_args))
                    check_declarations(config, clock())
                    check_meeting(config)
                    if config.transport == "tmux":
                        check_clients(config, runner, clock())
                        if target_pane(config, runner) != target:
                            raise Unsafe("pane_changed_before_send")
                    elif target_dtach(config) != target:
                        raise Unsafe("dtach_changed_before_send")
                    if not unchanged(config, snapshot):
                        raise Unsafe("inputs_changed_before_send")
                    if config.dry_run:
                        decision, reason = "WOULD COMPACT", "all_conditions_met"
                    else:
                        # Reserve before sending: a partial/uncertain send must not retry.
                        saved = {"version": 1, "last_compact_at": iso(clock()),
                                 "human_activity_at": iso(human), "status": "pending"}
                        if config.transport == "dtach":
                            saved.update(transport="dtach", transcript_path=str(snapshot[2]),
                                         transcript_size=snapshot[0][snapshot[2]][2],
                                         transcript_identity=list(snapshot[0][snapshot[2]][:2]))
                        write_state(config.state, saved)
                        # Final fingerprint check after durable reservation, just before injection.
                        if not unchanged(config, snapshot):
                            raise Unsafe("inputs_changed_before_send")
                        if config.transport == "dtach":
                            write_dtach(config, b"/compact")
                            time.sleep(1.5)
                            write_dtach(config, b"\r")
                        else:
                            runner([config.tmux_bin, "send-keys", "-t", target, "-l", "/compact"])
                            runner([config.tmux_bin, "send-keys", "-t", target, "Enter"])
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
    """Read-only JSONL per installed config. Legacy known_slugs is intentionally ignored."""
    runner = runner or subprocess.run
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
                                  transport=runtime.get("transport", "tmux"),
                                  agent_installed=agent_installed, agent_active=agent, last_decision=last,
                                  last_compaction_time=saved.get("last_compact_at") if saved.get("status") == "sent" else last_sent,
                                  send_status=saved.get("status"), context_tokens=context,
                                  compact_confirmed_at=saved.get("compact_confirmed_at"),
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
    parser.add_argument("--transport", choices=("tmux", "dtach"), default=argparse.SUPPRESS)
    parser.add_argument("--slug", default=argparse.SUPPRESS)
    parser.add_argument("--dtach-bin", default=argparse.SUPPRESS)
    parser.add_argument("--confirm-min", type=float, default=argparse.SUPPRESS)
    for name in ("transcript-dir", "ledger", "state", "log", "meeting-marker", "harness-selector", "dtach-socket"):
        parser.add_argument("--" + name, default=argparse.SUPPRESS)
    parser.add_argument("--idle-min", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--min-context", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--quiet-sec", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--human-user-ids", default=argparse.SUPPRESS)
    args = vars(parser.parse_args())
    status, directory, config_path = args.pop("status"), args.pop("config_dir"), args.pop("config")
    args.pop("agents_root")
    if status:
        directory = directory or (Path.home() / ".local/state/idle-compact/configs" if os.uname().sysname == "Darwin"
                                  else Path("/etc/bubble-idle-compact"))
        fleet_status(directory.expanduser(), threshold=args.get("min_context", 200000))
        return
    values = {}
    if config_path:
        try:
            values = json.loads(config_path.expanduser().read_text())["runtime"]
        except (OSError, ValueError, KeyError):
            parser.error("unreadable config")
    values.update(args)
    values.setdefault("tmux_session", "")
    for name in ("transcript_dir", "ledger", "state", "log", "meeting_marker", "harness_selector", "dtach_socket"):
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
            not math.isfinite(config.confirm_min) or config.confirm_min <= 0 or
            config.transport not in ("tmux", "dtach") or
            (config.transport == "tmux" and not config.tmux_session.strip()) or
            (config.transport == "dtach" and (not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", config.slug) or
                                               not config.dtach_socket.is_absolute())) or
            not all(str(uid).isdigit() for uid in config.human_user_ids)):
        parser.error("invalid thresholds, transport target, or human IDs")
    # Launchd PATH includes the three fleet locations; explicit --tmux-bin wins.
    config.tmux_bin = shutil.which(config.tmux_bin) or config.tmux_bin
    config.dtach_bin = shutil.which(config.dtach_bin) or config.dtach_bin
    check(config)


if __name__ == "__main__":
    main()
