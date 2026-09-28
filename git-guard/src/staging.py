"""Detect what would actually be pushed by `git push`.

The guard needs to know the COMPLETE set of paths that will land on the
remote — both files staged in the working tree AND files in commits that
haven't been pushed yet. If we only looked at `git diff --cached`, the
attacker could `git commit` a structural file FIRST, then call the guard
with only innocuous staged files, and the structural file would slip
through on push.

Two git commands cover the picture:
  (a) `git diff --cached --name-only` — staged in the index, not yet committed
  (b) `git diff <verified-remote-base>..<source> --name-only` — committed, not yet pushed

SECURITY (card #1413, PR #543 independent-review finding, critical): (b) MUST
NOT be computed from the local `refs/remotes/<remote>/<destination>` ref.
That ref lives in the SAME working tree the guarded actor controls — it can
be forged with a single `git update-ref refs/remotes/<remote>/<destination>
<anything>` (no push, no network, no broker call), making the destination
look arbitrarily "ahead" so `staged_paths_for_push()` returns an empty or
truncated set while a forbidden path still rides along in the real push.
`README.md` used to advertise this in plain text: "The guard does not fetch;
remote-tracking refs must be maintained by the caller" — i.e. the security
boundary was resting on a value the attacker supplies.

Fix: ask the REAL remote, over the network, every time:
  1. `git ls-remote <remote> refs/heads/<destination>` — the authoritative
     answer for "what SHA does the destination branch point to on the
     remote right now". Any transport/auth error here is fail-closed (the
     caller sees a `CalledProcessError`, no path check, no push).
  2. If the remote ref exists: fetch that exact SHA into a guard-private
     ref (`refs/git-guard/base`, never `refs/remotes/*`) and re-verify the
     object that landed matches the SHA `ls-remote` reported, THEN diff
     `refs/git-guard/base..<source>`. `git diff A..B` compares trees, not
     ancestry, so this is correct even across a force-push / rewritten
     history.
  3. If the remote ref does NOT exist (genuinely new branch): inspect every
     path in the source commit's complete tree with `git ls-tree -r`. A
     history walk is insufficient because ordinary `git log --name-only`
     suppresses merge-commit diffs and can miss paths introduced only by a
     merge result.

The source ref is resolved to an immutable commit SHA once and returned with
the checked paths. The caller must push that exact SHA, never re-resolve the
symbolic source after policy evaluation.

`refs/remotes/*` and `BUBBLE_GUARD_DIFF_BASE` are never read for this
decision — see `_unpushed_commit_paths()` below.

Notion v4 §"GitHub access model" line 725: paths are enforced LOCALLY.
This module is half of that enforcement.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional


# Guard-private ref used to hold the verified REAL-remote base commit for a
# diff. Deliberately OUTSIDE the `refs/remotes/*` namespace: nothing else in
# this repo (git itself, a caller, an attacker with a shell in the same
# working tree) writes here except `_fetch_verified_base()` below, and it is
# always force-overwritten from a value re-verified against `ls-remote` on
# every call — never read as-is from a prior run.
GUARD_BASE_REF = "refs/git-guard/base"

# Git's object and diff views are security inputs for the guard.  Keep every
# invocation on one hardened command/environment path so a caller's ambient
# config cannot selectively re-enable replacement objects or rename folding.
GIT_CONFIG_OVERRIDES: tuple[str, ...] = (
    # The guarded actor controls both .git/hooks and local core.hooksPath.
    # Disable hook discovery for EVERY Git subprocess, including reads whose
    # implementation may update refs (notably fetch/reference-transaction).
    "core.hooksPath=/dev/null",
    "core.useReplaceRefs=false",
    "diff.renames=false",
    "core.quotepath=off",
)

# These variables can redirect Git away from the repository named by
# ``repo_dir`` or splice attacker-controlled indexes/object namespaces into
# it.  None may cross the guard's subprocess boundary from its caller.
GIT_REPOSITORY_ROUTING_ENV: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)

DIFF_SAFETY_ARGS: tuple[str, ...] = (
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
)


def hardened_git_command(*args: str) -> List[str]:
    """Build argv for a Git subprocess whose object view cannot be replaced."""
    cmd = ["git", "--no-replace-objects"]
    for override in GIT_CONFIG_OVERRIDES:
        cmd.extend(("-c", override))
    cmd.extend(args)
    return cmd


def hardened_git_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return a deterministic environment for security-sensitive Git reads.

    Local repository config still supplies the selected remote URL, but global,
    system, and process-injected config cannot rewrite guard behavior.  The
    command-line overrides in :func:`hardened_git_command` are defense in depth
    for replacement refs, rename detection, and path quoting.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env.pop("GIT_CONFIG_PARAMETERS", None)
    env.pop("GIT_EXTERNAL_DIFF", None)
    env.pop("GIT_DIFF_OPTS", None)
    env.pop("GIT_REPLACE_REF_BASE", None)
    for name in GIT_REPOSITORY_ROUTING_ENV:
        env.pop(name, None)

    # Do not inherit command-scope configuration injected by the caller.  The
    # push path adds its own single http.extraheader after this scrub.
    try:
        config_count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        config_count = 0
    env.pop("GIT_CONFIG_COUNT", None)
    for idx in range(max(config_count, 0)):
        env.pop(f"GIT_CONFIG_KEY_{idx}", None)
        env.pop(f"GIT_CONFIG_VALUE_{idx}", None)
    return env


def _legacy_grafts_path(repo_dir: Path, env: Mapping[str, str]) -> Optional[Path]:
    """Resolve the deprecated graft file without trusting replacement objects."""
    proc = subprocess.run(
        hardened_git_command(
            "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"
        ),
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def legacy_grafts_error(
    repo_dir: Path, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Return a fail-closed diagnostic when a legacy graft cannot be excluded."""
    git_env = hardened_git_env(env)
    grafts_path = _legacy_grafts_path(repo_dir, git_env)
    if grafts_path is None:
        return None
    try:
        if grafts_path.is_file() and grafts_path.stat().st_size > 0:
            return (
                f"legacy grafts file present at {grafts_path}; "
                "refusing to trust Git object history"
            )
    except OSError as exc:
        return f"cannot verify legacy grafts file {grafts_path}: {exc}"
    return None


@dataclass(frozen=True)
class PushPlan:
    """Immutable identity and policy input for one guarded push attempt."""

    paths: List[str]
    source_commit: str
    destination: str
    expected_remote_sha: Optional[str]

    @property
    def destination_ref(self) -> str:
        return f"refs/heads/{self.destination}"


def _run_git(repo_dir: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run a git command in `repo_dir`. Returns the CompletedProcess.

    Does NOT raise on non-zero — callers inspect returncode. We want graceful
    handling of "no upstream configured" (exit 128) for fresh repos.
    """
    env = hardened_git_env()
    cmd = hardened_git_command(*args)

    # GIT_NO_REPLACE_OBJECTS does not disable the older info/grafts mechanism.
    # Reject a non-empty graft file before trusting any Git result.  Returning
    # a normal failed CompletedProcess preserves this helper's no-raise API;
    # callers already convert non-zero results into fail-closed errors.
    grafts_error = legacy_grafts_error(repo_dir, env)
    if grafts_error is not None:
        return subprocess.CompletedProcess(cmd, 128, "", grafts_error)

    return subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _assert_inside_work_tree(repo_dir: Path) -> None:
    """Fail loud if `repo_dir` is not inside a git work tree.

    Why this exists: when `repo_dir` does NOT live inside any git repo
    (e.g. the caller passed a wrong --repo-dir, or the cwd is on a path
    where no `.git/` ancestor exists), `git diff --cached --name-only`
    silently falls back to `--no-index` mode where the `--cached` flag
    is unrecognized. The resulting error message is:

        error: unknown option `cached'
        usage: git diff --no-index [<options>] <path> <path>

    That stderr is then buried inside a CalledProcessError, masking the
    actual root cause ("you're not in a git repo"). This guard short-
    circuits with a clear message BEFORE any diff/log command runs.

    Bug discovered 2026-05-20 when the main agent ran the guard from a
    non-repo cwd to push a CLAUDE.md patch (see Step-11 robustness
    report). The original CalledProcessError did surface — but its
    message ("unknown option `cached'") is so misleading that future
    callers will waste time hunting for a code bug instead of fixing
    their cwd.
    """
    proc = _run_git(repo_dir, "rev-parse", "--is-inside-work-tree")
    # git emits "true\n" on stdout when inside, exits 128 otherwise
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        if "legacy grafts file" in proc.stderr:
            raise subprocess.CalledProcessError(
                proc.returncode or 128,
                ["git", "rev-parse", "--is-inside-work-tree"],
                proc.stdout,
                proc.stderr,
            )
        # Use CalledProcessError so existing callers that catch it keep
        # working, but craft a clear message instead of letting the
        # downstream --no-index fallback poison the diagnostics.
        clear_stderr = (
            f"not inside a git work tree: {repo_dir!s}\n"
            f"(git rev-parse --is-inside-work-tree exited {proc.returncode}, "
            f"stdout={proc.stdout.strip()!r}, "
            f"stderr={proc.stderr.strip()!r})"
        )
        raise subprocess.CalledProcessError(
            proc.returncode or 128,
            ["git", "rev-parse", "--is-inside-work-tree"],
            proc.stdout,
            clear_stderr,
        )

    # Being *somewhere* inside a work tree is insufficient: Git walks parent
    # directories, so a typo such as --repo-dir=/repo/subdir would otherwise
    # silently select /repo/.git.  Bind all subsequent reads and the final
    # push to a Git directory physically contained by the requested path.
    git_dir_proc = _run_git(repo_dir, "rev-parse", "--absolute-git-dir")
    if git_dir_proc.returncode != 0 or not git_dir_proc.stdout.strip():
        raise subprocess.CalledProcessError(
            git_dir_proc.returncode or 128,
            ["git", "rev-parse", "--absolute-git-dir"],
            git_dir_proc.stdout,
            git_dir_proc.stderr or "could not resolve the repository Git directory",
        )
    requested_root = repo_dir.resolve()
    resolved_git_dir = Path(git_dir_proc.stdout.strip()).resolve()
    if resolved_git_dir != requested_root and requested_root not in resolved_git_dir.parents:
        clear_stderr = (
            f"resolved git directory {resolved_git_dir!s} is outside requested "
            f"repo dir {requested_root!s}; refusing ancestor or redirected repository"
        )
        raise subprocess.CalledProcessError(
            128,
            ["git", "rev-parse", "--absolute-git-dir"],
            git_dir_proc.stdout,
            clear_stderr,
        )


def currently_staged(repo_dir: Path) -> List[str]:
    """Return the list of paths currently staged in the index.

    Equivalent to `git diff --cached --name-only -z`. Returns [] if nothing
    is staged. Raises CalledProcessError if `repo_dir` isn't a git repo
    (with a CLEAR error message — see `_assert_inside_work_tree` for the
    backstory on the bug this fixes).
    """
    _assert_inside_work_tree(repo_dir)
    proc = _run_git(
        repo_dir,
        "diff",
        *DIFF_SAFETY_ARGS,
        "--cached",
        "--name-only",
        "-z",
    )
    if proc.returncode != 0:
        # Not a git repo, or some other hard error
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "diff", "--cached"], proc.stdout, proc.stderr
        )
    if not proc.stdout:
        return []
    return [p for p in proc.stdout.split("\x00") if p]


def _ls_remote_sha(repo_dir: Path, remote: str, destination: str) -> Optional[str]:
    """Ask the REAL remote for the current SHA of `refs/heads/<destination>`.

    Returns the 40-hex SHA if the branch exists on the remote right now, or
    `None` only when the remote positively confirms no such ref exists
    (`git ls-remote --exit-code` reports that as exit 2 — distinct from
    every other failure). ANY other non-zero exit (auth failure, unknown
    remote, network error, host down, ...) is a hard failure: callers must
    propagate it and fail closed rather than fall back to a local,
    attacker-controlled `refs/remotes/*` value.
    """
    target_ref = f"refs/heads/{destination}"
    proc = _run_git(repo_dir, "ls-remote", "--exit-code", remote, target_ref)
    if proc.returncode == 2:
        return None
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            ["git", "ls-remote", remote, target_ref],
            proc.stdout,
            proc.stderr or (
                "ls-remote against the real remote failed; refusing to fall "
                "back to a local refs/remotes/* value for the push-base decision"
            ),
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    sha = lines[0].split("\t", 1)[0].strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise subprocess.CalledProcessError(
            1, ["git", "ls-remote", remote, target_ref], proc.stdout,
            f"ls-remote returned a malformed sha for {target_ref!r}: {sha!r}",
        )
    return sha


def _fetch_verified_base(repo_dir: Path, remote: str, destination: str, sha: str) -> str:
    """Fetch `sha` from `remote` into `GUARD_BASE_REF` and verify it landed.

    Tries fetching the exact object first (works whenever the host allows
    fetching a reachable SHA directly, e.g. GitHub). Falls back to fetching
    the branch tip by NAME for hosts that reject fetch-by-SHA — but either
    way, the object under `GUARD_BASE_REF` is re-verified to match `sha`
    (the value `_ls_remote_sha()` already got straight from the remote)
    before this function ever returns it. Nothing here is trusted just
    because a fetch subprocess exited 0.
    """
    target_ref = f"refs/heads/{destination}"
    by_sha = _run_git(repo_dir, "fetch", "--no-tags", "--force", remote, f"{sha}:{GUARD_BASE_REF}")
    if by_sha.returncode != 0:
        by_name = _run_git(repo_dir, "fetch", "--no-tags", "--force", remote, f"{target_ref}:{GUARD_BASE_REF}")
        if by_name.returncode != 0:
            raise subprocess.CalledProcessError(
                by_name.returncode,
                ["git", "fetch", remote, f"{target_ref}:{GUARD_BASE_REF}"],
                by_name.stdout,
                by_name.stderr or "could not fetch the real remote base ref (tried by-sha and by-name)",
            )
    verify = _run_git(repo_dir, "rev-parse", "--verify", "--end-of-options", f"{GUARD_BASE_REF}^{{commit}}")
    fetched_sha = verify.stdout.strip()
    if verify.returncode != 0 or fetched_sha != sha:
        raise subprocess.CalledProcessError(
            1, ["git", "fetch", remote, f"{target_ref}:{GUARD_BASE_REF}"], "",
            f"fetched object under {GUARD_BASE_REF} ({fetched_sha or 'MISSING'}) does not "
            f"match the sha ls-remote reported for {target_ref!r} ({sha}); refusing to trust it",
        )
    return GUARD_BASE_REF


def _resolve_push_target(repo_dir: Path, remote: str, ref: str) -> tuple[str, str]:
    """Resolve a supported refspec to one immutable source SHA + branch name."""
    source, separator, destination = ref.partition(":")
    if not separator:
        branch = _run_git(repo_dir, "rev-parse", "--symbolic-full-name", source)
        destination = branch.stdout.strip()
        if branch.returncode != 0 or not destination.startswith("refs/heads/"):
            raise subprocess.CalledProcessError(
                1, ["git", "push", remote, ref], stderr="push source must name a branch or specify a destination"
            )
    destination = destination.removeprefix("refs/heads/")
    # Only single branch pushes are supported; fail closed on malformed,
    # deletion, wildcard, or non-branch destinations before minting a token.
    if (not source or source.startswith(("-", "+"))
            or remote.startswith("-") or destination == "HEAD"
            or destination.startswith("refs/")
            or _run_git(repo_dir, "check-ref-format", f"refs/heads/{destination}").returncode != 0):
        raise subprocess.CalledProcessError(
            1, ["git", "push", remote, ref], stderr="unsupported push refspec or remote"
        )
    tip = _run_git(repo_dir, "rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}")
    if tip.returncode != 0:
        raise subprocess.CalledProcessError(tip.returncode, ["git", "rev-parse"], tip.stdout, tip.stderr)
    return tip.stdout.strip(), destination


def _unpushed_commit_paths(
    repo_dir: Path,
    remote: str,
    source_commit: str,
    destination: str,
    remote_sha: Optional[str],
) -> List[str]:
    """Diff an immutable push source against its destination on the REAL remote.

    SECURITY (card #1413 / PR #543 independent-review finding, critical):
    the destination's current state is NEVER read from a local
    `refs/remotes/*` ref (see module docstring for the forgery this closes).
    It is asked from the remote directly via `_ls_remote_sha()` and only
    trusted after `_fetch_verified_base()` re-confirms the fetched object
    matches — on every single call, not cached across invocations.
    """
    if remote_sha is not None:
        base = _fetch_verified_base(repo_dir, remote, destination, remote_sha)
        # `git diff A..B` compares TREES, not ancestry — this is correct even
        # when `source_commit` doesn't descend from `base` (force-push /
        # rewritten history), so no special-casing is needed for that case.
        proc = _run_git(
            repo_dir,
            "diff",
            *DIFF_SAFETY_ARGS,
            f"{base}..{source_commit}",
            "--name-only",
            "-z",
        )
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["git", "diff"], proc.stdout, proc.stderr)
        return [p for p in proc.stdout.split("\x00") if p]

    # Genuinely new branch: check the complete final tree. A history walk is
    # unsafe here: `git log --name-only` suppresses merge-commit diffs by
    # default, hiding a path introduced only in a merge result.
    proc = _run_git(repo_dir, "ls-tree", "-r", "--name-only", "-z", source_commit)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "ls-tree"], proc.stdout, proc.stderr
        )
    return [p for p in proc.stdout.split("\x00") if p]


def prepare_push(repo_dir: Path, remote: str = "origin", ref: str = "HEAD") -> PushPlan:
    """Resolve and inspect one push without leaving any symbolic ref to re-read."""
    staged = currently_staged(repo_dir)
    source_commit, destination = _resolve_push_target(repo_dir, remote, ref)
    remote_sha = _ls_remote_sha(repo_dir, remote, destination)
    unpushed = _unpushed_commit_paths(
        repo_dir, remote, source_commit, destination, remote_sha
    )
    seen: set = set()
    paths: List[str] = []
    for path in list(staged) + list(unpushed):
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return PushPlan(
        paths=paths,
        source_commit=source_commit,
        destination=destination,
        expected_remote_sha=remote_sha,
    )


def staged_paths_for_push(repo_dir: Path, remote: str = "origin", ref: str = "HEAD") -> List[str]:
    """Return the deduped union of staged-in-index + unpushed-commit paths.

    This is the SET-TO-CHECK before invoking the broker. If any element of
    this set is denied by policy, the entire push is denied (atomicity).
    """
    return prepare_push(repo_dir, remote=remote, ref=ref).paths
