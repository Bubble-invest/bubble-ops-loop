"""App-signed structural-PR approval (board #1432, option C2).

The cockpit approves a structural/mission-path PR AS the `cockpit-approver` GitHub
App. The guard workflow (`structural-merge-guard`) then accepts it, because only the
cockpit holds the App's key — an identity the fleet's `vdk888` agents cannot forge.

Flow: READ the short-lived `pull_requests:write` installation token a root-owned
systemd TIMER mints into a tmpfs file (never a request-time `sudo`), then POST an
`APPROVED` review to the PR as the App.

Why a file, not `sudo -n` at request time: the console unit runs as `claude` with
`NoNewPrivileges=true` (console/deploy/bubble-ops-console.service.template) — under
which `sudo` (setuid-root) cannot escalate, so a request-time `sudo -n` call would
simply fail in production. This is the EXACT problem the contents-token minter
already solved (console/deploy/contents-token/): a root systemd timer mints on its
own schedule and writes a tmpfs file the console only reads. This module mirrors
that shape 1:1 — see console/deploy/cockpit-approver/bubble-cockpit-approver-token-
refresh.{sh,service,timer} for the timer side, and
console/services/github_reader.py::_read_contents_token() for the sibling reader.

Fails SAFE: if the key is not provisioned (before Joris drops the .pem) or the
refresh timer hasn't minted yet, the token file is absent/empty and this returns
("not_provisioned", …) and submits NOTHING — never a partial or false approval.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Root-owned timer writes here (tmpfs, 0640 root:claude — group-readable by the
# console's `claude` user, never sudo). Mirrors github_reader._CONTENTS_TOKEN_FILE.
_APPROVER_TOKEN_FILE = os.environ.get(
    "COCKPIT_APPROVER_TOKEN_FILE", "/run/bubble-cockpit-approver/token",
)
APPROVER_BOT = os.environ.get("APPROVER_BOT", "cockpit-approver[bot]")
_API = "https://api.github.com"


def _mint_token() -> str | None:
    """Return the current ghs_ token, or None if it isn't provisioned yet.

    Named `_mint_token` (not `_read_token`) to keep the public shape this
    module has always exposed to its caller (`submit_structural_pr_approval`)
    and to existing tests — but it now READS the timer-refreshed file instead
    of invoking the minter itself (see the module docstring). Falls back to
    GH_TOKEN/GITHUB_TOKEN env for dev/CI, mirroring
    `github_reader._read_contents_token()` exactly.
    """
    try:
        tok = open(_APPROVER_TOKEN_FILE).read().strip()
        if tok:
            # Defense in depth: a truncated/corrupt file write should read as
            # "not provisioned", never be forwarded as a bearer credential.
            return tok if tok.startswith("ghs_") else None
    except OSError:
        pass
    env_tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    return env_tok or None


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
