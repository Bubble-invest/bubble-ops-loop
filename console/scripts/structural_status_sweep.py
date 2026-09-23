#!/usr/bin/env python3
"""Periodic `structural-approval` status sweep (board #1462).

Lists every open PR on every `Bubble-invest/bubble-ops-*` repo the
cockpit-approver App is installed on, and (re-)evaluates + posts the
`structural-approval` commit status for each one's current head SHA via
`console.services.structural_status.evaluate()` — the SAME evaluation used
inline right after an operator's Approve click (`console/routes/pr.py`'s
`approve_structural_pr`).

Repo/PR listing below reuses `structural_status._paginate` (the SAME fully-
paginating helper `evaluate()` itself uses for files/reviews) rather than
keeping a second copy of the same page-loop.

This sweep is what keeps the required check honest for the two cases the
inline post doesn't cover:
  - a NON-structural PR (nobody ever clicks Approve on it, so it needs
    something to post its own `success` in the first place);
  - a structural PR approved from raw GitHub (not the cockpit) — its review
    exists but nothing has posted the App-authored status for it yet.

Run every ~2 minutes by `bubble-structural-status-sweep.timer` (a much
shorter interval than the ~45min token-refresh timer it sits beside — this
script does no secret decryption, it only reads the already-minted token and
calls the GitHub API). Needs no root: the App token is read from the same
tmpfs file `pr_approver.py` already reads
(`/run/bubble-cockpit-approver/token`, 0640 root:bubble-console) — this
script runs as the `bubble-console` user, same as the console itself (board
#1463 — NOT `claude`; the general-purpose `claude` uid must not be able to
read the approver token or post a status as this App, so a sweep running as
`claude` would defeat the uid isolation #1462/#1463 exist to build).

Fails CLOSED: no token yet -> prints one line, exits 0 (expected before the
App key is provisioned; nothing is posted for anyone, for any PR). A
per-repo or per-PR listing failure is logged and skipped — one bad repo/PR
never aborts the sweep for the rest.

Usage: python3 -m console.scripts.structural_status_sweep
       [--owner Bubble-invest] [--repo-prefix bubble-ops-]
"""
from __future__ import annotations

import argparse
import sys

from console.services import pr_approver, structural_status

_API = structural_status._API


def list_installed_repos(token: str) -> list[dict]:
    """Repos the App installation can see — exactly "repos where the App is
    installed" (board #1462 point 3), via GitHub's own installation-scoped
    listing rather than a hardcoded repo list that would silently go stale
    as new bubble-ops-* repos are spawned (dept-spawner). Any fetch problem
    returns [] (best-effort listing — a bad page here means "sweep nothing
    this tick", never a partial repo list mistaken for a complete one).
    """
    repos = structural_status._paginate(
        f"{_API}/installation/repositories", token, item_key="repositories")
    return repos if repos is not None else []


def list_open_prs(owner: str, repo: str, token: str) -> list[dict]:
    """Open PRs for one repo. Any fetch problem returns [] (best-effort — one
    repo's listing failure must not abort the whole sweep, and a partial PR
    list here isn't a security-relevant omission the way a partial FILES list
    inside `evaluate()` would be — the next tick simply re-lists)."""
    prs = structural_status._paginate(
        f"{_API}/repos/{owner}/{repo}/pulls?state=open", token)
    return prs if prs is not None else []


def sweep(owner_filter: str = "Bubble-invest", repo_prefix: str = "bubble-ops-") -> int:
    token = pr_approver._mint_token()
    if not token:
        print("structural-status-sweep: cockpit-approver App key not "
              "provisioned yet — nothing to do.")
        return 0

    n_repos = n_prs = n_success = n_pending = n_errors = 0
    for repo_obj in list_installed_repos(token):
        full_name = repo_obj.get("full_name") or ""
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)
        if owner_filter and owner.lower() != owner_filter.lower():
            continue
        if repo_prefix and not repo.lower().startswith(repo_prefix.lower()):
            continue
        n_repos += 1
        for pr in list_open_prs(owner, repo, token):
            number = pr.get("number")
            if not number:
                continue
            n_prs += 1
            state, description = structural_status.evaluate(owner, repo, number, token=token)
            print(f"structural-status-sweep: {owner}/{repo}#{number} -> {state} ({description})")
            if state == "success":
                n_success += 1
            elif state == "pending":
                n_pending += 1
            else:
                n_errors += 1

    print(f"structural-status-sweep: done — {n_repos} repo(s), {n_prs} open PR(s): "
          f"{n_success} success, {n_pending} pending, {n_errors} error(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="Bubble-invest",
                     help="restrict to this GitHub org (default: Bubble-invest)")
    ap.add_argument("--repo-prefix", default="bubble-ops-",
                     help="restrict to repos whose name starts with this prefix")
    args = ap.parse_args(argv)
    return sweep(args.owner, args.repo_prefix)


if __name__ == "__main__":
    sys.exit(main())
