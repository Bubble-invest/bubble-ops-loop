# Installing the Ben example agent

Ben is a **folder** (`agents/ben/`) inside the `bubble-ops-loop` repo, not a
standalone repo. You install it either:

- **Locally** (a `/loop` runner as a macOS launchd agent), or
- **On a VPS** (a `/loop` runner as a systemd unit).

Both use the framework's existing loop runners. You point the runner at this
folder as the dept directory. **No change to `bootstrap-dept.sh` is needed** —
that script scaffolds a *blank* new dept and creates a GitHub repo; here you
already have the dept (this folder), so you skip straight to wiring the runner.

> **Demo-first.** With no broker secrets in the environment, Ben runs
> proposals-only on the synthetic book — it never reaches a broker. That is the
> safe default for trying it out. Arm secrets only when your mandate is signed.

---

## 0. Prerequisites

- `claude` (Claude Code) on PATH.
- Python 3.10+.
- Build the synthetic database once:

  ```bash
  python3 agents/ben/data/seed_fund.py
  ```

- (Optional, only for the Alpaca worked example) `pip install alpaca-py`. The
  stub demo does NOT need it.

---

## 1. Local install (macOS, launchd)

The framework ships a generic local loop installer:
`deploy/local/install-local-loop.sh`. It renders a launchd KeepAlive agent that
runs a persistent `claude` session whose cwd is the dept folder, driven by the
dept's `CLAUDE.md` `/loop` protocol.

```bash
# From the repo root. Use the absolute path to this folder as the dept dir.
DEPT_DIR="$(pwd)/agents/ben"

# Dry-run first (renders the wrapper + plist, does NOT load launchd):
bash deploy/local/install-local-loop.sh --dept-dir "$DEPT_DIR" --slug ben

# When the rendered files look right, activate (loads the launchd agent):
bash deploy/local/install-local-loop.sh --dept-dir "$DEPT_DIR" --slug ben --activate
```

This installs:
- `~/Library/Application Support/bubble-ops-loop/ops-loop-ben-wrapper.sh`
- `~/Library/LaunchAgents/com.bubble.ops-loop-ben.plist` (KeepAlive + RunAtLoad)

Optional backup floor (a stale-heartbeat backstop that forces a layer tick if the
live loop dies): `deploy/local/install-local-loop-backup.sh` (see
`deploy/local/README.md`).

To remove:

```bash
bash deploy/local/install-local-loop.sh --uninstall --slug ben
```

### Local credentials

Locally the runner uses your own shell environment. Export any broker creds in
your shell profile (or a launchd `EnvironmentVariables` entry) — the
`broker-adapter` reads them from `os.environ`. For the demo, set nothing: the
agent stays proposals-only. To exercise the Alpaca example on **paper**:

```bash
export ALPACA_API_KEY=...        # your paper key
export ALPACA_SECRET_KEY=...     # your paper secret
# ALPACA_PAPER unset => PAPER (default). Set ALPACA_PAPER=false only for live.
```

Never commit these. Never put them in a file inside the repo.

---

## 2. VPS install (systemd)

On a VPS the framework runs each dept as a `ops-loop-<slug>.service` systemd unit,
rendered from `deploy/templates/ops-loop-dept.service.template`. The full
box-level manifest is in `deploy/INSTALL.md`. For an example agent you only need
the per-dept unit pointed at this folder.

1. Place this folder on the box where the runner expects dept directories (the
   layer-floor crons auto-discover `bubble-ops-*` dept dirs; for an example you
   can run it as a standalone unit pointed at `agents/ben`).
2. Render + install the unit from the template, substituting the dept slug
   (`ben`) and the dept directory. The canonical helper is
   `scripts/deploy-to-morty.sh --slug=ben` (it re-renders from the template and
   installs/reloads the unit), or hand-render the template if you run a different
   host layout.
3. Provide credentials via the SOPS-decrypted env file the unit loads into the
   process environment (the template decrypts a per-dept secrets file to
   `/run/claude-agent-ben/env`). Use the `auth` skill's `operator-set-secret`
   flow to write a secret without it ever touching cleartext on disk — e.g. a
   broker key:

   ```bash
   operator-set-secret.sh \
     --remote-prompt=<host> \
     --project=/etc/bubble/secrets-ben.sops.env \
     --key=ALPACA_API_KEY
   ```

   Do NOT edit the `.sops.env` file directly. Always go through the auth helper.

4. Start the unit:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now ops-loop-ben.service
   ```

The layer-floor crons (`scripts/install-loop-backup.sh`, 4 units) and the
liveness watchdog from `deploy/INSTALL.md` then cover Ben automatically once its
unit is enabled and it has `layers/N/PROMPT.md` (it does).

---

## 3. Verify it's running

- **Local:** `launchctl list | grep ops-loop-ben`, and check the log dir printed
  by the installer.
- **VPS:** `systemctl status ops-loop-ben.service` and `journalctl -u ops-loop-ben`.
- **Either:** after a tick, look for a heartbeat line under
  `agents/ben/outputs/<today>/heartbeat.log` and a situation brief under
  `outputs/<today>/1/`.

With no secrets armed you should see L1/L2 produce a **proposal card** in
`queues/gates/` and never a broker call — the intended demo state.

---

## 4. Going live (when you mean it)

1. Rewrite `MANDATE.md` for your real perimeter and have a human sign it (§10).
2. Replace `data/` with your real book.
3. Implement a real adapter behind `skills/broker-adapter/` (copy
   `adapters/_template.py`) and register it.
4. Arm the broker secrets in the environment (locally: shell/launchd; VPS: the
   SOPS env file).

Until the mandate is signed AND secrets are armed, the agent stays
proposals-only — by design (`CLAUDE.md`, `MANDATE.md` §6/§10).
