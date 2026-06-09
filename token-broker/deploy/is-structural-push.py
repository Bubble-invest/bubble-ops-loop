#!/usr/bin/env python3
"""is-structural-push.py — decide whether the un-pushed delta of a git repo
touches any STRUCTURAL (mission-definition) path.

This is the structural-detection half of the box-side mission-file lock
(governance fix 2026-06-01). It is called by the root git credential helper
(`bubble-gh-credential-helper.sh`) to decide whether to mint a `contents:write`
token (normal runtime push) or a read-only token (push touches a mission file →
must go through a human-merged PR instead).

WHY a standalone script (not `bubble-token-broker check`):
  - The root credential helper runs as root with a minimal env; importing the
    broker's package (`src.policy`) is fragile under arbitrary cwd/PYTHONPATH.
  - We load `policy.py` BY FILE PATH (the same `spec_from_file_location` trick
    the git-guard's policy_loader uses) so the STRUCTURAL_PATH_GLOBS list stays
    SINGLE-SOURCED in `policy.py` and cannot drift.

WHY fail-OPEN-to-write on uncertainty (the OPPOSITE of the guard's fail-closed):
  - This script's ONLY job is to POSITIVELY detect a structural path so the
    helper can downgrade to read-only. If we cannot compute the diff (cwd is not
    a repo, git missing, fresh clone with no commits, etc.) there is, by
    definition, nothing being pushed that we can identify as structural — so we
    return "not structural" and the helper mints write exactly as it does today.
    This guarantees we NEVER break a legitimate push (the runtime loop, clone,
    fetch). The enforcement is purely: *positively-detected structural ->
    read-only*. In the real push case the diff computation always works (cwd =
    the repo being pushed), so structural pushes are always caught.

Exit codes:
  0  -> at least one structural path is in the un-pushed delta  (helper: read-only)
  1  -> no structural path detected / could not determine        (helper: write)

Usage:
  is-structural-push.py [--repo-dir DIR] [--policy-py PATH] [--verbose]
  (default --repo-dir = cwd; default --policy-py = sibling broker policy.py)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def _load_policy_module(policy_py: Path):
    spec = importlib.util.spec_from_file_location("bubble_broker_policy", str(policy_py))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build module spec for {policy_py}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bubble_broker_policy"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_git(repo_dir: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def _is_inside_work_tree(repo_dir: Path) -> bool:
    proc = _run_git(repo_dir, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _repo_name(repo_dir: Path) -> str | None:
    """Bare repo name from the `origin` remote URL (e.g. 'bubble-ops-loop').

    Returns None on any error (fail-OPEN: framework globs simply won't apply, so
    a legitimate push is never blocked by an unknown repo name). Handles both
    https (…/org/name.git) and ssh (git@host:org/name.git) remotes.
    """
    proc = _run_git(repo_dir, "config", "--get", "remote.origin.url")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    url = proc.stdout.strip()
    tail = url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    # ssh form "git@host:org/name" leaves "name" already after the split above
    return tail or None


def _unpushed_paths(repo_dir: Path) -> list[str]:
    """Union of staged-in-index + committed-but-unpushed paths.

    Mirrors git-guard/src/staging.py::staged_paths_for_push, but fail-OPEN:
    on ANY git error we return [] (no detectable structural path) so the helper
    mints write. The push case always has a valid repo cwd, so the real
    structural pushes are still caught.
    """
    paths: list[str] = []

    # (a) staged in the index (includes deletions — `git diff` reports D)
    proc = _run_git(repo_dir, "diff", "--cached", "--name-only", "-z")
    if proc.returncode == 0 and proc.stdout:
        paths += [p for p in proc.stdout.split("\x00") if p]

    # (b) committed locally but not yet on the upstream
    proc = _run_git(repo_dir, "diff", "@{upstream}..HEAD", "--name-only", "-z")
    if proc.returncode == 0:
        if proc.stdout:
            paths += [p for p in proc.stdout.split("\x00") if p]
    else:
        # No upstream configured (fresh branch). Be INCLUSIVE: every path on
        # the branch counts as un-pushed. fail-CLOSED for structural detection
        # here (we'd rather flag than miss), because a brand-new branch pushing
        # a mission file is exactly the case we must catch.
        proc = _run_git(repo_dir, "log", "--name-only", "--pretty=format:", "HEAD")
        if proc.returncode == 0 and proc.stdout:
            paths += [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]

    # dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="is-structural-push.py")
    parser.add_argument("--repo-dir", default=".", help="git work tree (default: cwd)")
    parser.add_argument(
        "--policy-py",
        default=None,
        help="path to broker policy.py (default: sibling ../src/policy.py)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()

    # Resolve policy.py: explicit flag > env > sibling layout > /opt install.
    if args.policy_py:
        policy_py = Path(args.policy_py)
    elif os.environ.get("BUBBLE_BROKER_POLICY_PY"):
        policy_py = Path(os.environ["BUBBLE_BROKER_POLICY_PY"])
    else:
        here = Path(__file__).resolve()
        candidate = here.parent.parent / "src" / "policy.py"  # deploy/.. -> src/
        opt = Path("/opt/bubble-token-broker/src/policy.py")
        policy_py = candidate if candidate.is_file() else opt

    try:
        pol = _load_policy_module(policy_py)
        # Repo-aware check: shared mission globs (every repo) + framework-source
        # globs (only when pushing the framework repo). Fall back to the
        # repo-agnostic check on older policy.py that lacks the new function.
        is_structural_for_repo = getattr(pol, "is_structural_for_repo", None)
        if is_structural_for_repo is None:
            _legacy = pol._is_structural  # noqa: SLF001 — single-sourced glob check
            is_structural_for_repo = lambda p, _r=None: _legacy(p)  # noqa: E731
    except Exception as exc:  # noqa: BLE001 — fail-OPEN to write
        if args.verbose:
            print(f"[is-structural] cannot load policy.py ({exc}) -> not-structural", file=sys.stderr)
        return 1

    if not _is_inside_work_tree(repo_dir):
        if args.verbose:
            print(f"[is-structural] {repo_dir} not a work tree -> not-structural", file=sys.stderr)
        return 1

    try:
        paths = _unpushed_paths(repo_dir)
    except Exception as exc:  # noqa: BLE001 — fail-OPEN to write
        if args.verbose:
            print(f"[is-structural] diff error ({exc}) -> not-structural", file=sys.stderr)
        return 1

    repo_name = _repo_name(repo_dir)
    structural_hits = [p for p in paths if is_structural_for_repo(p, repo_name)]
    if structural_hits:
        if args.verbose:
            print(
                f"[is-structural] STRUCTURAL paths in push (repo={repo_name}): "
                f"{structural_hits}",
                file=sys.stderr,
            )
        return 0  # structural detected -> helper mints read-only

    if args.verbose:
        print(f"[is-structural] {len(paths)} path(s), none structural -> write", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
