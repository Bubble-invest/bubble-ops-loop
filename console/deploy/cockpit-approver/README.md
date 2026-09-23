# cockpit-approver — App-signed structural-PR approval (board #1432, option C2)
# + the unforgeable `structural-approval` status (board #1462)

The cockpit approves structural/mission-path PRs AS the `cockpit-approver` GitHub App,
so the merge gate accepts an approval the fleet's agents cannot forge (they are
`vdk888` with `repo` + `workflow` scope and could self-review; only the cockpit
holds this App's key).

**Board #1462 (read this if you're here after #1432):** the original gate — a
GitHub Actions job named `guard` required in the ruleset — turned out to be
forgeable too: `structural-merge-guard.yml` checks out the PR's OWN ref and
runs the PR's OWN `.github/scripts/structural_merge_guard.py`, and `vdk888`'s
`workflow` scope lets a PR edit either file (or add a fake job literally named
`guard`) to satisfy the check regardless of what it verifies. The fix is to
move the REQUIRED check to a commit status — context `structural-approval` —
that only the App can author (a ruleset can require `integration_id=5019127`
on a status, and nothing PR-editable can mint that App's installation token).
See "Making the check required (board #1462)" below for the exact ruleset
change and `console/services/structural_status.py` for the evaluation logic.

## Pieces

- `bubble-cockpit-approver-token.sh` — root-owned; mints a ~1h `pull_requests:write` +
  `statuses:write` + `metadata:read` installation token for App `cockpit-approver`
  (app_id 5019127, installation 163454332). `statuses:write` (board #1462) lets the
  App post the unforgeable `structural-approval` commit status; Joris has granted +
  accepted that permission on the installation.
- `bubble-cockpit-approver-token-refresh.{sh,service,timer}` — a root-owned systemd
  TIMER (every ~45min) that calls the minter above and writes the token to
  `/run/bubble-cockpit-approver/token` (tmpfs, 0640 root:bubble-console — group-readable by
  the console's dedicated `bubble-console` user (board #1463; was `claude`), **never** via `sudo`). **1:1 mirror of
  `console/deploy/contents-token/`** — same file layout, same perms, same cadence,
  same fail-closed behaviour when the key isn't provisioned yet.
- `console/services/pr_approver.py` — READS that token file (no subprocess, no sudo),
  then POSTs an `APPROVED` review to the PR as the App. Mirrors
  `console/services/github_reader.py::_read_contents_token()`'s reader shape exactly.
- `console/routes/pr.py` — `GET /pr/{owner}/{repo}/{number}` (deep-link view) +
  `POST /pr/{owner}/{repo}/{number}/approve` (cockpit-auth + RBAC gated, same
  principal check as gate decisions), triggers the approval.
- `console/services/pr_detail_reader.py` — read-only PR detail fetch (title, changed
  files, guard check status) behind the `GET` view above.
- `console/services/structural_paths.py` — re-exports `is_structural_for_repo` from
  `token-broker/src/policy.py` (the SAME policy the guard workflow enforces) so the
  cockpit's "is this PR structural?" UI hint never drifts from the guard's own truth.
- `console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh` — idempotent
  installer for the minter + refresh timer (see step 2 below). There is **no sudoers
  grant anywhere in this feature** — the console never invokes the minter, only reads
  the file the timer writes.
- `.github/workflows/structural-merge-guard.yml` (already merged) — requires an `APPROVED`
  review by `cockpit-approver[bot]` on the head SHA for any PR touching structural paths.
  **Board #1462: this Actions check stays INFORMATIONAL only** (see below) — it is
  no longer the required gate, precisely because a PR can edit it.
- `console/services/structural_status.py` (board #1462) — `evaluate(owner, repo,
  number)` re-reads the PR's files + reviews via the App token and POSTs the
  `structural-approval` commit status: `success` ("not structural") when no
  structural path is touched, `success` ("approved by cockpit on <sha>") when a
  `cockpit-approver[bot]` `APPROVED` review is pinned to the CURRENT head SHA,
  else `pending` ("needs Joris's cockpit approval"). Idempotent (`post_status`
  skips an unchanged status) and fails CLOSED (no App token -> posts nothing).
  Called from two places:
    - `console/routes/pr.py`'s `approve_structural_pr`, right after
      `pr_approver.submit_structural_pr_approval()` returns `"approved"`, so
      an Approve click reflects on the PR immediately;
    - `console/scripts/structural_status_sweep.py`, run every ~2min by
      `bubble-structural-status-sweep.{service,timer}` — lists every open PR on
      every `Bubble-invest/bubble-ops-*` repo the App is installed on (via
      `GET /installation/repositories`, so a newly-spawned dept repo is picked
      up automatically) and (re-)evaluates each one. This is what gives a
      NON-structural PR (nobody ever clicks Approve on those) its own `success`
      status, and what catches a structural PR approved from raw GitHub rather
      than the cockpit.
    - `console/deploy/cockpit-approver/install-structural-status-sweep.sh` —
      idempotent installer for the sweep's wrapper + unit + timer (runs as
      `claude`, not root — it only reads the already-minted token file, same
      as the console itself; see the script's own header for why this is a
      SEPARATE installer from the token-refresh one above).

### Why a timer + tmpfs file, not `sudo -n` at request time

The production console unit sets `NoNewPrivileges=true`
(`console/deploy/bubble-ops-console.service.template`). Under `NoNewPrivileges=true`,
`sudo` (a setuid-root binary) cannot escalate — a request-time `sudo -n` call from the
console process would simply fail. This is the exact problem
`console/deploy/contents-token/` already solved: instead of the console sudo-ing at
request time, a root-owned systemd timer mints on its own schedule and writes a tmpfs
file the console only *reads*. This feature was originally shipped with a `sudo -n`
call (see PR #482's first revision) before that conflict was caught in review; it now
mirrors the contents-token shape 1:1 instead.

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
- `GET`/`POST` are both scoped to `Bubble-invest/bubble-ops-*` (`_is_allowed_repo`
  in `console/routes/pr.py`) — not a security boundary on its own (the App
  installation token can't produce a valid review on a repo it isn't installed
  on regardless), but it closes a repo-existence probe an arbitrary owner/repo
  could otherwise use against the GET view's 404-vs-degraded-fields behaviour.

### Head-SHA pinning (board #1432 review, blocking finding)

The Approve button sends the EXACT commit SHA displayed on the page it was
clicked from (`head_sha`, a query param on the POST) — REQUIRED: the route
400s if it's missing or not a 40-hex-char SHA. `pr_approver.py` then
independently re-fetches the PR's CURRENT head and refuses (`"stale"` -> HTTP
409, nothing posted) if it no longer matches, before finally posting the
review with `commit_id` pinned to that SHA. This closes a TOCTOU window: a
PR's own author (an explicitly adversarial fleet agent in this design) could
otherwise push a new commit between "Joris reads the diff" and "Joris clicks
Approve," and an un-pinned review would silently land on GitHub's THEN-current
HEAD — a diff Joris never saw — since `structural_merge_guard.py` accepts any
`cockpit-approver[bot]` `APPROVED` review whose `commit_id` matches the head at
guard-run time.

## One-time operator steps — provision the App private key (deferred until Joris is home)

The refresh timer mints from the App's private key, read from a root-owned SOPS file.
Until it exists, every mint attempt fails closed, the token file is never written, and
`pr_approver` cleanly reports "approver key not provisioned" — submits nothing (no
false approval).

1. **Drop the `.pem` (SOPS).** On the machine where you downloaded the App's `.pem` at
   App-creation time, SOPS-encrypt it to the VPS age recipient and drop it at:
   `/srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem` (root:root, 0600).
   Use the same secure flow as the other App keys (the `auth` skill / `morty-sops-add-key`
   pattern) — the key never transits chat.
2. **Run the installer:**
   `bash console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh`
   (root, on the VPS). Idempotent — installs the minter + refresh script (0750
   root:root) to `/usr/local/bin/`, the unit + timer (0644) to
   `/etc/systemd/system/`, then `daemon-reload` + `enable --now` the timer.
   `--dry-run` shows what it would do without writing anything; `--mint-now` also
   fires one immediate mint (useful right after dropping the key, instead of
   waiting for `OnBootSec=30s`/the next boot). No sudoers file, no `sudo` grant —
   there's nothing to grant; the console only reads the tmpfs file this timer writes.
3. **Smoke-test:**
   ```bash
   test -s /run/bubble-cockpit-approver/token          # the timer minted something
   sudo -u bubble-console test -r /run/bubble-cockpit-approver/token   # console can read it (board #1463)
   sudo -u claude test -r /run/bubble-cockpit-approver/token && echo BAD-READABLE || echo "OK — claude denied"
   ```
   then approve a throwaway structural PR from the cockpit (either the home-page
   button or a `/pr/{owner}/{repo}/{number}` link) and confirm a `structural-approval`
   status posts on the PR (board #1462 — see step 4 below for making it required).
4. **Install the periodic sweep** (board #1462, so non-structural PRs and PRs
   approved outside the cockpit also get a status):
   `bash console/deploy/cockpit-approver/install-structural-status-sweep.sh --run-now`
   (root, on the VPS — run AFTER step 2/3 above, the sweep is a no-op until the
   token file exists). Verify with
   `journalctl -u bubble-structural-status-sweep.service -n 40 --no-pager`.

## Making the check required (board #1462)

**Do not require the `guard` Actions check** (board #1462: it is forgeable — a
PR can edit `.github/workflows/structural-merge-guard.yml` or
`.github/scripts/structural_merge_guard.py`, or add its own job literally named
`guard`, since the fleet's `vdk888` identity holds `workflow` scope). Require
the App-posted status instead:

1. After `structural_status.evaluate()` has posted at least once on a PR (so
   GitHub knows the context name — approve or sweep a throwaway PR first, see
   step 3 above), open the repo's ruleset (Settings → Rules → Rulesets) that
   protects `main`.
2. Under "Require status checks to pass", add a check with:
   - **Context:** `structural-approval`
   - **Integration:** `cockpit-approver` (app id `5019127`) — GitHub lets you
     pin a required status check to a specific App/integration ID, which is
     the whole point: a status posted by any OTHER identity (including
     `vdk888`, including a forged Actions job) does not satisfy this
     requirement, even if it uses the exact same context string.
3. **Remove** `structural-merge-guard / guard` from the required list on the
   same ruleset (keep the workflow file itself — it stays informational, a
   second opinion in the PR checks tab, just no longer load-bearing).
4. Repeat on every `Bubble-invest/bubble-ops-*` repo the App is installed on
   (same rollout scope board #1432 already used).
5. Do NOT enable branch-wide "require a pull request before merging" — that
   would block the loops' routine runtime direct-pushes; a required *status
   check* gates PR merges only.

Joris makes this ruleset change by hand (GitHub Settings UI/API, not this repo) —
nothing in this PR does it automatically.
