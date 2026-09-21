"""App-signed structural-PR approval (board #1432, option C2).

The cockpit approves a structural/mission-path PR AS the `cockpit-approver` GitHub
App. The guard workflow (`structural-merge-guard`) then accepts it, because only the
cockpit holds the App's key — an identity the fleet's `vdk888` agents cannot forge.

Flow: mint a short-lived `pull_requests:write` installation token via the root-owned
minter, then POST an `APPROVED` review to the PR as the App.

Fails SAFE: if the minter/key is not provisioned (before Joris drops the .pem), this
returns ("not_provisioned", …) and submits NOTHING — never a partial or false approval.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

# Installed root-owned minter; invoked via a scoped `sudo -n` (mirrors contents-token).
MINTER = os.environ.get(
    "COCKPIT_APPROVER_MINTER",
    "sudo -n /usr/local/bin/bubble-cockpit-approver-token.sh",
)
APPROVER_BOT = os.environ.get("APPROVER_BOT", "cockpit-approver[bot]")
_API = "https://api.github.com"


def _mint_token() -> str | None:
    """Return a ghs_ token, or None if the App key isn't provisioned / minting failed."""
    try:
        out = subprocess.run(
            MINTER.split(), capture_output=True, text=True, timeout=30
        )
    except Exception:
        return None
    tok = (out.stdout or "").strip()
    return tok if tok.startswith("ghs_") else None


def _post(url: str, token: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "bubble-cockpit-approver",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body


def submit_structural_pr_approval(
    owner: str, repo: str, number: int, actor: str, note: str = ""
) -> tuple[str, str]:
    """Approve PR {owner}/{repo}#{number} as the cockpit-approver App.

    Returns (status, message):
      "approved"        — an APPROVED review was posted by the App.
      "not_provisioned" — the App key/minter isn't set up yet; nothing submitted.
      "error"           — the review POST failed (message carries the reason).
    """
    token = _mint_token()
    if not token:
        return ("not_provisioned",
                "cockpit-approver App key not provisioned yet — no approval submitted.")
    body = (f"Approved by {actor} via the Bubble cockpit (structural merge guard, #1432)."
            + (f"\n\n{note}" if note else ""))
    status, resp = _post(
        f"{_API}/repos/{owner}/{repo}/pulls/{number}/reviews",
        token,
        {"event": "APPROVE", "body": body},
    )
    if status in (200, 201):
        return ("approved",
                f"Approved {owner}/{repo}#{number} as {APPROVER_BOT} "
                f"(review {resp.get('id','?')}).")
    return ("error",
            f"GitHub {status}: {resp.get('message', 'review submission failed')}")
