# Contents-token minter — host=local gate-decision delivery

The cockpit must commit an operator's gate decision to a **host=local** dept's
GitHub repo (e.g. `Bubble-invest/bubble-ops-content`) so that dept's loop (on its
own Mac) pulls it. The console runs as `claude` with `NoNewPrivileges=yes` and no
`gh auth` — so it cannot authenticate to GitHub on its own.

This mirrors the existing **board token** mechanism (`bubble-board-token*`, which
mints an *issues*-only token for the kanban) but mints a **contents:write** token
into a tmpfs file the console reads.

```
bubble-ops-contents-token.sh          → mints contents:write+metadata token (root-only)
bubble-ops-contents-token-refresh.sh  → writes it to /run/bubble-ops-contents/token (0640 root:claude)
bubble-ops-contents-token-refresh.{service,timer} → re-mint every 45 min
```

`console/services/github_reader.py::_read_contents_token()` reads
`/run/bubble-ops-contents/token` (env `GH_TOKEN`/`GITHUB_TOKEN` fallback for
dev/CI) and passes it to `gh api` via `GH_TOKEN` for the decision PUT.

## Install (run as root on the VPS — joris-cx33)

```bash
# 1. Place the minter + refresher
install -m 0750 -o root -g root bubble-ops-contents-token.sh \
    /usr/local/bin/bubble-ops-contents-token.sh
install -m 0750 -o root -g root bubble-ops-contents-token-refresh.sh \
    /usr/local/bin/bubble-ops-contents-token-refresh.sh

# 2. Place the unit + timer
install -m 0644 bubble-ops-contents-token-refresh.service \
    /etc/systemd/system/bubble-ops-contents-token-refresh.service
install -m 0644 bubble-ops-contents-token-refresh.timer \
    /etc/systemd/system/bubble-ops-contents-token-refresh.timer

# 3. Enable + start
systemctl daemon-reload
systemctl enable --now bubble-ops-contents-token-refresh.timer
systemctl start bubble-ops-contents-token-refresh.service   # mint immediately

# 4. Verify the token landed (claude-readable, ghs_*)
ls -l /run/bubble-ops-contents/token
sudo -u claude head -c4 /run/bubble-ops-contents/token   # → ghs_
```

## Verify end-to-end (after the console is redeployed at the new code)

```bash
# As the claude service user, the cockpit's PUT should now succeed:
GH_TOKEN=$(cat /run/bubble-ops-contents/token) \
  gh api repos/Bubble-invest/bubble-ops-content --jq .full_name
# → Bubble-invest/bubble-ops-content   (was: auth error before this change)
```

## Security notes
- The App PEM stays root-only (SOPS-encrypted at
  `/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem`).
- The minted token is **contents:write + metadata:read only**, ~1h-lived, on
  tmpfs, never persisted to disk, group-readable by `claude` only.
- This grants the cockpit write access to repo *contents* across the
  Bubble-invest installation. That is the capability required to deliver
  decisions to any host=local dept repo. If tighter per-repo scoping is wanted
  later, the App token request can name `repositories` explicitly.
