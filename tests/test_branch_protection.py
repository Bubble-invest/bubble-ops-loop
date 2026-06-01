"""Regression tests for at-rest defense layers on vdk888/bubble-ops-fixture.

Doctrine: Notion v4 line 725 — *"branch protection"* is one of the
four-rail defense layers. The git-guard is the **runtime** enforcement;
this test covers the **at-rest** enforcement that must live on GitHub.

Constraint discovered during QA-FIXES-COMPLEMENT:
GitHub native branch protection API is **gated behind GitHub Pro for
private repos** (HTTP 403 on the free tier). The fixture repo is private
and owned by a user account with `plan: null`.

For MVP we therefore enforce the at-rest layer via:
  1. `.github/CODEOWNERS` — structural paths owned by @vdk888 (review
     required when branch protection is later enabled OR when the repo
     is upgraded to Pro / made public).
  2. `.github/workflows/path-policy.yml` — a path-policy enforcer that
     runs on every PR and fails the check when structural files are
     touched without a `settings_pr` label.

When the fixture repo is later upgraded to GitHub Pro (or replaced by an
org repo), the first test below should switch from `xfail` to a real
positive assertion against the branch-protection API.

Run:
    python3 -m pytest tests/test_branch_protection.py -v
"""

from __future__ import annotations

import json
import subprocess

import pytest

REPO = "vdk888/bubble-ops-fixture"
BRANCH = "main"


def _gh_api(path: str) -> tuple[int, str]:
    """Run `gh api <path>` and return (returncode, stdout-or-stderr).

    Stays read-only. Captures both streams so 403/404 paths are
    inspectable without raising.
    """
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.returncode, (result.stdout or result.stderr)


def _gh_contents_exists(path_in_repo: str) -> bool:
    """Return True iff the file exists at HEAD of `main` in the fixture repo."""
    rc, _ = _gh_api(f"repos/{REPO}/contents/{path_in_repo}?ref={BRANCH}")
    return rc == 0


@pytest.mark.xfail(
    reason=(
        "GitHub branch protection API requires GitHub Pro for private repos. "
        "Fixture repo (private, free tier) returns HTTP 403. "
        "Tracked: once upgraded to Pro or made public, flip xfail → real assertion."
    ),
    strict=False,
)
def test_main_has_branch_protection() -> None:
    """When the repo can use branch protection, verify enforce_admins + no force-push."""
    rc, body = _gh_api(f"repos/{REPO}/branches/{BRANCH}/protection")
    assert rc == 0, f"branch protection not retrievable: {body[:300]}"
    data = json.loads(body)
    # Minimal MVP guarantees: no force-push, no deletion of main.
    assert data.get("allow_force_pushes", {}).get("enabled") is False
    assert data.get("allow_deletions", {}).get("enabled") is False


def test_codeowners_file_present() -> None:
    """`.github/CODEOWNERS` must exist and protect structural paths.

    Notion v4 line 700: structural paths require PR. CODEOWNERS is the
    way to force a human review on those PRs (the git-guard already
    denies direct runtime pushes to those paths)."""
    assert _gh_contents_exists(
        ".github/CODEOWNERS"
    ), "CODEOWNERS missing on main — at-rest defense incomplete"

    rc, body = _gh_api(f"repos/{REPO}/contents/.github/CODEOWNERS?ref={BRANCH}")
    assert rc == 0, body[:300]
    payload = json.loads(body)
    import base64

    content = base64.b64decode(payload["content"]).decode("utf-8")

    # The 9 structural categories from Notion v4 line 700 + missions/ (Step 11
    # adds this) + .github/ self-ownership.
    required_paths = [
        "/MANDATE.md",
        "/dept.yaml",
        "/CLAUDE.md",
        "/layers/",
        "/subagents/",
        "/skills/",
        "/tools/",
        "/.claude/",
        "/missions/",
        "/.github/",
    ]
    for p in required_paths:
        assert p in content, (
            f"CODEOWNERS missing structural path {p!r} — runtime pushes "
            f"to this path are guard-blocked, but PRs need a CODEOWNER review."
        )
    # And it must designate at least one owner for those paths.
    assert "@vdk888" in content, "CODEOWNERS has no human owner assigned"


@pytest.mark.xfail(
    reason=(
        "Pushing to .github/workflows/ requires the `workflow` OAuth scope "
        "(PAT) or `workflows: write` GitHub App permission. As of "
        "2026-05-20, neither Joris's PAT (scopes: gist, read:org, repo) "
        "nor the bubble-ops-bot App installation grants `workflows`. "
        "Remediation: (a) regenerate PAT with `workflow` scope and push "
        "the workflow file manually, OR (b) grant `workflows: write` to "
        "the bubble-ops-bot App in GitHub App settings, then add "
        "`workflows:write` to the broker's `settings_pr` PERMISSION_CLASSES. "
        "Until then, CODEOWNERS is the at-rest enforcement (covered above)."
    ),
    strict=False,
)
def test_path_policy_workflow_present() -> None:
    """A GitHub Actions workflow must enforce path policy on PRs.

    This is the **path-level** complement to branch protection — without
    it, branch protection can only enforce 'no force-push' on the whole
    branch. The workflow rejects PRs that touch structural paths without
    the `settings_pr` label.

    See xfail reason for the deployment blocker."""
    assert _gh_contents_exists(
        ".github/workflows/path-policy.yml"
    ), "path-policy workflow missing — PRs to structural paths are unguarded"


def test_repo_still_private() -> None:
    """Fixture remains private (no accidental visibility change)."""
    rc, body = _gh_api(f"repos/{REPO}")
    assert rc == 0, body[:300]
    data = json.loads(body)
    assert data["private"] is True, "fixture repo went public — investigate"
