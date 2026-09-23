"""App-signed structural-PR approval (board #1432, option C2).

The cockpit approves a structural/mission-path PR AS the `cockpit-approver` GitHub
App. The guard workflow (`structural-merge-guard`) then accepts it, because only the
cockpit holds the App's key — an identity the fleet's `vdk888` agents cannot forge.

Flow: READ the short-lived `pull_requests:write` installation token a root-owned
systemd TIMER mints into a tmpfs file (never a request-time `sudo`), re-verify the
PR's CURRENT head still matches the head the operator reviewed, then POST an
`APPROVED` review — pinned to that exact commit — to the PR as the App.

Why a file, not `sudo -n` at request time: the console unit runs as `claude` with
`NoNewPrivileges=true` (console/deploy/bubble-ops-console.service.template) — under
which `sudo` (setuid-root) cannot escalate, so a request-time `sudo -n` call would
simply fail in production. This is the EXACT problem the contents-token minter
already solved (console/deploy/contents-token/): a root systemd timer mints on its
own schedule and writes a tmpfs file the console only reads. This module mirrors
that shape 1:1 — see console/deploy/cockpit-approver/bubble-cockpit-approver-token-
refresh.{sh,service,timer} for the timer side, and
console/services/github_reader.py::_read_contents_token() for the sibling reader.

Fails SAFE:
  - key not provisioned (before Joris drops the .pem) or the refresh timer hasn't
    minted yet -> token file absent/empty/corrupt -> ("not_provisioned", …), NOTHING
    submitted. No env-var fallback here (see `_mint_token`'s docstring for why this
    differs from `_read_contents_token()`'s dev/CI fallback).
  - the PR's head moved since the operator loaded the deep-link page (board #1432
    review: an adversarial fleet agent could push a new commit between "Joris reads
    the diff" and "Joris clicks Approve") -> ("stale", …), NOTHING submitted.
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
# Matches structural_merge_guard.py's own default literal (`--approver-bot`'s
# default) — this only affects the human-readable success message below, but a
# reviewer flagged the mismatch as worth aligning (board #1432 review).
APPROVER_BOT = os.environ.get("APPROVER_BOT", "cockpit-approver[bot]")
_API = "https://api.github.com"


def _mint_token() -> str | None:
    """Return the current ghs_ token, or None if it isn't provisioned yet.

    Named `_mint_token` (not `_read_token`) to keep the public shape this
    module has always exposed to its caller (`submit_structural_pr_approval`)
    — but it now READS the timer-refreshed file instead of invoking the
    minter itself (see the module docstring).

    DELIBERATELY NO env-var fallback (board #1432 review, blocking finding):
    an earlier revision fell back to `GH_TOKEN`/`GITHUB_TOKEN`, mirroring
    `github_reader._read_contents_token()`. But unlike that READ-only path,
    both vars are confirmed set in PRODUCTION on the console unit itself
    (`console/deploy/bubble-ops-console.service.template`'s `ExecStartPre`
    derives `GH_TOKEN` from `GITHUB_TOKEN` unconditionally at every start) —
    so any time the token file is merely missing/stale (first ~30s after a
    reboot, a transient mint failure, a tmpfs hiccup), the old fallback would
    silently post an APPROVED review as whatever identity `GH_TOKEN` is —
    NOT the cockpit-approver App — exactly the impersonation this feature
    exists to prevent. A missing/stale/corrupt token file must mean
    "not_provisioned", full stop; never a silent identity swap.
    """
    try:
        tok = open(_APPROVER_TOKEN_FILE).read().strip()
        if tok:
            # Defense in depth: a truncated/corrupt file write should read as
            # "not provisioned", never be forwarded as a bearer credential.
            return tok if tok.startswith("ghs_") else None
    except OSError:
        pass
    return None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-cockpit-approver",
    }


def _request(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    headers = _headers(token)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body


def _get(url: str, token: str) -> tuple[int, dict]:
    return _request("GET", url, token)


def _post(url: str, token: str, payload: dict) -> tuple[int, dict]:
    return _request("POST", url, token, payload)


def submit_structural_pr_approval(
    owner: str, repo: str, number: int, actor: str, head_sha: str, note: str = ""
) -> tuple[str, str]:
    """Approve PR {owner}/{repo}#{number} as the cockpit-approver App — pinned to
    `head_sha` (the commit the operator actually reviewed on the deep-link page).

    Returns (status, message):
      "approved"        — an APPROVED review was posted by the App, pinned to
                           head_sha via the review's commit_id.
      "not_provisioned" — the App key/timer hasn't minted a token yet; nothing
                           submitted.
      "stale"           — the PR's CURRENT head no longer matches head_sha (it
                           changed since the operator loaded the page); nothing
                           submitted — the caller must reload and re-review.
      "error"           — the head-verification GET or the review POST failed
                           (message carries the reason).
    """
    token = _mint_token()
    if not token:
        return ("not_provisioned",
                "cockpit-approver App key not provisioned yet — no approval submitted.")

    # TOCTOU guard (board #1432 review, blocking finding): re-check the PR's
    # CURRENT head immediately before approving, rather than trusting the
    # `head_sha` the caller sent. Without this, an operator who read the diff
    # at head_sha on the deep-link page, then clicked Approve after the PR's
    # own author (an explicitly adversarial fleet agent in this design)
    # pushed a new commit, would unknowingly authorize a diff they never saw
    # — GitHub would otherwise default a commit_id-less review to whatever is
    # HEAD at POST time.
    status, pr_body = _get(f"{_API}/repos/{owner}/{repo}/pulls/{number}", token)
    if status != 200:
        return ("error",
                f"GitHub {status}: could not verify the PR's current head before "
                f"approving — nothing submitted.")
    current_sha = ((pr_body.get("head") or {}).get("sha")) or ""
    if not current_sha or current_sha.lower() != (head_sha or "").lower():
        return ("stale",
                "PR changed since you loaded this page — reload and re-review "
                "before approving.")

    body = (f"Approved by {actor} via the Bubble cockpit (structural merge guard, #1432)."
            + (f"\n\n{note}" if note else ""))
    status, resp = _post(
        f"{_API}/repos/{owner}/{repo}/pulls/{number}/reviews",
        token,
        {"event": "APPROVE", "body": body, "commit_id": head_sha},
    )
    if status in (200, 201):
        return ("approved",
                f"Approved {owner}/{repo}#{number} as {APPROVER_BOT} "
                f"(review {resp.get('id','?')}, pinned to {head_sha[:12]}).")
    return ("error",
            f"GitHub {status}: {resp.get('message', 'review submission failed')}")
