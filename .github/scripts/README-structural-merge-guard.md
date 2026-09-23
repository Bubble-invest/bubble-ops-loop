# Structural merge guard (board #1432, option C2)

Makes the "don't self-merge mission/mandate/loop/agent-def + framework paths"
guard a **hard, un-bypassable** control on PR merges — closing the gap where a
`vdk888` agent (Rick or any worker) can approve and merge its own structural PR
because it holds `repo` scope.

## How it works

`structural-merge-guard.yml` runs on every PR. The `structural_merge_guard.py`
check:

1. Lists the PR's changed files and asks `token-broker/src/policy.py`'s
   `is_structural_for_repo()` whether any are structural — the **same** path set
   the runtime push guard already enforces, so PR and push protection never drift.
2. If none are structural → **pass** (ordinary PRs are unaffected).
3. If some are → it requires an **APPROVED review authored by the cockpit App bot**
   (`vars.APPROVER_BOT`, default `cockpit-approver[bot]`), bound to the
   **current head SHA** (so an approve-then-push-a-structural-change cannot slip
   past). A review by any other identity — **including `vdk888`** — does not count.

Only the cockpit holds the App's key, so an approval by the App bot is a signal
the fleet's agents provably cannot forge. Runtime direct-pushes to `main` never go
through a PR, so they are untouched by this check.

## Enabling it (operator / Joris steps — account & settings, not code)

1. **Create the GitHub App** (slug `cockpit-approver`). Give it, per repo,
   Pull requests: **Read & write** (to submit reviews). Install it on the
   bubble-ops-* repos. Its review author login is `cockpit-approver[bot]`
   (set `vars.APPROVER_BOT` if you name it differently).
2. **Cockpit "approve PR" action**: add a control in the cockpit that, when Joris
   approves a structural PR, uses the App's key to submit an `APPROVED` review on
   that PR as the App. (Lighter than teaching the cockpit a full PR backend — the
   proof lives on the PR, authored by the App.)
3. **Make the check required**: in each repo's branch protection / ruleset for
   `main`, add `structural-merge-guard / guard` to **required status checks**.
   Do **not** enable "require a pull request before merging" branch-wide — that
   would block the loops' routine runtime direct-pushes. A required *status check*
   gates PR merges without touching direct pushes.

## Roll-out

Prototype on `bubble-ops-loop` first, confirm it blocks a structural self-merge
and passes a non-structural PR, then drop the same workflow + script into each
`bubble-ops-*` repo (or a reusable org workflow). The script needs `policy.py`;
dept repos vendor it, or reference the reusable workflow in bubble-ops-loop.

## Local test

```bash
# structural path, no App approval -> exit 1 (blocked)
echo '{"files":[{"path":"MANDATE.md"}],"reviews":[]}' \
  | python3 .github/scripts/structural_merge_guard.py --repo Bubble-invest/bubble-ops-loop --head-sha abc
# structural path, App approval on head -> exit 0 (allowed)
echo '{"files":[{"path":"MANDATE.md"}],"reviews":[{"user":{"login":"cockpit-approver[bot]"},"state":"APPROVED","commit_id":"abc"}]}' \
  | python3 .github/scripts/structural_merge_guard.py --repo Bubble-invest/bubble-ops-loop --head-sha abc
# non-structural path -> exit 0 (allowed)
echo '{"files":[{"path":"outputs/x.md"}],"reviews":[]}' \
  | python3 .github/scripts/structural_merge_guard.py --repo Bubble-invest/bubble-ops-loop --head-sha abc
```
