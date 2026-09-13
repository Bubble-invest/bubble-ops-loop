# #1253 token-refresh retry deployment

This change installs bounded retry/backoff in the two existing GitHub App token
refresh wrappers. It does not change the minters, encrypted PEM, age key,
systemd units/timers, token destinations, scopes, or consumers.

Each wrapper attempts to mint at most four times, sleeping 5s, 10s, then 20s
(about 35s total delay). A successful mint is validated before the existing
atomic destination-file replacement. Exhaustion returns nonzero and leaves the
previous token file untouched. Journal diagnostics never forward helper stdout
or stderr and never include token material.

## Install after merge

Run as root on `joris-cx33` after the reviewed PR is merged. The checkout must be
clean and on the deployed `main` branch before pulling; do not stash, reset, or
overwrite a dirty/ahead/non-main checkout.

```bash
(
set -euo pipefail
cd /opt/bubble-ops-loop
git status --short --branch
test -z "$(git status --porcelain)"
test "$(git branch --show-current)" = main
git fetch origin
git pull --ff-only origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"

install -m 0755 -o root -g root \
  deploy/bin/bubble-board-token-refresh.sh \
  /usr/local/bin/bubble-board-token-refresh.sh
install -m 0750 -o root -g root \
  console/deploy/contents-token/bubble-ops-contents-token-refresh.sh \
  /usr/local/bin/bubble-ops-contents-token-refresh.sh

bash -n /usr/local/bin/bubble-board-token-refresh.sh
bash -n /usr/local/bin/bubble-ops-contents-token-refresh.sh
)
```

No `systemctl daemon-reload` is required because no unit changed. Do not restart
the console, any agent, or any fund/broker service for this deployment.

## Mint once and verify without printing credentials

The only service actions needed are manual starts of these two oneshot refresh
units. `systemctl start` waits for the bounded retry sequence and returns
nonzero if all four attempts fail.

```bash
(
set -euo pipefail
systemctl start bubble-board-token-refresh.service
systemctl start bubble-ops-contents-token-refresh.service

test "$(systemctl show bubble-board-token-refresh.service --property=Result --value)" = success
test "$(systemctl show bubble-board-token-refresh.service --property=ExecMainCode --value)" = exited
test "$(systemctl show bubble-board-token-refresh.service --property=ExecMainStatus --value)" = 0
test "$(systemctl show bubble-ops-contents-token-refresh.service --property=Result --value)" = success
test "$(systemctl show bubble-ops-contents-token-refresh.service --property=ExecMainCode --value)" = exited
test "$(systemctl show bubble-ops-contents-token-refresh.service --property=ExecMainStatus --value)" = 0

journalctl -u bubble-board-token-refresh.service -n 30 --no-pager
journalctl -u bubble-ops-contents-token-refresh.service -n 30 --no-pager

test -s /run/bubble-board/token
runuser -u claude -- test -r /run/bubble-board/token
test -s /run/bubble-ops-contents/token
runuser -u claude -- test -r /run/bubble-ops-contents/token
)
```

Expected unit properties are `Result=success`, `ExecMainCode=exited`, and
`ExecMainStatus=0`. A recovery after a transient failure produces only sanitized
messages such as `mint attempt 1/4 failed (helper exit 1; output withheld)` and
`mint succeeded on attempt 2/4`. None of the verification commands displays a
token.
