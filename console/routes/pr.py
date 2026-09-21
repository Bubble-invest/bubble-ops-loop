"""Structural-PR approval endpoint (board #1432, option C2).

POST /pr/{owner}/{repo}/{number}/approve — the authenticated operator approves a
structural/mission-path PR; the cockpit then submits an APPROVED review AS the
`cockpit-approver` App (a signal the fleet's vdk888 agents cannot forge), which the
`structural-merge-guard` workflow requires before such a PR can merge.

Auth: same principal + RBAC check as gate decisions (only a real cockpit operator,
never an agent token, reaches this route).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from console.services import gate_rbac, pr_approver

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
