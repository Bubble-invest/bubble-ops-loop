"""
test_systemd_path_matches_deploy.py — Sprint H+I Fix 1.

The systemd unit template declares a WorkingDirectory= path that MUST
match the path where scripts/deploy-to-morty.sh clones the dept repo
on Morty. Otherwise the service would start in a directory that has
no repo and crash-loop.

The canonical convention is /home/claude/agents/<slug> (matches the
existing Morty fixture, docs/ARCHITECTURE.md §1, docs/OPERATOR-GUIDE.md,
and the Phase-G smoke test).
"""
from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy-to-morty.sh"

CANONICAL_REPO_PATH_PATTERN = "/home/claude/agents/${DEPT_SLUG}"


def _extract_working_directory(template_text: str) -> str:
    for line in template_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WorkingDirectory="):
            return stripped.split("=", 1)[1].strip()
    raise AssertionError("template has no WorkingDirectory= directive")


def _extract_remote_repo_path(deploy_text: str) -> str:
    # Look for REMOTE_REPO_PATH="..." assignment.
    match = re.search(r'REMOTE_REPO_PATH="([^"]+)"', deploy_text)
    if not match:
        raise AssertionError("scripts/deploy-to-morty.sh has no REMOTE_REPO_PATH= assignment")
    return match.group(1)


def test_systemd_working_directory_matches_deploy_repo_path():
    """The systemd WorkingDirectory must equal the path where deploy
    clones the dept repo. The placeholder names differ slightly
    (${DEPT_SLUG} vs ${SLUG}); we normalize before comparing."""
    template_text = TEMPLATE.read_text(encoding="utf-8")
    deploy_text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    working_dir = _extract_working_directory(template_text)
    repo_path = _extract_remote_repo_path(deploy_text)

    # Normalize placeholder names so we compare the path shape only.
    norm_working = working_dir.replace("${DEPT_SLUG}", "<slug>").replace("${SLUG}", "<slug>")
    norm_repo = repo_path.replace("${DEPT_SLUG}", "<slug>").replace("${SLUG}", "<slug>")

    assert norm_working == norm_repo, (
        f"Path mismatch between systemd template and deploy script:\n"
        f"  WorkingDirectory= {working_dir}\n"
        f"  REMOTE_REPO_PATH= {repo_path}\n"
        f"The service would start in a dir that has no cloned repo."
    )


def test_systemd_working_directory_uses_canonical_convention():
    """Anchor the convention so any future drift fails loudly. Canonical
    path = /home/claude/agents/<slug> (Morty fixture + docs)."""
    template_text = TEMPLATE.read_text(encoding="utf-8")
    working_dir = _extract_working_directory(template_text)
    assert working_dir == CANONICAL_REPO_PATH_PATTERN, (
        f"WorkingDirectory drifted from canonical convention:\n"
        f"  expected: {CANONICAL_REPO_PATH_PATTERN}\n"
        f"  got:      {working_dir}"
    )


def test_deploy_repo_path_uses_canonical_convention():
    deploy_text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    repo_path = _extract_remote_repo_path(deploy_text)
    # The deploy script uses ${SLUG} not ${DEPT_SLUG}.
    canonical = CANONICAL_REPO_PATH_PATTERN.replace("${DEPT_SLUG}", "${SLUG}")
    assert repo_path == canonical, (
        f"REMOTE_REPO_PATH drifted from canonical convention:\n"
        f"  expected: {canonical}\n"
        f"  got:      {repo_path}"
    )
