# Fleet idle compaction

The standard-library Python 3.9+ tool at `scripts/idle-compact/idle_compact.py`
checks a dedicated Claude Code tmux session every five minutes. Its default
human-idle threshold is **55 minutes**; the default minimum context is
**200,000 input tokens** and transcript quiet time is **120 seconds**.
Installers enable checks immediately, or install observation-only checks with
`--dry-run`. No installer restarts the department agent. The pilot directory is
reference material; deployed jobs use only the framework script.
**VPS agents running under dtach are not covered yet.** This release requires
a dedicated Claude Code tmux pane; dtach hosts log `SKIP reason=no_tmux_session`.

## Safety and context

The pilot's ledger/transcript rules remain: take the latest allowlisted human
activity across the Telegram delivery ledger (all message kinds) and terminal
text in direct project JSONL transcripts. IDs default to
`6532205130,7470271615`; override the tool's `--human-user-ids` or the installed
config's `runtime.human_user_ids` for another operator. Channel wrappers, tool
results, compaction summaries, inject/boot-rearm sources and OODA/DUE_MISSIONS
machine wakes do not count as humans. Built-in machine recognisers are:

- Channel envelopes (`<channel`, including `bubble-inject`), local-command and
  command-name/message/args wrappers, task notifications (`<task-notification`),
  system reminders, and `[SYSTEM NOTIFICATION` notices.
- `Resume <Name>'s OODA loop` (including multiword, hyphenated and Unicode names,
  and `Resume your OODA loop`), `DUE_MISSIONS=`, and `MEETING POLL`.
- `[session-rotate, automated maintenance` and transcript/message sources
  equal to `bubble-inject` or `ops-loop-boot-rearm`.

Wake regexes are case-insensitive and match only at the start of user text
(after whitespace); keywords quoted later in text do not suppress human activity.
Tool inputs/results and assistant text are never searched for wake keywords.
Add per-agent regular expressions in the
installed config's `runtime.machine_wake_patterns` array (built-ins remain
active), for example `["^FLEET HEARTBEAT\\b"]`. Mac reinstalls and VPS `--all` preserve these additions.
Invalid regexes, empty patterns and patterns matching empty text skip safely.
Recognised automation never refreshes human idle, even across days of wakes.
Direct human text (including separate text blocks beside task notices) and any
allowlisted human ledger entry reset the clock. Human ledger entries are
always authoritative, including messages quoting generated prompts. Recent
older transcripts also contribute human activity; malformed older files conservatively contribute their mtime.
The newest transcript is selected by mtime, never filename.

Context tokens come from the last assistant's input + cache-read + cache-create
usage; a later compaction boundary/summary resets this estimate. The live pane's
`| ctx N% left` gauge plus an Opus/Sonnet/Haiku model validates the status footer,
including truncated quota segments. A permissions-only footer fails closed;
percentages never replace transcript usage.

An assistant `end_turn`, quiet transcript, empty boxed `❯`, recognized footer,
and absence of spinners/interrupt hints are all required. Typed text, multiline
drafts and paste/image placeholders block sending. SGR dim suggestions are
ignored only on the input line; normal-intensity characters remain drafts.
Missing/corrupt input, future timestamps, unknown layouts or tmux errors skip.

Fleet additions require an exact session name with exactly one pane whose
current command is `claude` or a version string matching `^\d+\.\d+\.\d+$`
(observed on real Macs as `2.1.288` and `2.1.283`). `node` is rejected: the pilot
has no accepted node transport. Shells and all other commands fail closed.
The model plus context footer remains required for either accepted command shape. All tmux calls use the service's own Unix UID;
there is no root terminal injection. An attached client's `client_activity`
within 55 minutes blocks, even if its typed text was erased without submission.
Unknown client timestamps fail closed. The resolved pane ID, attached-client
activity, meeting/harness declarations, pane contents and transcript/ledger
fingerprints are rechecked before sending. A human/wake can still race the final
check: tmux has no atomic conditional send primitive.

Declare an ongoing meeting by creating `<dept>/state/meeting-poll.active`
**before** scheduling its poll and retain it until the poll job is deleted.
Use `--meeting-marker PATH` for another declaration path. Its mere existence,
including an unreadable/broken symlink, blocks. In the newest transcript only,
an assistant `CronCreate` tool-use declares a meeting when its `prompt` starts
with `MEETING POLL` after optional whitespace. Text in documents, shell commands,
tool results or user messages cannot declare a meeting. The declaration ends
on a later assistant `CronDelete` targeting the job id from the correlated
`CronCreate` result, at three hours (the meeting-room skill's bound), or on
session rotation, whichever comes first. Unknown result formats retain the
three-hour bound. Meeting skips include human idle and context token metrics.
The marker covers declarations outside scanned transcripts and
longer meetings or poll schedules with no recent transcript event. No pre-existing meeting marker
contract exists in this repository; meeting operators must adopt this one.

At most one send per quiet stretch. Another send needs at least `idle_min`
minutes since the last send, a new completed assistant turn after that send,
and context that has re-crossed `min_context`. A post-send compact boundary or
below-threshold assistant usage establishes the reduction; later assistant
usage must again reach the threshold. Every other gate still applies, including
human idle, transcript quiet, empty input, attached-client activity and meetings.
No new human message is required: autonomous agents can compact repeatedly
as their context regrows. A stale high-context snapshot or a new turn without
any evidence of reduction cannot trigger another send. Do not delete state on
ordinary restarts.
An atomic `pending` reservation is written before literal `/compact` and Enter,
then changed to `sent`. Pending state blocks all retries: inspect/clear partial
input manually and archive state only after resolving the uncertainty.
`COMPACT_SENT` confirms keystrokes, not completed compaction. Dry-run never
sends or writes compaction state; it may create lock/directories/logs.

## Macs

Run as the agent's login user from a stable framework checkout:

```sh
scripts/install-mac-idle-compact.sh --slug rnd --tmux-session ops-loop-rnd \
  --dept-dir "$HOME/claude-workspaces/Rick_RnD" --dry-run
# Observe WOULD COMPACT; rerun the same command without --dry-run to enable sends.
```

Optional `--transcript-dir DIR`, `--ledger PATH`, `--tmux-bin PATH`,
`--meeting-marker PATH`, `--selector-dir DIR` override local conventions.
Transcript directory defaults to `~/.claude/projects/` plus the absolute dept
path with non-alphanumeric characters replaced by `-`, matching the launcher.
Ledger defaults to `~/.claude/channels/telegram-<slug>/delivery-ledger.jsonl`.
The harness selector defaults to
`~/Library/Application Support/bubble-ops-loop/harness-<slug>`; supply the
existing launcher's custom selector directory when applicable.

The plist uses `/usr/bin/python3` (stdlib only), the framework's absolute script
path, and a JSON config at `~/.local/state/idle-compact/configs/<slug>.json`.
Label: `com.bubble.idle-compact-<slug>`. Interval: 300 seconds, no KeepAlive.
Decision/stdout/stderr logs live in `~/Library/Logs`. The installer validates
XML with plistlib and plutil before replacement, preserves a loaded definition
on failure, and leaves unchanged loaded jobs alone. Rollback attempts re-bootstrap
even if file restoration fails, and reports the original and each recovery error
by stage/type without subprocess output. State survives updates.
Hermes selectors and Morty are explicitly skipped.

Uninstall one agent (remove plist/config, retain state and logs):

```sh
scripts/install-mac-idle-compact.sh --slug rnd --uninstall
```

## VPS

Install the framework at `/opt/bubble-ops-loop`; run the installer as root:

```sh
scripts/install-idle-compact.sh --slug maya --tmux-session ops-loop-maya --dry-run
scripts/install-idle-compact.sh --all --dry-run
# Per-agent: rerun without --dry-run after observing the actual terminal.
```

The service itself uses `User=agent-%i`, `Group=agent-%i`,
`HOME=/home/agent-%i`, and `/srv/agents/%i`. It does not need rotate's root
privileges: rotate stops/restarts services and moves owned transcripts;
compaction only reads its own files and writes its own terminal/state.
Root-readable configs are at `/etc/bubble-idle-compact/<slug>.json` (0644,
no secrets). State and decision logs are under
`/home/agent-<slug>/.local/state/idle-compact`, owned by that agent.
Hardening includes NoNewPrivileges, ProtectSystem=strict, ProtectHome=read-only
and one writable state directory. PrivateTmp is deliberately false to expose
the agent's existing `/tmp/tmux-UID` socket.

`--all` enumerates `/srv/agents`, skips `/etc/bubble-harness/<slug>` selectors
set to Hermes and Morty, and preserves installed per-agent overrides.
`--all --dry-run` also forces existing configs into observation mode.
For new configs, `ops-loop-<slug>` is a **candidate session name**, matching the
Mac convention, not a claim about the current VPS. Use `--slug --tmux-session`
to supply a verified name. Transcript and ledger defaults are
`~/.claude/projects/-srv-agents-<slug>` and the own-home Telegram path above.
Other paths are per-agent installer overrides. Unit/config publication rolls
back on reload/enable failures and restores prior timer enabled/active states.

**VPS transport limitation:** real VPS agents use **dtach**, which this change
**does not cover yet**. Timers alone cannot compact those agents. Without a
supported own-user tmux session the tool logs
`decision=SKIP reason=no_tmux_session`. This release does not migrate the
runtime or send slash commands through `bubble-inject` channel messages.
Legacy shared `claude` UID/custom-home units also need explicit runtime review;
the installer refuses those layouts.

Uninstall one agent, or every installed/discovered VPS agent:

```sh
scripts/install-idle-compact.sh --slug maya --uninstall
scripts/install-idle-compact.sh --all --uninstall
```

Uninstall disables/stops the timer and any running idle-compaction service,
removes its config, and retains agent state and logs. Shared unit templates
remain while another config uses them; the last uninstall removes both templates
and reloads systemd. Mac uninstall boots out the job and removes its plist/config.
Neither uninstaller stops the department agent. Rollback on installation attempts
all recovery steps even if restore/reload fails and reports every failed stage.

## Logs and fleet status

One content-free decision line per invocation:

```text
2026-10-03T12:00:00Z decision=SKIP reason=human_recent idle_minutes=12.00 context_tokens=210000
```

Decisions: `SKIP`, `WOULD COMPACT`, `COMPACT_SENT`. Reasons are fixed strings;
unevaluated numbers are `unknown`. No messages or subprocess output are logged.
If the log cannot be opened, no action occurs and stdout reports
`SKIP reason=log_unavailable`. Arrange host log rotation; preserve current logs
long enough for the fleet alarm window.

```sh
scripts/install-mac-idle-compact.sh --status
scripts/install-idle-compact.sh --status
python3 scripts/idle-compact/idle_compact.py --status --min-context 300000
```

Status is read-only JSONL per configured agent: latest decision/time,
last confirmed send time (`last_compaction_time`), pending/sent state,
context tokens, `high_context_since`, timer installed/enabled and agent installed/active.
The Mac active flag indicates that its agent LaunchAgent is loaded. An observed
context at or above the selected threshold starts `high_context_since`; a
known smaller observation resets it, unknown observations preserve it.
Alarm if this age exceeds one day, or if decisions stop advancing. This is an
observation history, not a live context measurement or proof of completion.
Unconfigured department directories (VPS) or agent LaunchAgents (Mac) produce
explicit missing-config rows with unknown installation flags; Hermes entries
are expected rollout exclusions. Root is needed on VPS to read all private logs.
Corrupt config/state or unavailable service commands produce `status_error`.

## Offline verification and live acceptance

```sh
python3 -m unittest discover -s tests -p 'test_idle_compact*.py' -v
python3 -m pytest -q tests/test_idle_compact.py tests/test_idle_compact_fleet.py
```

Host acceptance must verify Python/tmux paths, actual UID/socket/pane command,
absolute transcript and ledger paths, custom harness selectors, context/footer
variants, attached typing and meeting inhibition, enabled timer cadence, and
one observed dry-run before a send. Then prove the compaction boundary/context
reduction in the actual transcript and that another poll skips. Check reboot
persistence, disable/reinstall, and config/state/log ownership. VPS acceptance
also requires resolving the documented transport mismatch; no host was
contacted or changed by this implementation.
