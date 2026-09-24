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


def _is_approver_review(user: dict | None, approver_bot: str, approver_bot_id: int) -> bool:
    """True when `user` (a review's `user` object, exactly as GitHub's REST
    `/pulls/{n}/reviews` returns it) identifies the trusted cockpit App bot —
    and ONLY that App, never a same-named human account.

    Mirrors `console.services.pr_approver.is_approver_review` (this script
    can't import that module — it runs standalone in the Actions runner, no
    `console` on its path). Requires ALL THREE, exact (no normalization):
    `login == approver_bot` (App-bot logins always carry the `[bot]` suffix
    on REST), `type == "Bot"`, and `id == approver_bot_id`.

    Board #1461 review follow-up (2026-09-24): an earlier revision of this
    guard normalized the `[bot]` suffix off both sides before comparing —
    which would let a plain GitHub USER account literally named
    `cockpit-approver` (no suffix; anyone can register that username)
    satisfy this gate, defeating the entire point of #1462 (only the App's
    key can ever produce this identity). No normalization here; all three
    fields must match exactly.
    """
    user = user or {}
    return (
        user.get("login") == approver_bot
        and user.get("type") == "Bot"
        and user.get("id") == approver_bot_id
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name of the PR's repo")
    ap.add_argument("--head-sha", required=True, help="PR head commit SHA")
    ap.add_argument(
        "--approver-bot",
        default=os.environ.get("APPROVER_BOT", "cockpit-approver[bot]"),
        help="login of the trusted cockpit App bot whose APPROVED review authorizes structural merges",
    )
    ap.add_argument(
        "--approver-bot-id",
        type=int,
        # The App's bot ACCOUNT id (not the App id 5019127 — a different
        # number; see pr_approver.APPROVER_BOT_ID's docstring). Confirmed
        # live via `gh api repos/Bubble-invest/bubble-ops-loop/pulls/494/
        # reviews`.
        default=int(os.environ.get("APPROVER_BOT_ID", "331993040")),
        help="numeric user id of the trusted cockpit App bot account (second, id-based identity check)",
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
        and _is_approver_review(r.get("user"), args.approver_bot, args.approver_bot_id)
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
