# RUNBOOK — #606 Mac maintenance-inject consumer recovery

This restores the standard `bubble-inject` consumer in a Mac Telegram plugin
cache without changing the independent `bubble-agent-message` peer transport.
The maintenance queue is local operator input. The peer spool keeps its own
fixed sender identities and lifecycle.

## Preconditions

- Use the reviewed, merged `bubble-ops-loop` checkout on the target Mac.
- Confirm the main agent session is on the Claude harness and that its prompt is
  idle. Save a private `0600` pane/session snapshot before reconnecting MCP.
- Record SHA-256 and marker counts for the live plugin `server.ts`. There must be
  either no peer watcher markers or exactly one valid begin/end pair.
- Treat every pre-existing maintenance-inject line as unknown. Do not print,
  classify by content, or replay it. Record only byte count, line count, SHA-256,
  UTF-8 validity, control-character count, owner, mode, and link count.
- If fixed-peer messaging is already provisioned, require its existing private
  sender config and canonical installed watcher package. The first reviewed
  self-heal records the route already embedded in the live watcher as private
  `watcher.json`; later cache refreshes reconstruct that exact route. Missing
  peer config skips peer installation. A config without its canonical package
  fails visibly and never invents identities or a channel id.

On Linux, systemd reaches the shared installer through the root-owned lifecycle
helper. The installer validates the explicitly rendered
`BUBBLE_AGENT_OS_USER` and `BUBBLE_AGENT_HOME` against passwd, then re-execs the
entire home/cache operation under that UID with a minimal environment. It uses
the protected root-owned watcher package under `/usr/local/lib`. Root never
imports or executes a tenant-owned watcher package or opens a tenant plugin.

## Review-stage proof (no runtime writes)

Run the installer in strict dry-run mode against the actual plugin cache:

```sh
CHANNEL_PATCHES_PLUGIN_GLOB="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/" \
  scripts/install-channel-patches.sh --dry-run --strict
```

Run the quarantine tool without `--apply`, pinned to the freshly measured
metadata. It exits nonzero if the queue changed since measurement:

```sh
scripts/quarantine-mac-inject-backlog.py \
  --state-dir "$HOME/.claude/channels/telegram-socials" \
  --slug content \
  --expected-sha256 REVIEWED_SHA256 \
  --expected-lines REVIEWED_LINE_COUNT
```

Both commands must leave the plugin and queue hashes unchanged.

## Reviewed deployment order

1. Recheck session idleness, harness selector, plugin hash/markers, and queue
   metadata. Stop if any reviewed expectation changed.
2. Run the pinned quarantine command with `--apply`. It atomically renames the
   exact old inode beneath `.held-maintenance-inject/` and creates a new empty
   mode-`0600` queue. The held directory is mode `0700`. Keep the held file for
   explicit human review; never feed it to either consumer.
3. Run `install-channel-patches.sh --strict`. It applies and Bun-validates the
   canonical maintenance consumer. It also hashes the complete peer-watcher
   block before and after; any mutation restores the pre-run `server.ts`.
4. Re-render the existing main wrapper with its already-reviewed arguments and
   the merged `--channel-patches-script` path, without `--activate`. Compare the
   candidate against the live wrapper and accept only the expected self-heal
   block. This makes future natural launches reapply the cache patch after a
   plugin update without restarting the current session.
5. Verify syntax/build, exact peer-watcher digest, one maintenance marker pair,
   and an empty maintenance queue. The peer spool files and watcher block must
   remain byte-identical.
6. From the saved, idle main session, request `/mcp reconnect`. Do not reload the
   LaunchAgent automatically if reconnect fails. Report the consumer as staged,
   leave the backup job unloaded, and schedule a reviewed idle lifecycle instead.
7. After reconnect succeeds, activate the separately reviewed normal backstop
   job. If its existing stale-session gate emits the approved fixed wake,
   verify that this new queue drains with `meta.source=bubble-inject`. Do not add
   a separate synthetic prompt to the live agent, send through the peer spool,
   or impersonate a department sender. Queueing is not execution: require the
   normal agent heartbeat to advance before calling the backstop healthy.

## Rollback

Keep the installer's adjacent `server.ts` backups and the private session
snapshot. A failed build restores the plugin automatically. For a later manual
rollback, restore the exact pre-deploy `server.ts`, Bun-build it, and reconnect
MCP during an idle session. Leave the held backlog untouched.
