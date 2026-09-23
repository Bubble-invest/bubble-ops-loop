"""Structural-PR approval + deep-link view (board #1432, option C2).

GET  /pr/{owner}/{repo}/{number}          — deep-link page: title, changed
    files (flagged structural or not), the structural-merge-guard's check
    status, and the Approve button. This is the single link Rick can send
    Joris (Telegram) per PR instead of walking him through the cockpit.
POST /pr/{owner}/{repo}/{number}/approve  — the authenticated operator approves a
structural/mission-path PR; the cockpit then submits an APPROVED review AS the
`cockpit-approver` App (a signal the fleet's vdk888 agents cannot forge), which the
`structural-merge-guard` workflow requires before such a PR can merge.

Auth: both routes sit behind the same global cockpit auth middleware as every
other route (session cookie / bearer). The mutating POST additionally requires
the same principal + RBAC check as gate decisions (only a real cockpit
operator, never an agent token, may submit an approval) — the GET view is
read-only and open to any authenticated viewer, mirroring gate_card.html
(the gate detail page has no RBAC check either; only its POST /decide does).

`head_sha` (board #1432 review, blocking finding): the POST REQUIRES the exact
commit SHA the operator saw on the GET page. Without this, a PR's own author
(an explicitly adversarial fleet agent in this design) could push a new commit
between "Joris reads the diff" and "Joris clicks Approve", and the click would
silently authorize a diff he never reviewed. `pr_approver.py` re-verifies this
against the PR's live head before posting anything.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from console.services import gate_rbac, pr_approver, pr_detail_reader, structural_status

router = APIRouter()

# Structural approval is an operator/infra authority; gate it on decide-rights for the
# rnd (infra) department, where the merge-guard + this control live.
_GUARD_DEPT = "rnd"

# A full, lower/upper-case-tolerant git commit SHA — nothing else is accepted
# as `head_sha` (board #1432 review: reject missing/malformed input at the
# route boundary with a clear 400, before ever touching GitHub).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Scope both routes to the repos the structural-merge-guard workflow is
# deployed on today (bubble-ops-loop) and is named to roll out to fleet-wide
# (board #1432 thread: "repeat 3-5 across the other bubble-ops-* repos") —
# the Bubble-invest/bubble-ops-* convention used throughout this codebase
# (see e.g. console/routes/kanban.py's `_repo_for_dept`). NOT a security
# boundary by itself (the App-installation-scoped token can't produce a
# valid review on a repo it isn't installed on regardless) — this closes the
# repo-existence probe the GET route's 404-vs-degraded-fields behavior would
# otherwise allow against arbitrary owner/repo strings (board #1432 review).
_ALLOWED_OWNER = "bubble-invest"
_ALLOWED_REPO_RE = re.compile(r"^bubble-ops-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)


def _is_allowed_repo(owner: str, repo: str) -> bool:
    return (owner or "").strip().lower() == _ALLOWED_OWNER and bool(
        _ALLOWED_REPO_RE.match((repo or "").strip())
    )


def _require_operator(request: Request) -> str:
    actor = getattr(request.state, "user", None)
    if not gate_rbac.may_decide(actor, _GUARD_DEPT):
        # Deliberately generic — do not let this enumerate repos/PRs.
        raise HTTPException(403, "Not authorized to approve structural PRs")
    return actor


@router.get("/pr/{owner}/{repo}/{number}", response_class=HTMLResponse)
def pr_detail(owner: str, repo: str, number: int, request: Request):
    """Deep-link PR page — one URL Rick can send Joris per PR (Telegram-friendly).

    Read-only: no RBAC check here (same convention as gate_card.html), only
    the POST /approve below enforces gate_rbac.may_decide. 404s (rather than
    500s) when the PR can't be resolved at all (missing board token, the
    owner/repo isn't on the allowlist, or the PR/repo genuinely doesn't
    exist — one opaque outcome for all three, no existence oracle) — the
    secondary fields (files, guard status) degrade individually instead, see
    pr_detail_reader.
    """
    if not _is_allowed_repo(owner, repo):
        raise HTTPException(404, f"PR not found: {owner}/{repo}#{number}")
    detail = pr_detail_reader.fetch_pr_detail(owner, repo, number)
    if detail is None:
        raise HTTPException(404, f"PR not found: {owner}/{repo}#{number}")
    return request.app.state.templates.TemplateResponse(
        "pr_detail.html",
        {"request": request, "pr": detail},
    )


@router.post("/pr/{owner}/{repo}/{number}/approve")
def approve_structural_pr(
    owner: str, repo: str, number: int, request: Request,
    head_sha: str | None = Query(None),
):
    actor = _require_operator(request)
    if not _is_allowed_repo(owner, repo):
        raise HTTPException(404, f"PR not found: {owner}/{repo}#{number}")
    if not head_sha or not _SHA_RE.match(head_sha):
        raise HTTPException(
            400,
            "head_sha must be the 40-character commit SHA shown on the PR page "
            "(reload the page and try again).",
        )
    status, message = pr_approver.submit_structural_pr_approval(
        owner=owner, repo=repo, number=number, actor=actor, head_sha=head_sha,
    )
    if status == "approved":
        # Board #1462: immediately reflect the approval as the unforgeable
        # `structural-approval` commit status too, so the PR's REQUIRED check
        # flips right away instead of waiting for the periodic sweep's next
        # ~2min tick (console/scripts/structural_status_sweep.py). Best-effort:
        # a failure here never turns a successful approval into an error
        # response — the sweep will re-post the same status idempotently on
        # its next tick regardless.
        structural_status.post_status(
            owner, repo, head_sha, "success",
            f"approved by cockpit on {head_sha[:12]}",
        )
        return {"ok": True, "status": status, "detail": message}
    if status == "not_provisioned":
        # 503: the feature exists but the App key isn't installed yet (pre-provisioning).
        raise HTTPException(503, message)
    if status == "stale":
        # 409: the request conflicts with the PR's current state (it moved).
        raise HTTPException(409, message)
    raise HTTPException(502, message)
