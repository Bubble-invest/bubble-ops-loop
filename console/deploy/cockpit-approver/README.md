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
- `console/routes/pr.py` — `GET /pr/{owner}/{repo}/{number}` (deep-link view) +
  `POST /pr/{owner}/{repo}/{number}/approve` (cockpit-auth + RBAC gated, same
  principal check as gate decisions), triggers the approval.
- `console/services/pr_detail_reader.py` — read-only PR detail fetch (title, changed
  files, guard check status) behind the `GET` view above.
- `console/services/structural_paths.py` — re-exports `is_structural_for_repo` from
  `token-broker/src/policy.py` (the SAME policy the guard workflow enforces) so the
  cockpit's "is this PR structural?" UI hint never drifts from the guard's own truth.
- `console/deploy/cockpit-approver/install-cockpit-approver-minter.sh` +
  `deploy/templates/bubble-cockpit-approver.sudoers` — idempotent installer for the
  minter binary + its scoped `sudo -n` grant (see step 2 below).
- `.github/workflows/structural-merge-guard.yml` (already merged) — requires an `APPROVED`
  review by `cockpit-approver[bot]` on the head SHA for any PR touching structural paths.

## Cockpit UI (board #1432 follow-up)

- **`GET /pr/{owner}/{repo}/{number}`** — the per-PR deep-link page (title, changed
  files with structural ones flagged, the guard's check status, and the Approve
  button). This is the single link Rick sends Joris per PR instead of walking him
  through the whole cockpit.
- **Home page "Prêt à merger" cards** (`console/templates/home.html`) now also show
  an "Approve (structural)" button + a "Voir la fiche PR →" link to the page above,
  for any merge-ready PR `merge_ready_reader.py` flagged as touching a structural
  path (`is_structural`, computed via the SAME shared `is_structural_for_repo`
  policy the guard uses — see `console/services/structural_paths.py`).
- Both buttons call the SAME shared `bubbleApprovePR()` JS helper in `base.html`,
  which POSTs to the existing `/pr/.../approve` route and renders its `detail`
  message in place — including the clean "approver key not provisioned yet" state
  before step 1 below is done.

## One-time operator steps — provision the App private key (deferred until Joris is home)

The token minter reads the App's private key from a root-owned SOPS file. Until it exists,
`pr_approver` cleanly reports "approver key not provisioned" and submits nothing (no false
approval).

1. **Drop the `.pem` (SOPS).** On the machine where you downloaded the App's `.pem` at
   App-creation time, SOPS-encrypt it to the VPS age recipient and drop it at:
   `/srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem` (root:root, 0600).
   Use the same secure flow as the other App keys (the `auth` skill / `morty-sops-add-key`
   pattern) — the key never transits chat.
2. **Run the installer:**
   `bash console/deploy/cockpit-approver/install-cockpit-approver-minter.sh`
   (root, on the VPS). Idempotent — installs the minter (0750 root:root) to
   `/usr/local/bin/` and the scoped `sudo -n` grant
   (`/etc/sudoers.d/bubble-cockpit-approver`, mirroring the settings_pr broker-mint
   convention in `deploy/templates/bubble-broker-mint.sudoers`) via `visudo -cf`
   validation. `--dry-run` shows what it would do without writing anything. It does
   NOT touch the key and does NOT run/smoke-test the minter.

   **Known gap to resolve before this is load-bearing** (found while building the
   button/page, not fixed here — needs a decision): the production console unit
   sets `NoNewPrivileges=true`
   (`console/deploy/bubble-ops-console.service.template`), under which `sudo`
   cannot escalate — so `pr_approver.py`'s request-time `sudo -n` call will fail
   exactly the way a request-time `sudo` for contents-token would have (which is
   why THAT minter is instead a root systemd timer writing a tmpfs file the
   console only reads — see `console/deploy/contents-token/README.md`). Either
   drop `NoNewPrivileges` for this unit or migrate `pr_approver.py` to the same
   timer+tmpfs-file pattern before step 3 below is expected to pass in production.
3. **Smoke-test:** `sudo -n /usr/local/bin/bubble-cockpit-approver-token.sh` prints a `ghs_…`
   token; then approve a throwaway structural PR from the cockpit (either the home-page
   button or a `/pr/{owner}/{repo}/{number}` link) and confirm the guard flips to pass.

## Making the check required

After the workflow has run once on a PR (so GitHub knows the check name), add
`structural-merge-guard / guard` to `main`'s required status checks (rulesets) on each
bubble-ops-* repo. Do NOT enable branch-wide "require a pull request before merging" — that
would block the loops' routine runtime direct-pushes; a required *status check* gates PR
merges only.
