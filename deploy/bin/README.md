# deploy/bin — on-box helper scripts

These scripts run on the VPS (the "box") at `/usr/local/bin/`. They are versioned
here so a fresh clone can reproduce the box-side of the control plane (closing the
VOIE3 reproducibility gap §1 / §5 PR-D — on-box-only scripts that the repo could
not previously reproduce).

**Install:** copy each into `/usr/local/bin/`, `chmod 0755` (the credential
helpers stay root-owned where noted), then wire the env / sudoers below. All
operator-specific values are read from environment variables — set them privately
at runtime (typically from the SOPS-decrypted env or the systemd unit), never in
these files.

## Common env vars

| var | meaning | default |
|-----|---------|---------|
| `BUBBLE_GH_APP_ID` | GitHub App ID of the board/ops bot | — (required) |
| `BUBBLE_GH_INSTALL_ID` | App installation ID (covers the org) | — (required) |
| `BUBBLE_GH_ORG` | org-account owner | `Bubble-invest` |
| `BUBBLE_GH_PERSONAL_OWNER` | personal-account owner whose install also covers the operator's own repos | empty (org-only) |
| `BUBBLE_BOARD_PEM_ENC` | path to the SOPS-encrypted App PEM | `/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem` |
| `BUBBLE_AGE_KEY_FILE` | age key file for SOPS decryption | `/etc/age/key.txt` |
| `BUBBLE_OPERATOR_CHAT_ID` | Telegram chat_id to notify | empty (TG skipped) |
| `BUBBLE_FIXTURE_AGENT` | agent dir under `/home/claude/agents` for the L4 canary | `fixture` |

---

## bubble-board-token.sh

Mints a SHORT-LIVED, `issues:write`+`metadata:read`-only GitHub App token for the
kanban control plane (`Bubble-invest/bubble-ops-board`) and prints it.

- **Why it matters:** the `claude` user runs the dept agents + the kanban emitter
  but must NOT hold the bot's App private key (PEM is root-only by design). This
  root-owned minter is exposed to `claude` via a tight sudoers NOPASSWD rule and
  mints the minimum scope needed — it can create board issues, nothing else.
- **Install:** `/usr/local/bin/bubble-board-token.sh`, root-owned `0755`.
- **Env:** `BUBBLE_GH_APP_ID`, `BUBBLE_GH_INSTALL_ID`, `BUBBLE_BOARD_PEM_ENC`,
  `BUBBLE_AGE_KEY_FILE`.
- **Sudoers:** a NOPASSWD rule letting `claude` run this script (and only this
  script). Requires `sops` at `/usr/local/bin/sops` and the age key readable by root.

## bubble-board-token-refresh.sh

Mints a fresh board token into `/run/bubble-board/token` (tmpfs, `0640`,
claude-readable) every ~45 min via a systemd timer.

- **Why it matters:** the cockpit runs `NoNewPrivileges=yes` so it cannot `sudo`
  at request time; it reads the pre-minted short-lived token from tmpfs instead.
- **Install:** `/usr/local/bin/bubble-board-token-refresh.sh`, root-owned `0755`,
  driven by a root systemd timer (~45 min). Calls `bubble-board-token.sh`.

## bubble-git

Pre-auth `git` wrapper: mints an App install token via the credential helper and
injects it into `http.extraheader` for the duration of the command.

- **Why it matters:** git's `credential.helper` protocol only kicks in on
  401-retry; private repos return "not found" on the first request before auth.
  `extraheader` pre-auths so the first request succeeds — needed for the runtime
  loop's push/pull/fetch/clone.
- **Install:** `/usr/local/bin/bubble-git`, `0755`.
- **Env:** `BUBBLE_GH_ORG`, optional `BUBBLE_GH_PERSONAL_OWNER`.
- **Sudoers:** needs `sudo -n /usr/local/bin/bubble-gh-credential-helper.sh get`.

## bubble-gh

Pre-auth `gh` CLI wrapper: mints an App installation token, sets `GH_TOKEN`, then
exec's `gh`.

- **Why it matters:** same private-repo first-request problem as `bubble-git`; the
  `gh` CLI takes `GH_TOKEN` at the start of every API call so there is no
  negotiation issue. Token TTL ~9 min, one per invocation, never written to disk.
- **Install:** `/usr/local/bin/bubble-gh`, `0755`.
- **Env:** `BUBBLE_GH_ORG`, optional `BUBBLE_GH_PERSONAL_OWNER`.
- **Sudoers:** needs `sudo -n /usr/local/bin/bubble-gh-credential-helper.sh get`.

## bubble-is-structural-push.py

Decides whether the un-pushed delta of a git repo touches any STRUCTURAL
(mission-definition) path. Called by the root credential helper to choose between
a `contents:write` token (normal push) and a read-only token (structural push →
must go through a human-merged PR).

- **Why it matters:** the box-side mission-file lock (governance fix 2026-06-01).
  It single-sources the `STRUCTURAL_PATH_GLOBS` from the broker's `policy.py` (by
  file path) so the rule cannot drift, and fails OPEN to write on any uncertainty
  so a legitimate push is never broken.
- **Install:** `/usr/local/bin/bubble-is-structural-push.py`, `0755`.
- **Env:** `BUBBLE_BROKER_POLICY_PY` (optional override; default resolves the
  broker's `src/policy.py` via the sibling layout or `/opt/bubble-token-broker`).

## bubble-layer4-canary.sh

Verifies a dept fixture produced Layer-4 outputs at the scheduled hour tonight and
pings the operator on Telegram with the verdict. Idempotent (writes a daily log).

- **Why it matters:** a canary that catches a silent Layer-4 (risk) dispatch
  failure before it goes unnoticed for a day.
- **Install:** `/usr/local/bin/bubble-layer4-canary.sh`, `0755`, driven by a
  systemd timer after the scheduled hour (e.g. 22:35 UTC).
- **Env:** `BUBBLE_FIXTURE_AGENT`, `BUBBLE_OPERATOR_CHAT_ID`; `TELEGRAM_BOT_TOKEN`
  is sourced from `/run/claude-agent/env` at runtime.
- **Sudoers:** appends to `/var/log/bubble-security/...` via `sudo tee`.

## bubble-watchdog-resume-dropin

Root-owned helper that installs/removes the watchdog's TRANSIENT resume drop-in
with FIXED, non-caller-controlled content. The unprivileged `claude` watchdog
calls it via a tightly-scoped sudoers rule.

- **Why it matters:** closes a privilege-escalation vuln (SPEC-021 FIX-6). The
  prior design let `claude` write arbitrary `[Service]` override content (e.g.
  `User=root`) and escalate via systemd. Here the override content is hardcoded
  root-owned, and the only caller input is the service NAME, validated against a
  strict anchored allowlist.
- **Install:** `/usr/local/bin/bubble-watchdog-resume-dropin`, root-owned `0755`.
- **Sudoers:** `sudo /usr/local/bin/bubble-watchdog-resume-dropin install|remove <svc>`
  for the watchdog; the script itself runs `systemctl daemon-reload`.

## guard-stale-credentials.sh

ExecStartPre guard that prevents a stale `~/.claude/.credentials.json` from
SHADOWING the env `CLAUDE_CODE_OAUTH_TOKEN` (board #294 / incident 2026-06-25:
the shared on-disk creds file expired 2026-06-03 and 401'd all 5 depts, because
claude prefers the on-disk credentials file over the env token).

- **What it does:** if the dept env file provides `CLAUDE_CODE_OAUTH_TOKEN` AND
  `/home/claude/.claude/.credentials.json` exists, moves the file aside
  (`.shadowed-<ts>`) so claude falls back to the env token. Only acts when an env
  token exists to fall back to — never strips the sole available auth.
- **Reversible / fail-open:** renames (never deletes), and always `exit 0` so it
  can never block a dept from starting. Never echoes secret values.
- **Install:** `/usr/local/bin/guard-stale-credentials.sh`, root-owned `0755`.
- **Called by:** `ExecStartPre=+/usr/local/bin/guard-stale-credentials.sh ${ENV_FILE}`
  in `ops-loop-dept.service.template` (runs as root, before `EnvironmentFile`).

## bubble-rotate-dept-secret

Safely ROTATES an existing key in a per-dept SOPS dotenv file
(`/etc/bubble/secrets-<dept>.sops.env`), so no agent ever hand-rolls the sops
invocation again (board #457, closing the exact traps that corrupted a dept's
secrets file in incident #451 — see `shared/systems/sops-dotenv-reencryption-trap.md`).
Complements `morty-sops-add-key`, which is ADD-only and refuses to overwrite an
existing key by design: rotation is the deliberate, separate, verified path
this script provides.

- **Usage:** `printf '%s' "$VALUE" | bubble-rotate-dept-secret <dept> <KEY_NAME> [--probe telegram-bot|none] [--restart] [--file PATH]`
  — the new value is read from **stdin only**, never a CLI arg (argv leaks via
  `ps`/shell history/logs).
- **What it does, in order:** reads stdin into a shred-trapped `/dev/shm`
  work dir → refuses if the target file is already JSON-corrupted (first byte
  `{`) → discovers the age recipient via `grep -oE 'age1[0-9a-z]{50,}'` (NOT
  `sops --extract`, which returns empty on these files) → decrypts the current
  file to a tmpfs baseline (records key count) → builds the new plaintext,
  encrypts with explicit `--input-type dotenv --output-type dotenv` → refuses
  to install if the freshly-encrypted output is JSON → backs up the current
  file to `$F.bak-rotate-<epoch>` (mode `0400`) → installs the new file
  (`root:root 0600`) → decrypts the INSTALLED file to a tmpfs FILE (never
  stdout — the sops-guard Layer-2 wrapper blocks interactive decrypt-to-stdout)
  and asserts the key is present and the key-count did not decrease →
  optionally live-probes the new value (`--probe telegram-bot` → Telegram
  `getMe`) → on ANY verify/probe failure, automatically rolls back from the
  backup and exits nonzero. Never prints the secret value on stdout or stderr.
- **`--restart`:** also runs `systemctl restart ops-loop-<dept>` and greps the
  journal for `sops`/`401` errors; without it, prints the exact manual restart
  + verification steps instead.
- **Install:** `/usr/local/bin/bubble-rotate-dept-secret`, root-owned `0755`
  (writes to `/etc/bubble/secrets-<dept>.sops.env`, requires root).
- **Env overrides (tests only):** `SOPS_BIN`, `TMPFS_DIR`, `SYSTEMCTL_BIN`,
  `CURL_BIN`, `INSTALL_OWNER`, `INSTALL_GROUP`.
- **Tests:** `tests/bubble-rotate-dept-secret/test_bubble_rotate_dept_secret.sh`
  — runs unprivileged against fake `sops`/`curl`/`systemctl` stubs; exercises
  corrupt-JSON refusal, missing-recipient refusal, post-encrypt JSON refusal +
  rollback, missing-key/key-count-drop verify failures + rollback, the
  telegram-bot probe (401 rollback / ok success), and confirms the secret
  value never appears in any captured output.
