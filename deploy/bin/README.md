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
