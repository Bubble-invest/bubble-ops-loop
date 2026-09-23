#!/usr/bin/env python3
"""Structural merge guard (board #1432, option C2).

Fails a PR check when the PR touches STRUCTURAL / mission-definition paths unless
it carries an APPROVING review authored by the trusted cockpit approver identity
(a GitHub App bot that agents cannot impersonate — see the identity rationale in
board #1432 and memory self-merge-guard-is-behavioral).

Why an App bot and not @vdk888: `vdk888` is the shared git identity of Rick + all
worker subagents, whose tokens carry `repo` scope (= can submit reviews). A guard
keyed on a @vdk888 approval is therefore forgeable by the very agents it constrains.
Only the cockpit holds the App's key, so an approval authored by the App bot is a
signal the agents provably cannot produce.

Reuses `is_structural_for_repo` from token-broker/src/policy.py — the SAME path set
the runtime push guard already enforces — so PR-merge protection and push protection
never drift.

Input (stdin): JSON {"files": [{"path": ...}, ...], "reviews": [{"user": {"login": ...},
"state": ..., "commit_id": ...}, ...]}  (exactly the shape of the GitHub REST
`/pulls/{n}/files` + `/pulls/{n}/reviews` responses).
Args: --repo <owner/name> --head-sha <sha> [--approver-bot <login>]

Exit 0 = allowed (no structural paths, or a valid App approval on the head SHA).
Exit 1 = blocked (structural paths touched without a valid App approval).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Import the canonical structural-path policy (single source of truth).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "token-broker", "src"))
try:
    from policy import is_structural_for_repo  # type: ignore
except Exception as exc:  # pragma: no cover - fail CLOSED if policy can't load
    print(f"::error::structural-merge-guard: cannot import policy.py ({exc}); failing closed")
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name of the PR's repo")
    ap.add_argument("--head-sha", required=True, help="PR head commit SHA")
    ap.add_argument(
        "--approver-bot",
        default=os.environ.get("APPROVER_BOT", "cockpit-approver[bot]"),
        help="login of the trusted cockpit App bot whose APPROVED review authorizes structural merges",
    )
    args = ap.parse_args()

    payload = json.load(sys.stdin)
    files = [f.get("path") or f.get("filename") for f in payload.get("files", [])]
    files = [f for f in files if f]
    reviews = payload.get("reviews", [])

    structural = sorted(p for p in files if is_structural_for_repo(p, args.repo))

    if not structural:
        print(f"::notice::structural-merge-guard: PR touches no structural paths "
              f"({len(files)} files) — allowed.")
        return 0

    print("structural-merge-guard: PR touches STRUCTURAL / mission-definition paths:")
    for p in structural:
        print(f"  - {p}")

    # A valid authorization = an APPROVED review, authored by the cockpit App bot,
    # bound to the CURRENT head SHA (so an approve-then-push-structural-change
    # cannot slip past — the stale approval no longer matches the head).
    approved = any(
        (r.get("state") == "APPROVED")
        and ((r.get("user") or {}).get("login") == args.approver_bot)
        and (r.get("commit_id") == args.head_sha)
        for r in reviews
    )

    if approved:
        print(f"::notice::structural-merge-guard: authorized by {args.approver_bot} "
              f"on head {args.head_sha[:12]} — allowed.")
        return 0

    print(f"::error::structural-merge-guard: this PR changes structural/mission paths and "
          f"requires an approval from {args.approver_bot} (posted by Joris via the cockpit) "
          f"on the current head commit. No such approval found — merge blocked. "
          f"A review by any other identity (including vdk888) does NOT authorize a structural merge.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
