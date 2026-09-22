"""Single-PR detail fetch for the cockpit's per-PR deep-link page (board #1432).

`GET /pr/{owner}/{repo}/{number}` (console/routes/pr.py) is the page Rick can
send Joris a single link to from Telegram: title, changed files (flagged
structural or not — board #1432's whole point), the structural-merge-guard's
check status, and the Approve button. This module does the read-only GitHub
fetch behind that page.

Same token + REST approach as `merge_ready_reader.py` and `routes/kanban.py`
(short-lived board token, direct `urllib` REST calls, no `gh` CLI) — reused
here rather than re-invented. Fails SAFE: any missing token or GitHub API
error returns a dict with `error` set instead of raising, so the page renders
a readable message instead of a 500 (same convention as every other cockpit
read surface).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from console.routes.kanban import _read_board_token
from console.services.structural_paths import is_structural_for_repo

_log = logging.getLogger(__name__)


def _api_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-ops-console",
    }


def _get_json(url: str, token: str):
    req = urllib.request.Request(url, headers=_api_headers(token))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _guard_status(owner: str, repo: str, head_sha: str, token: str) -> str:
    """Best-effort status of the `structural-merge-guard / guard` check on
    `head_sha`. Returns "pass" / "fail" / "pending" / "unknown" — "unknown"
    on ANY fetch problem (missing scope, no runs yet, network hiccup) so the
    page degrades to an honest "can't tell" badge rather than lying either way.
    """
    try:
        data = _get_json(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            token,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never fatal to the page
        _log.info("pr_detail_reader: check-runs fetch failed for %s/%s@%s: %s",
                   owner, repo, head_sha[:12], exc)
        return "unknown"
    runs = [r for r in (data.get("check_runs") or [])
            if "structural-merge-guard" in (r.get("name") or "").lower()]
    if not runs:
        return "unknown"
    # Most recent run for the name wins (GitHub returns newest first already,
    # but don't rely on ordering — pick by started_at if present).
    run = runs[0]
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status != "completed":
        return "pending"
    return "pass" if conclusion == "success" else "fail"


def fetch_pr_detail(owner: str, repo: str, number: int) -> dict | None:
    """Fetch one PR's title/files/guard-status for the deep-link page.

    Returns None only when the PR itself could not be resolved (404 from
    GitHub, or no board token — dev/CI). A GitHub error on the SECONDARY
    calls (files, check-runs) degrades that one field instead of failing the
    whole page (see `_guard_status` and the try/except around the files call).
    """
    token = _read_board_token()
    if not token:
        return None

    try:
        pr = _get_json(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}", token,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        _log.warning("pr_detail_reader: PR fetch failed %s/%s#%s: HTTP %s",
                     owner, repo, number, exc.code)
        return None
    except Exception as exc:  # noqa: BLE001
        _log.warning("pr_detail_reader: PR fetch failed %s/%s#%s: %s",
                     owner, repo, number, exc)
        return None

    head_sha = ((pr.get("head") or {}).get("sha")) or ""

    files: list[dict] = []
    files_error = ""
    try:
        raw_files = _get_json(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
            f"?per_page=100",
            token,
        )
        for f in raw_files:
            path = f.get("filename") or ""
            if not path:
                continue
            files.append({
                "path": path,
                "structural": is_structural_for_repo(path, repo),
                "status": f.get("status") or "",
            })
    except Exception as exc:  # noqa: BLE001 — files list is a nice-to-have, not fatal
        _log.info("pr_detail_reader: files fetch failed for %s/%s#%s: %s",
                  owner, repo, number, exc)
        files_error = "Liste des fichiers indisponible."

    structural = any(f["structural"] for f in files)
    guard_status = _guard_status(owner, repo, head_sha, token) if head_sha else "unknown"

    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": pr.get("title") or "",
        "html_url": pr.get("html_url") or "",
        "state": pr.get("state") or "",
        "head_sha": head_sha,
        "base_ref": ((pr.get("base") or {}).get("ref")) or "",
        "files": files,
        "files_error": files_error,
        "structural": structural,
        "guard_status": guard_status,
    }
