# `retire-secrets-quarantine.sh` — secret quarantine on dept retirement

Side effect 2b of `retire_dept.py` (2026-06-05 security fix). When a dept is
retired, its **history** stays reviewable (GitHub repo + transcripts) but its
**live secret access is revoked**.

## What it does (root-owned, idempotent)
1. **Locks the Telegram bot** — `access.json` → `dmPolicy=denied`, `allowFrom=[]`
   (immediate code-level lockout; backup saved as `.bak-retire-<ts>`).
2. **Archives the SOPS env** → `/etc/bubble/retired/secrets-<slug>.sops.env.<ts>`
   (reversible, root 0600 — NOT deleted, in case of un-retire).
3. **Wipes runtime decrypted secrets** — `/run/bubble-<slug>`,
   `/run/claude-agent-<slug>` (tmpfs).
4. **Logs** to `/var/log/bubble-security/secrets-retire-<slug>-<ts>.log` and
   prints the MANUAL steps still required (BotFather token revoke, GitHub App
   install removal — human actions that can't be automated).

## Install (on the VPS, as root)
```bash
install -m 0755 -o root -g root deploy/bin/retire-secrets-quarantine.sh \
  /usr/local/bin/retire-secrets-quarantine.sh
```

## Sudoers (so the claude user — which runs retire_dept.py — can invoke it)
`/etc/sudoers.d/claude-retire-secrets` (mode 0440, validate with `visudo -cf`):
```
claude ALL=(root) NOPASSWD: /usr/local/bin/retire-secrets-quarantine.sh *
```
The helper validates its slug arg and only ever archives/locks/wipes the named
dept; it never deletes the SOPS env (archive only) and is idempotent.

## How retire_dept.py calls it
`_quarantine_secrets(slug)` (Side effect 2b) runs `sudo -n
/usr/local/bin/retire-secrets-quarantine.sh <slug>` (on the VPS) or proxies via
`ssh remote`. Non-blocking: a failure logs a WARN but does not block retirement
(the dept is already disabled, so the security window is bounded).
