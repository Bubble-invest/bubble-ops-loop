#!/usr/bin/env python3
"""Promote an approved operator-intent proposal into the private vault via a
PR — never a direct push to main, never a merge.

This is the "approved -> vault" leg of the SAME mechanism `propose_operator_
intents.py` already implements (#1320: extend it, not build a parallel
process). That script lets an agent submit CANDIDATE intent text as a
constrained patch under `shared/operator-intents-proposals/**` in the shared
wiki. This script is the next step: it takes a constrained patch that writes
under `operator-intents/**` in the private vault
(`Bubble-invest/bubble-operator-intents`) and opens a PR for it — but ONLY
once an explicit, checkable Joris approval already exists on the board card
that carries the proposal. It never authors, edits, or infers the approval
itself.

The approval gate (the fleet's existing convention, not invented here): a
comment on `Bubble-invest/bubble-ops-board#<issue>`, authored by an account in
ALLOWED_APPROVERS, matching APPROVAL_RE (the "APPROVED by Joris" convention
already used on e.g. #1326/#1327). For a card approved inline in the same
message as its capture (no separate comment thread, e.g. #1328), pass
--approved-inline and the issue BODY itself must match INLINE_APPROVAL_RE.
Without one of these, the script refuses and emits a needs:human card with
the complete patch instead of touching the vault — it never guesses.

Vault write-isolation (#1272) is preserved structurally: this script only
ever clones the vault fresh, creates a NAMED branch, applies a
scope-validated patch, pushes that branch, and opens a PR against
`Bubble-invest/bubble-operator-intents`. It contains no code path that
pushes to `main` or that merges a PR. Joris remains the vault's only
merger — this script produces something for him to review and merge, same
as any other PR.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path


VAULT_REPO = "Bubble-invest/bubble-operator-intents"
VAULT_REMOTE = f"git@github.com:{VAULT_REPO}.git"
BOARD_REPO = "Bubble-invest/bubble-ops-board"
ALLOWED_PREFIX = "operator-intents/"
BRANCH_RE = re.compile(r"^promote/[a-z0-9][a-z0-9-]{2,80}$")
PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"

# The fleet's existing "approved" convention (see #1326/#1327 board comments).
APPROVAL_RE = re.compile(r"✅?\s*APPROVED by Joris", re.IGNORECASE)
# Narrower marker for a card approved inline (no separate comment thread),
# e.g. #1328's "Captured + APPROVED intent (Joris 2026-09-14)".
INLINE_APPROVAL_RE = re.compile(r"APPROVED\s+intent\s*\(Joris", re.IGNORECASE)
# Isolation caveat inherited from the vault's own README (2026-09-13): agents
# currently share the vdk888 identity, so this allowlist is a convention gate,
# not hard identity access-control, until that identity split lands.
ALLOWED_APPROVERS = {"vdk888"}


class PromotionError(RuntimeError):
    pass


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, timeout=120)


def validate_patch(patch: str) -> None:
    if not patch.strip() or "GIT binary patch" in patch or "Binary files" in patch:
        raise PromotionError("promotion patch is empty or binary")
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            match = re.match(r"diff --git a/(\S+) b/(\S+)$", line)
            if not match:
                raise PromotionError("malformed diff header")
            paths.update(match.groups())
        elif line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value != "/dev/null":
                paths.add(value.removeprefix("a/").removeprefix("b/"))
    if not paths or any(not path.startswith(ALLOWED_PREFIX) for path in paths):
        raise PromotionError("patch may touch only operator-intents/")
    if any(".." in Path(path).parts for path in paths):
        raise PromotionError("patch contains path traversal")


def issue_json(issue: int, jq: str) -> str:
    result = run(["gh", "api", f"repos/{BOARD_REPO}/issues/{issue}", "--jq", jq])
    if result.returncode != 0:
        raise PromotionError(f"could not read board issue #{issue}: {result.stderr.strip()}")
    return result.stdout


def check_approval(issue: int, *, approved_inline: bool) -> str:
    """Return a human-readable citation of the approval evidence, or raise."""
    comments = run([
        "gh", "api", f"repos/{BOARD_REPO}/issues/{issue}/comments",
        "--jq", ".[] | [.user.login, .html_url, .body] | @tsv",
    ])
    if comments.returncode != 0:
        raise PromotionError(f"could not read comments on issue #{issue}: {comments.stderr.strip()}")
    for line in comments.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        login, url, body = parts
        if login in ALLOWED_APPROVERS and APPROVAL_RE.search(body):
            return f"comment approval by {login}: {url}"
    if approved_inline:
        body = issue_json(issue, ".body")
        if INLINE_APPROVAL_RE.search(body):
            return (
                f"inline approval in issue body: "
                f"https://github.com/{BOARD_REPO}/issues/{issue}"
            )
        raise PromotionError(
            f"--approved-inline given but issue #{issue} body does not match the inline-approval marker"
        )
    raise PromotionError(
        f"no '✅ APPROVED by Joris' comment from an allowed approver found on issue #{issue} "
        "(pass --approved-inline only if the card documents an inline approval)"
    )


def emit_needs_human(emit: Path, title: str, reason: str, patch: str) -> None:
    if not emit.is_file():
        raise PromotionError(f"cannot emit fallback card; emitter missing: {emit}")
    body = (
        f"Vault promotion PR was not opened: {reason}\n\n"
        "This is the approved-intent patch that was withheld. Once the approval evidence "
        "exists, re-run promote_operator_intents.py; it never pushes to main or merges — "
        "Joris still reviews and merges the resulting PR.\n\n"
        f"```diff\n{patch.rstrip()}\n```"
    )
    result = run([
        str(emit),
        "task=vault-intent-promotion-blocked",
        f"title={title}",
        f"body={body}",
        "type=decision",
        "owner=rnd",
        "priority=normal",
        "budget=2",
    ])
    if result.returncode != 0:
        raise PromotionError(f"fallback card emission failed: {result.stderr.strip()}")


def viewer_permission() -> str:
    query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){viewerPermission}}"
    result = run([
        "gh", "api", "graphql", "-f", f"query={query}",
        "-F", "owner=Bubble-invest", "-F", "name=bubble-operator-intents",
        "--jq", ".data.repository.viewerPermission",
    ])
    if result.returncode != 0:
        raise PromotionError(f"vault permission query failed: {result.stderr.strip()}")
    return result.stdout.strip()


def promote(
    patch_file: Path,
    branch: str,
    title: str,
    body: str,
    issue: int,
    *,
    approved_inline: bool,
    emit: Path,
) -> int:
    patch = patch_file.read_text(encoding="utf-8")
    validate_patch(patch)
    try:
        evidence = check_approval(issue, approved_inline=approved_inline)
        auth = run(["gh", "auth", "status"])
        if auth.returncode != 0:
            raise PromotionError("gh authentication unavailable")
        permission = viewer_permission()
        if permission not in {"WRITE", "MAINTAIN", "ADMIN"}:
            raise PromotionError(f"vault viewerPermission is {permission or 'empty'}")
        with tempfile.TemporaryDirectory(prefix="operator-intents-promotion-") as temporary:
            checkout = Path(temporary) / "vault"
            cloned = run(["git", "clone", "--quiet", "--branch", "main", "--single-branch", VAULT_REMOTE, str(checkout)])
            if cloned.returncode != 0:
                raise PromotionError(f"fresh vault clone failed: {cloned.stderr.strip()}")
            if run(["git", "checkout", "-b", branch], cwd=checkout).returncode != 0:
                raise PromotionError("could not create named promotion branch")
            applied = run(["git", "apply", "--index", str(patch_file.resolve())], cwd=checkout)
            if applied.returncode != 0:
                raise PromotionError(f"promotion patch does not apply cleanly: {applied.stderr.strip()}")
            changed = run(["git", "diff", "--cached", "--name-only"], cwd=checkout)
            names = [line for line in changed.stdout.splitlines() if line]
            if not names or any(not name.startswith(ALLOWED_PREFIX) for name in names):
                raise PromotionError("staged change escaped the operator-intents/ path")
            commit_message = f"promote: {title}\n\nApproval: {evidence}\nCard: https://github.com/{BOARD_REPO}/issues/{issue}"
            commit = run(["git", "commit", "-m", commit_message], cwd=checkout)
            if commit.returncode != 0:
                raise PromotionError(f"promotion commit failed: {commit.stderr.strip()}")
            # Never touch main directly: push only the named branch.
            dry = run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
            if dry.returncode != 0:
                raise PromotionError(f"vault push dry-run failed: {dry.stderr.strip()}")
            pushed = run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
            if pushed.returncode != 0:
                raise PromotionError(f"vault promotion push failed: {pushed.stderr.strip()}")
            pr_body = (
                f"{body.rstrip()}\n\n"
                f"Approval evidence: {evidence}\n"
                f"Card: https://github.com/{BOARD_REPO}/issues/{issue}\n\n"
                f"{PR_FOOTER}"
            )
            pr = run([
                "gh", "pr", "create", "--repo", VAULT_REPO, "--base", "main",
                "--head", branch, "--title", title, "--body", pr_body,
            ])
            if pr.returncode != 0:
                run(["git", "push", "origin", f":refs/heads/{branch}"], cwd=checkout)
                raise PromotionError(f"vault PR creation failed: {pr.stderr.strip()}")
            print(pr.stdout.strip())
            return 0
    except (FileNotFoundError, OSError, PromotionError) as exc:
        emit_needs_human(emit, title, str(exc), patch)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--issue", type=int, required=True, help="bubble-ops-board issue carrying the approval")
    parser.add_argument(
        "--approved-inline",
        action="store_true",
        help="the card documents approval inline in its body rather than in a separate comment",
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=Path("/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh"),
    )
    args = parser.parse_args()
    if not BRANCH_RE.fullmatch(args.branch):
        parser.error("--branch must match promote/<safe-slug>")
    try:
        return promote(
            args.patch, args.branch, args.title, args.body, args.issue,
            approved_inline=args.approved_inline, emit=args.emit,
        )
    except (OSError, PromotionError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
