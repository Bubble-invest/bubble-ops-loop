# cockpit-approver — App-signed structural-PR approval (board #1432, option C2)

The cockpit approves structural/mission-path PRs AS the `cockpit-approver` GitHub App,
so the `structural-merge-guard` check accepts an approval the fleet's agents cannot forge
(they are `vdk888` with `repo` scope and could self-review; only the cockpit holds this
App's key).

## Pieces

- `bubble-cockpit-approver-token.sh` — root-owned; mints a ~1h `pull_requests:write`
  installation token for App `cockpit-approver` (app_id 5019127, installation 163454332).
- `console/services/pr_approver.py` — calls the minter, then POSTs an `APPROVED` review to
  the PR as the App.
- `console/routes/pr.py` — `POST /pr/{owner}/{repo}/{number}/approve`, cockpit-auth + RBAC
  gated (same principal check as gate decisions), triggers the approval.
- `.github/workflows/structural-merge-guard.yml` (already merged) — requires an `APPROVED`
  review by `cockpit-approver[bot]` on the head SHA for any PR touching structural paths.

## One-time operator step — provision the App private key (deferred until Joris is home)

The token minter reads the App's private key from a root-owned SOPS file. Until it exists,
`pr_approver` cleanly reports "approver key not provisioned" and submits nothing (no false
approval). To provision:

1. On the machine where you downloaded the App's `.pem` at App-creation time, SOPS-encrypt it
   to the VPS age recipient and drop it at:
   `/srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem` (root:root, 0600).
   Use the same secure flow as the other App keys (the `auth` skill / `morty-sops-add-key`
   pattern) — the key never transits chat.
2. Install the minter: `install -o root -g root -m 0750 bubble-cockpit-approver-token.sh \
   /usr/local/bin/bubble-cockpit-approver-token.sh` and grant the console user a scoped
   `sudo -n` for it (mirroring the contents-token grant).
3. Smoke-test: `sudo -n /usr/local/bin/bubble-cockpit-approver-token.sh` prints a `ghs_…`
   token; then approve a throwaway structural PR from the cockpit and confirm the guard flips
   to pass.

## Making the check required

After the workflow has run once on a PR (so GitHub knows the check name), add
`structural-merge-guard / guard` to `main`'s required status checks (rulesets) on each
bubble-ops-* repo. Do NOT enable branch-wide "require a pull request before merging" — that
would block the loops' routine runtime direct-pushes; a required *status check* gates PR
merges only.
