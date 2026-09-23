"""App-posted `structural-approval` commit status — the unforgeable gate (board #1462).

Board #1462: the REQUIRED status check `guard` (a GitHub Actions job) can be
forged today — `.github/workflows/structural-merge-guard.yml` checks out the
PR's OWN ref and runs the PR's OWN `.github/scripts/structural_merge_guard.py`,
and the fleet's shared git identity (`vdk888`) holds `workflow` scope, so a PR
can edit either file (or add a fake job literally named `guard`) to satisfy
the check regardless of what it actually verifies.

This module evaluates the SAME policy SERVER-SIDE — the path policy comes
from `structural_paths.py` (not from anything a PR can edit), and the review
check reads GitHub directly via the cockpit-approver App's own token — then
POSTs the verdict as a commit status under context `structural-approval`.
Once the ruleset requires that context with `integration_id=5019127` (the
App's id), only an App-authored status can ever satisfy it: a PR editing its
own workflow/scripts can no longer forge the merge gate, because nothing PR-
editable can mint the App's installation token or author a status as it.

Called from two places:
  - `console/routes/pr.py`'s `approve_structural_pr` — right after
    `pr_approver.submit_structural_pr_approval()` returns `"approved"`, it
    calls `post_status(..., "success", ...)` directly, so an operator's
    Approve click reflects on the PR right away instead of waiting for the
    periodic sweep below. (Deliberately NOT called from inside
    `pr_approver.py` itself: that module is also imported at App-approval
    time by test fixtures that reset `sys.modules` per test — a lazy
    cross-import there would bind a stale copy of this module's functions
    under that harness. The route layer already imports both services, so
    it's the natural, cycle-free place to sequence "approve, then post the
    status".)
  - the periodic sweep (`console/scripts/structural_status_sweep.py`, run
    every ~2min by `bubble-structural-status-sweep.timer`) — evaluates every
    open PR across the Bubble-invest/bubble-ops-* repos the App is installed
    on, so non-structural PRs (nobody ever clicks Approve on those) and
    structural PRs approved outside the cockpit both get a current status
    without anyone clicking anything in the cockpit.

Deliberately calls `pr_approver._get` / `pr_approver._post` /
`pr_approver._mint_token` THROUGH the `pr_approver` module object
(`pr_approver.xxx(...)`), never via a `from ... import xxx` copy — so any
test (or call site) that monkeypatches those names on the `pr_approver`
module transparently covers calls made from here too, and there is exactly
ONE token-reading + HTTP implementation in this codebase to keep honest, not
two independently-drifting ones.

Fails CLOSED throughout, same contract as `pr_approver`:
  - no App token yet -> ("not_provisioned", ...), nothing posted.
  - a GitHub fetch error -> ("error", ...), nothing posted (there is nothing
    verified yet to report).
Idempotent: `post_status` skips the POST when an identical `structural-
approval` status (same state + description) already sits on that sha.
"""
from __future__ import annotations

from console.services import pr_approver
from console.services.structural_paths import is_structural_for_repo

_API = "https://api.github.com"

# The required-status-check context this module owns end to end (evaluate,
# post, and the ruleset entry Joris configures per the README).
STATUS_CONTEXT = "structural-approval"


def _latest_status(owner: str, repo: str, sha: str, token: str) -> dict | None:
    """Best-effort read of the current `structural-approval` status on `sha`
    (used only for the idempotence check in `post_status`). Any fetch problem
    reads as "no existing status" — worst case that re-POSTs an identical
    status, a harmless no-op write, never a wrong or missing one.
    """
    try:
        status, body = pr_approver._get(
            f"{_API}/repos/{owner}/{repo}/commits/{sha}/status", token)
    except Exception:  # noqa: BLE001 — best-effort only, never fatal to the caller
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    for s in body.get("statuses") or []:
        if s.get("context") == STATUS_CONTEXT:
            return s
    return None


def post_status(owner: str, repo: str, sha: str, state: str, description: str,
                 token: str | None = None) -> tuple[bool, str]:
    """POST the `structural-approval` commit status onto `sha`, as the App.

    Returns (ok, message). `ok=False` with a "not provisioned" message when
    there is no App token yet — fails CLOSED, posts nothing (mirrors
    `pr_approver.submit_structural_pr_approval`'s own `not_provisioned`
    contract: this feature exists specifically so nothing PR-editable can
    ever produce this status, so an absent token must never fall back to
    posting as some other, forgeable identity).

    Idempotent: skips the POST when `sha` already carries an identical
    `structural-approval` status (same state + description) — the periodic
    sweep calls this every ~2min for every open PR, and there is no reason
    to keep re-writing an unchanged verdict.
    """
    token = token or pr_approver._mint_token()
    if not token:
        return (False, "cockpit-approver App key not provisioned yet — nothing posted.")

    existing = _latest_status(owner, repo, sha, token)
    if existing and existing.get("state") == state and existing.get("description") == description:
        return (True, f"structural-approval already {state} on {sha[:12]} — unchanged, skipped.")

    status, resp = pr_approver._post(
        f"{_API}/repos/{owner}/{repo}/statuses/{sha}",
        token,
        {"state": state, "context": STATUS_CONTEXT, "description": description[:140]},
    )
    if status in (200, 201):
        return (True, f"posted structural-approval={state} on {sha[:12]}.")
    return (False, f"GitHub {status}: {resp.get('message', 'status POST failed')}")


def evaluate(owner: str, repo: str, number: int, token: str | None = None) -> tuple[str, str]:
    """Evaluate PR #{number} in {owner}/{repo} against the structural-path +
    App-approval policy, and POST the `structural-approval` status onto its
    CURRENT head SHA.

    Returns (state, description):
      "success"         — the PR touches no structural path (per
                           `structural_paths.is_structural_for_repo`), OR it
                           does and carries a `cockpit-approver[bot]`
                           APPROVED review pinned (`commit_id`) to the
                           CURRENT head SHA.
      "pending"         — the PR touches a structural path and no such
                           approval exists yet.
      "error"           — could not fetch the PR / its files / its reviews
                           from GitHub, or the status POST itself failed.
                           Nothing is posted for a fetch failure — there is
                           nothing verified yet to report.
      "not_provisioned" — no App token yet; nothing posted.
    """
    token = token or pr_approver._mint_token()
    if not token:
        return ("not_provisioned",
                "cockpit-approver App key not provisioned yet — nothing posted.")

    status, pr_body = pr_approver._get(f"{_API}/repos/{owner}/{repo}/pulls/{number}", token)
    if status != 200:
        return ("error", f"GitHub {status}: could not fetch PR #{number}.")
    head_sha = ((pr_body.get("head") or {}).get("sha")) or ""
    if not head_sha:
        return ("error", f"PR #{number} has no head sha.")

    status, files = pr_approver._get(
        f"{_API}/repos/{owner}/{repo}/pulls/{number}/files?per_page=100", token)
    if status != 200 or not isinstance(files, list):
        return ("error", f"could not fetch PR #{number}'s changed files.")
    paths = [f.get("filename") for f in files if f.get("filename")]
    structural_paths = sorted(p for p in paths if is_structural_for_repo(p, repo))

    if not structural_paths:
        state, description = "success", "not structural"
    else:
        status, reviews = pr_approver._get(
            f"{_API}/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100", token)
        if status != 200 or not isinstance(reviews, list):
            return ("error", f"could not fetch PR #{number}'s reviews.")
        approved = any(
            (r.get("state") == "APPROVED")
            and ((r.get("user") or {}).get("login") == pr_approver.APPROVER_BOT)
            and (r.get("commit_id") == head_sha)
            for r in reviews
        )
        if approved:
            state = "success"
            description = f"approved by cockpit on {head_sha[:12]}"
        else:
            state = "pending"
            description = "needs Joris's cockpit approval"

    posted, msg = post_status(owner, repo, head_sha, state, description, token=token)
    if not posted:
        return ("error", msg)
    return (state, description)
