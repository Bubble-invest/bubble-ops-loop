# Fleet idle compaction

The standard-library Python 3.9+ tool at `scripts/idle-compact/idle_compact.py`
checks Claude Code every five minutes through tmux (Mac) or dtach (VPS). Its default
human-idle threshold is **55 minutes**; the default minimum context is
**200,000 input tokens** and transcript quiet time is **120 seconds**.
Installers enable checks immediately, or install observation-only checks with
`--dry-run`. No installer restarts the department agent. The pilot directory is
reference material; deployed jobs use only the framework script.
Transport defaults to `tmux`; the VPS installer writes `runtime.transport=dtach`.
Hermes agents, including Morty, remain excluded.

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

For tmux, an assistant `end_turn`, quiet transcript, empty boxed `❯`, recognized footer,
and absence of spinners/interrupt hints are all required. Typed text, multiline
drafts and paste/image placeholders block sending. SGR dim suggestions are
ignored only on the input line; normal-intensity characters remain drafts.
Missing/corrupt input, future timestamps, unknown layouts or tmux errors skip.

The tmux transport requires an exact session name with exactly one pane whose
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
human idle, transcript quiet and meetings, plus empty input and attached-client
activity on tmux.
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
scripts/install-idle-compact.sh --slug maya --dry-run
scripts/install-idle-compact.sh --all --dry-run
# After observing WOULD COMPACT for this agent, enable sends:
scripts/install-idle-compact.sh --slug maya
```

VPS agents use `/usr/bin/dtach -N /run/bubble-agent-<slug>/dtach.sock` under
`bubble-agent@<slug>.service`. No screen is available. This transport is for
headless Claude Code agents with bypass permissions and humans delivering
through the Telegram ledger. Terminal drafts, menus and permission dialogs
cannot be inspected; verify these runtime assumptions before enabling sends.
The tool does not attach a screen or migrate the department runtime.

Without screen evidence, dtach requires all of the following:

- The newest main transcript (mtime, excluding top-level `agent-*.jsonl`) ends
  in a completed assistant `end_turn` with no tool-use blocks. User/tool-result,
  unknown meaningful rows and incomplete JSONL writes block. Known metadata
  and turn-duration rows are ignored as on tmux.
- Both its mtime and that assistant row's timestamp are at least 120 seconds
  old. Future or invalid row timestamps fail closed.
- No JSONL under `<session-id>/subagents/` (including nested files), the legacy
  project `subagents/` directory, or top-level `agent-*.jsonl` was modified
  within 120 seconds. Sub-agent files never supply main-session context.
- The socket exists, is an actual socket (symlinks refused), and belongs to the
  runtime UID. `/proc` must show exactly one live own-UID dtach master with
  `-N <socket>` in `bubble-agent@<slug>.service`, owning a live Claude child.
  Missing, foreign-owned, dead or ambiguous targets skip.
- All shared gates pass: ledger/transcript human idle, context usage,
  cooldown/reduction/regrowth, pending reservation, harness and meeting checks.
  After a short pause, socket/master identity and transcript/ledger/sub-agent
  fingerprints must still match. Fingerprints are checked again immediately
  before writing the command, after reserving pending state.

The send uses two bounded (five-second timeout) `dtach -p <socket>` subprocesses:
first the fixed bytes `/compact` without newline, then a 1.5-second pause, then
`\r`. There is no shell and no arbitrary payload. A failure leaves pending
state and blocks retries; inspect the terminal manually before clearing it.
A delivery or background write can still race the last check or the two writes:
dtach has no atomic conditional send primitive.

`COMPACT_SENT` records delivery of both writes. Later timer polls look for a
new `compact_boundary` in the original transcript inode, after the reserved
byte offset, timestamped within `--confirm-min` minutes (default 10) of sending.
They record `compact_confirmed_at` in state. If no valid boundary appears,
one poll logs `COMPACT_UNCONFIRMED reason=compact_boundary_timeout` and persists
that notification flag. A missing/replaced transcript or a summary alone is not
confirmation. Reporting happens on the first poll at/after the deadline;
no oneshot waits ten minutes. Unconfirmed sends retain the normal cooldown and
reduction/regrowth rule; timeout alone never authorizes a resend.

The service uses `User=agent-%i`, `Group=agent-%i`, `HOME=/home/agent-%i`, and
`/srv/agents/%i`. Configs are `/etc/bubble-idle-compact/<slug>.json` (0644,
no secrets). State/logs are `/home/agent-<slug>/.local/state/idle-compact`, owned
by that agent. Hardening includes NoNewPrivileges, ProtectSystem=strict,
ProtectHome=read-only, an explicit read-only `/run/bubble-agent-%i` mount and
one writable state directory. Connecting to an existing Unix socket does not
need a writable filesystem mount. `/proc` stays visible for own-UID liveness
checks; PrivateTmp remains false for compatibility with existing tmux configs.
The optional socket mount tolerates an absent runtime directory so the tool
can log a missing-socket skip.

`--all` enumerates `/srv/agents`, skips Hermes selectors and Morty, and preserves
installed per-agent overrides while upgrading old VPS configs to dtach.
`--all --dry-run` forces existing configs into observation mode. Enable sends
for an observed agent with `--slug` without `--dry-run`.
Transcript and ledger defaults are
`/home/agent-<slug>/.claude/projects/-srv-agents-<slug>` and
`/home/agent-<slug>/.claude/channels/telegram-<slug>/delivery-ledger.jsonl`.
Per-agent installer overrides include `--dtach-socket PATH`, `--dtach-bin PATH`,
`--confirm-min N`, transcript/ledger paths and meeting/harness selectors.
For direct tool invocation use `--transport dtach --slug <slug>` (slug otherwise
comes from the current `agent-<slug>` Unix username); no tmux session is required.
Unit/config publication rolls back on reload/enable failures and restores prior
timer enabled/active states. Legacy shared UID/custom-home layouts are refused.

Observe the installed transport, timer and content-free decisions:

```sh
scripts/install-idle-compact.sh --status
systemctl status idle-compact@maya.timer
sudo -u agent-maya tail -n 20 /home/agent-maya/.local/state/idle-compact/maya.log
# Trigger an observation immediately while its config is still dry_run=true:
systemctl start idle-compact@maya.service
```

Disable one agent's checks while retaining its config/state/logs:

```sh
systemctl disable --now idle-compact@maya.timer
systemctl stop idle-compact@maya.service
# Re-enable only when ready:
systemctl enable --now idle-compact@maya.timer
```

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

Decisions: `SKIP`, `WOULD COMPACT`, `COMPACT_SENT`, `COMPACT_UNCONFIRMED` (dtach). Reasons are fixed strings;
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
last send time (`last_compaction_time`), transport, pending/sent state,
dtach boundary confirmation time (`compact_confirmed_at`),
context tokens, `high_context_since`, timer installed/enabled and agent installed/active.
The Mac active flag indicates that its agent LaunchAgent is loaded. An observed
context at or above the selected threshold starts `high_context_since`; a
known smaller observation resets it, unknown observations preserve it.
Alarm if this age exceeds one day, or if decisions stop advancing. This is an
observation history, not a live context measurement or proof of completion.
Only idle-compact configs produce status rows. Unrelated backup/wake/main
LaunchAgents and unconfigured departments are omitted. Root is needed on VPS
to read all private logs.
Corrupt config/state or unavailable service commands produce `status_error`.

## Offline verification and live acceptance

```sh
python3 -m unittest discover -s tests -p 'test_idle_compact*.py' -v
python3 -m pytest -q tests/test_idle_compact.py tests/test_idle_compact_fleet.py
```

Host acceptance must verify Python/transport paths, actual UID/socket/process,
absolute transcript and ledger paths, custom harness selectors, context/footer
variants, attached typing and meeting inhibition, enabled timer cadence, and
one observed dry-run before a send. Then prove the compaction boundary/context
reduction in the actual transcript and that another poll skips. Check reboot
persistence, disable/reinstall, and config/state/log ownership. For VPS, verify
headless/bypass assumptions, `/proc` command/cgroup identity, socket connectivity
under the hardened service, sub-agent layout and quiet inhibition, the two-write
send, boundary confirmation within ten minutes and one unconfirmed-timeout log.
No host was contacted or changed by this implementation.
