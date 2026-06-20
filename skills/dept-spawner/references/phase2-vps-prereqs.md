# Phase 2 — VPS prerequisites (operator, manual) + deploy

**The order is load-bearing.** Each step prevents a specific "service active but silent" failure.
Do them in order; do not skip.

## 2a. GitHub App coverage
If `bubble-ops-bot` install (id 135214360) is `repository_selection=ALL` on the org → new repos
auto-covered, nothing to do. Otherwise the human clicks "add repo" in the App UI (setup callback).
*Prevents:* git pushes failing because the App can't see the repo.

## 2b. Broker policy (do before the service starts)
`/opt/bubble-token-broker/deploy/policies/<slug>-policy.yaml` — root-owned, cloned from maya's
policy with slug-swap. scaffold.py renders a canonical version at `deploy/policies/<slug>-policy.yaml`;
the operator copies it to the broker's policies dir.
*Prevents:* git-guard blocking ALL pushes (fails closed) if the policy is missing.

## 2c. Secrets (SOPS) — human-handled
`/etc/bubble/secrets-<slug>.sops.env` with:
- `DEPT_TELEGRAM_BOT_TOKEN` (from BotFather — created by the human's Telegram account)
- `CLAUDE_CODE_OAUTH_TOKEN` (copied from an existing dept per human OK)
- `NOTION_API_KEY` (copied)
Create in `/dev/shm`, shred plaintext immediately, encrypt to BOTH age recipients (box + Mac).
Use `bubble-set-secret` / the `auth` skill — never put raw secret values in chat/logs.
*Prevents:* the agent having no identity / no bot to talk through.

## 2d. Channel directory (needs the bot token from 2c)
`/home/claude/.claude/channels/telegram-<slug>/`:
- `.env` (`TELEGRAM_BOT_TOKEN=<token>`, mode 600)
- `access.json` (`allowFrom`: {{OPERATOR}} {{OPERATOR_CHAT_ID}}, {{OPERATOR_2}} {{OPERATOR_2_CHAT_ID}})
- `approved/` and `inbox/` dirs
*Prevents:* the bot never receiving messages.

## 2e. Folder trust (as the claude OS user) — the most common silent-hang cause
```python
import json; f="/home/claude/.claude.json"; d=json.load(open(f))
p="/home/claude/agents/bubble-ops-<slug>"
d["projects"].setdefault(p,{})["hasTrustDialogAccepted"]=True
json.dump(d, open(f,"w"), indent=2)
```
*Prevents:* headless `claude` hanging forever on the "Do you trust this folder?" modal.

## 2f. Telegram watchdog (4 artifacts, slug-swap from maya — deploy-to-morty does NOT render these)
- `telegram-watchdog-<slug>.sh` (**chmod 0755** or systemd fails 203/EXEC)
- `telegram-watchdog-<slug>.service` + `.timer`
- `/etc/sudoers.d/claude-telegram-watchdog-<slug>` (**validate with `visudo -cf` BEFORE install**)
Then: `systemctl daemon-reload && systemctl enable --now telegram-watchdog-<slug>.timer`
*Prevents:* a deaf agent never getting auto-healed.

## 2g. Systemd unit install
```bash
./scripts/deploy-to-morty.sh --slug=<slug> --remote=root@
```
Renders `deploy/templates/ops-loop-dept.service.template` (model pin, boot-rearm env, script(1)
pty) → `/etc/systemd/system/ops-loop-<slug>.service`, plus the App-id drop-in
`ops-loop-<slug>.service.d/github-app-id.conf`. Then `systemctl daemon-reload && systemctl
enable --now ops-loop-<slug>`.

## 2h. Clone to agents/ (WITH the bubble-ops- prefix)
Clone the dept repo to `/home/claude/agents/bubble-ops-<slug>` — the prefix is how the
fleet-backup cron auto-discovers depts.

## Verify before Phase 3
- `systemctl is-active ops-loop-<slug>` = active.
- The bun/telegram poller spawned (not hung on the trust modal).
- A test message to the new bot reaches the session.
