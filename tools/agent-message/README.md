# Fixed-peer agent messaging

Additive Ben ↔ Miranda messages using one dedicated SSH key per direction, fixed forced-command receivers, and a dedicated Telegram MCP spool watcher. Python 3.9+ standard library; OpenSSH client/server. Does not use or drain the existing operator `inject` file.

## Operator setup

For each agent account:

1. Install `agent_message.py` as an executable `agent-message` on the agent PATH (or a wrapper invoking a fixed absolute Python interpreter and this file). Keep the implementation in a trusted deployment directory.
2. Create `~/.config/bubble-agent-message/inbox` owned by that account, mode 0700. Use a physical absolute path: receiver rejects symlinks in all inbox path components, including symlink aliases such as macOS `/var`.
3. Generate a dedicated SSH key for only this outbound pair route. Add its public key to the peer's authorized keys with `restrict,command="/absolute/python3 /absolute/agent_message.py receive --sender ben --recipient miranda --inbox-dir /absolute/miranda/.config/bubble-agent-message/inbox"`. Reverse the identities for Miranda's key on Ben. Do not reuse a general administration key. The sender intentionally supplies no remote command; any nonempty `SSH_ORIGINAL_COMMAND` is rejected.
4. Pin and independently verify the peer SSH host key in the dedicated known-hosts file. Do not accept a key merely because an untrusted network scan returned it.
5. Install this account's fixed route at `~/.config/bubble-agent-message/config.json` (mode 0600), for example:

```json
{
  "self": "ben",
  "peer": "miranda",
  "host": "peer.example",
  "user": "miranda",
  "identity_file": "/absolute/ben/.ssh/ben-to-miranda",
  "known_hosts_file": "/absolute/ben/.config/bubble-agent-message/known_hosts"
}
```

6. Install the recipient watcher against its actual running Telegram plugin cache file:

```sh
python3 install_inject_watcher.py --server /absolute/plugin/server.ts \
  --inbox /absolute/miranda/.config/bubble-agent-message/inbox \
  --sender ben --recipient miranda --chat-id EXISTING_CHANNEL_ID
```

The installer creates `server.ts.bak-agent-message`, rejects a differing existing patch, and is idempotent for identical routes. Validate the updated plugin with the deployed Bun runtime, then reconnect that agent's Telegram MCP to load it. Preserve any unsent session draft before using the session UI. Repeat with reverse identities on Ben. Plugin cache replacement requires reapplying the watcher; this package does not modify lifecycle installers automatically.

7. Install `SKILL.md` into each account's skill directory. Test a synthetic question and a substantive correlated response in the actual sessions before claiming bidirectional communication works.

## Deployment adaptation for service accounts

The operator's paired deployment uses source-address restrictions in addition to `restrict,command=`: Ben's outbound key can enter Miranda's account only from the expected VPS address; Miranda's return key can enter the VPS only from the expected M1 address. Provision the keys and private route configurations locally; never commit keys or live credentials.

Where Ben's service account has a `nologin` shell, preserve that shell. The return route can use a dedicated root authorized-key entry whose forced command immediately invokes `/usr/sbin/runuser -u agent-ben -- /absolute/python3 /absolute/agent_message.py receive --sender miranda --recipient ben --inbox-dir /absolute/ben/inbox`. This is a narrowly scoped inbound drop to Ben's spool, not a reusable root shell credential. Pin host keys through the existing authenticated administration SSH path, and verify the exact `runuser`/Python paths on the target. The per-user `user` route field must match the selected SSH login account (root for this return adaptation).

## Protocol and guarantees

Sender accepts only text and an optional reply ID; host/user/destination cannot be selected at the CLI. SSH disables user config, agent auth, agent forwarding and port forwarding, uses the dedicated identity and strict host-key checks, and requires batch authentication. Text travels only as JSON stdin, never in a remote shell command.

Receiver validates the key-bound fixed identities, UUID IDs and correlation, envelope size (8192 bytes), text size (4096 UTF-8 bytes), and control characters. Embedded newlines are JSON escaped. It serializes receiver writes, rejects writable-peer directories/files, symlinks, hardlinks and nonregular files, and atomically publishes an owned mode-0600 `<id>.json` in the inbox. Repeated IDs in queued or delivered files are rejected. The consumer validates the event again, emits `source=bubble-agent-message` and `user=<sender>`, and renames successful notifications to `<id>.delivered`.

`queued` means transport accepted the event, **not read, processed or answered**. A `.delivered` file means the MCP notification call succeeded, **not that the recipient semantically processed it**. Notification failure keeps the queued event for a later tick. The watcher polls every two seconds and handles up to 100 events per tick. A crash after notification but before rename may redeliver: recipients must deduplicate IDs before consequential actions. Do not automatically acknowledge acknowledgements. The existing channel chat ID is used for routing; the event's agent provenance is explicit and does not confer human authority.

Delivered files remain as bounded-per-message audit/replay records; there is no automatic expiry or total-spool quota. The grant is for trusted fixed peers, not an untrusted public inbox. Deliberate flooding can consume disk space. Invalid or unreadable events pause that drain and require operator inspection. An SSH timeout can be ambiguous; inspect the receipt/spool rather than blindly retrying. Delivery filenames sort by ID, so messages have no strict chronological ordering guarantee.

Rollback: remove the two dedicated authorized-key entries, restore each plugin's `server.ts.bak-agent-message` only after checking for concurrent cache edits, reconnect MCP, and remove the added route configs/skills/command. Leave inbox receipts for diagnosis; no existing inject queue needs changing.

## Tests

```sh
python3 -m unittest discover -s tools/agent-message -p 'test_*.py' -v
```

Tests cover framing and bidirectional correlation, malicious identities/IDs/controls, oversized inputs, SSH command overrides, symlinks/hardlinks/FIFOs, writable destinations, replay guards, fixed SSH arguments, real Node watcher emission, provenance metadata, failed notifications, untouched legacy queues and idempotent installation. Node is required for watcher runtime tests; Python-only transport tests remain available otherwise.
