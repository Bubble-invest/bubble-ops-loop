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
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import gate_rbac, pr_approver, pr_detail_reader

router = APIRouter()

# Structural approval is an operator/infra authority; gate it on decide-rights for the
# rnd (infra) department, where the merge-guard + this control live.
_GUARD_DEPT = "rnd"


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
    500s) when the PR can't be resolved at all (missing board token, or the
    PR/repo genuinely doesn't exist) — the secondary fields (files, guard
    status) degrade individually instead, see pr_detail_reader.
    """
    detail = pr_detail_reader.fetch_pr_detail(owner, repo, number)
    if detail is None:
        raise HTTPException(404, f"PR not found: {owner}/{repo}#{number}")
    return request.app.state.templates.TemplateResponse(
        "pr_detail.html",
        {"request": request, "pr": detail},
    )


@router.post("/pr/{owner}/{repo}/{number}/approve")
def approve_structural_pr(owner: str, repo: str, number: int, request: Request):
    actor = _require_operator(request)
    status, message = pr_approver.submit_structural_pr_approval(
        owner=owner, repo=repo, number=number, actor=actor
    )
    if status == "approved":
        return {"ok": True, "status": status, "detail": message}
    if status == "not_provisioned":
        # 503: the feature exists but the App key isn't installed yet (pre-provisioning).
        raise HTTPException(503, message)
    raise HTTPException(502, message)
