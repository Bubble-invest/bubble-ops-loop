#!/usr/bin/env python3
"""Open a shared-wiki proposal PR from a constrained patch and fresh clone.

On missing/inadequate credentials, the complete patch is emitted in a
needs:human card and no repository is changed. The private intents vault is
never a remote or target of this helper.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path


REPO = "vdk888/bubble-shared-wiki"
REMOTE = f"git@github.com:{REPO}.git"
ALLOWED_PREFIX = "shared/operator-intents-proposals/"
BRANCH_RE = re.compile(r"^proposal/operator-intents-[a-z0-9][a-z0-9-]{2,60}$")
PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


class ProposalError(RuntimeError):
    pass


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, timeout=120)


def validate_patch(patch: str) -> None:
    if not patch.strip() or "GIT binary patch" in patch or "Binary files" in patch:
        raise ProposalError("proposal patch is empty or binary")
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            match = re.match(r"diff --git a/(\S+) b/(\S+)$", line)
            if not match:
                raise ProposalError("malformed diff header")
            paths.update(match.groups())
        elif line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value != "/dev/null":
                paths.add(value.removeprefix("a/").removeprefix("b/"))
    if not paths or any(not path.startswith(ALLOWED_PREFIX) for path in paths):
        raise ProposalError("patch may touch only shared/operator-intents-proposals/")
    if any(".." in Path(path).parts for path in paths):
        raise ProposalError("patch contains path traversal")


def emit_needs_human(emit: Path, title: str, reason: str, patch: str) -> None:
    if not emit.is_file():
        raise ProposalError(f"cannot emit fallback card; emitter missing: {emit}")
    body = (
        f"Shared-wiki proposal PR was not created: {reason}\n\n"
        "Apply this complete constrained patch on a reviewed shared-wiki branch; "
        "Joris then decides whether to copy accepted content to the private vault.\n\n"
        f"```diff\n{patch.rstrip()}\n```"
    )
    result = run([
        str(emit),
        "task=wiki-intent-proposal-patch",
        f"title={title}",
        f"body={body}",
        "type=decision",
        "owner=rnd",
        "priority=normal",
        "budget=2",
    ])
    if result.returncode != 0:
        raise ProposalError(f"fallback card emission failed: {result.stderr.strip()}")


def viewer_permission() -> str:
    query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){viewerPermission}}"
    result = run([
        "gh", "api", "graphql", "-f", f"query={query}",
        "-F", "owner=vdk888", "-F", "name=bubble-shared-wiki",
        "--jq", ".data.repository.viewerPermission",
    ])
    if result.returncode != 0:
        raise ProposalError(f"shared-wiki permission query failed: {result.stderr.strip()}")
    return result.stdout.strip()


def propose(patch_file: Path, branch: str, title: str, body: str, emit: Path) -> int:
    patch = patch_file.read_text(encoding="utf-8")
    validate_patch(patch)
    try:
        auth = run(["gh", "auth", "status"])
        if auth.returncode != 0:
            raise ProposalError("gh authentication unavailable")
        permission = viewer_permission()
        if permission not in {"WRITE", "MAINTAIN", "ADMIN"}:
            raise ProposalError(f"shared-wiki viewerPermission is {permission or 'empty'}")
        with tempfile.TemporaryDirectory(prefix="operator-intents-proposal-") as temporary:
            checkout = Path(temporary) / "wiki"
            cloned = run(["git", "clone", "--quiet", "--branch", "main", "--single-branch", REMOTE, str(checkout)])
            if cloned.returncode != 0:
                raise ProposalError(f"fresh shared-wiki clone failed: {cloned.stderr.strip()}")
            if run(["git", "checkout", "-b", branch], cwd=checkout).returncode != 0:
                raise ProposalError("could not create named proposal branch")
            applied = run(["git", "apply", "--index", str(patch_file.resolve())], cwd=checkout)
            if applied.returncode != 0:
                raise ProposalError(f"proposal patch does not apply cleanly: {applied.stderr.strip()}")
            changed = run(["git", "diff", "--cached", "--name-only"], cwd=checkout)
            names = [line for line in changed.stdout.splitlines() if line]
            if not names or any(not name.startswith(ALLOWED_PREFIX) for name in names):
                raise ProposalError("staged change escaped proposal path")
            commit = run(["git", "commit", "-m", f"docs: {title}"], cwd=checkout)
            if commit.returncode != 0:
                raise ProposalError(f"proposal commit failed: {commit.stderr.strip()}")
            dry = run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
            if dry.returncode != 0:
                raise ProposalError(f"shared-wiki push dry-run failed: {dry.stderr.strip()}")
            pushed = run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
            if pushed.returncode != 0:
                raise ProposalError(f"shared-wiki proposal push failed: {pushed.stderr.strip()}")
            pr = run([
                "gh", "pr", "create", "--repo", REPO, "--base", "main",
                "--head", branch, "--title", title,
                "--body", f"{body.rstrip()}\n\n{PR_FOOTER}",
            ], cwd=checkout)
            if pr.returncode != 0:
                run(["git", "push", "origin", f":refs/heads/{branch}"], cwd=checkout)
                raise ProposalError(f"shared-wiki PR creation failed: {pr.stderr.strip()}")
            print(pr.stdout.strip())
            return 0
    except (FileNotFoundError, OSError, ProposalError) as exc:
        emit_needs_human(emit, title, str(exc), patch)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument(
        "--emit",
        type=Path,
        default=Path("/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh"),
    )
    args = parser.parse_args()
    if not BRANCH_RE.fullmatch(args.branch):
        parser.error("--branch must match proposal/operator-intents-<safe-slug>")
    try:
        return propose(args.patch, args.branch, args.title, args.body, args.emit)
    except (OSError, ProposalError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
