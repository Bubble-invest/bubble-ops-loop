"""Build and inspect a push in a guard-owned Git repository.

The actor owns its checkout, including ``.git/config``, hooks, refs, index,
attributes, and environment. None of those are an authorization boundary.
The guard performs exactly one kind of Git read in that checkout: it resolves
the requested source to an immutable commit SHA. It then imports that exact
object into a fresh, mode-0700 temporary bare repository and does every remote
lookup, fetch, tree diff, policy-relevant path enumeration, and push there.

The temporary repository has a guard-written config, a private HOME, no hooks,
no global/system config, no replacement objects, and no inherited Git routing
variables. The destination is a literal policy-derived URL supplied by the
caller; actor ``remote.*``, ``url.*.insteadOf``, proxy, include, credential,
receive-pack, and hook settings are never consulted.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple


GIT_BINARY = "/usr/bin/git"
SOURCE_REF = "refs/git-guard/source"
BASE_REF = "refs/git-guard/base"
# Branch a NEW push target is compared against (it forks from it). Without this,
# a push that creates a branch diffed against nothing and every file in the repo
# counted as changed (Ben, settings_pr, 2026-09-29).
DEFAULT_BASE_BRANCH = "main"

DIFF_SAFETY_ARGS: Tuple[str, ...] = (
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
)

GIT_CONFIG_OVERRIDES: Tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.useReplaceRefs=false",
    "diff.renames=false",
    "core.quotepath=off",
)

# libcurl honors these even when Git config is pristine. An actor-controlled
# proxy environment must not observe an Authorization header.
PROXY_ENV = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)


def hardened_git_command(*args: str) -> List[str]:
    """Return argv rooted at the trusted system Git binary."""
    cmd = [GIT_BINARY, "--no-replace-objects"]
    for override in GIT_CONFIG_OVERRIDES:
        cmd.extend(("-c", override))
    cmd.extend(("-c", "credential.helper="))
    cmd.extend(("-c", "http.proxy="))
    cmd.extend(("-c", "http.sslVerify=true"))
    cmd.extend(args)
    return cmd


def hardened_git_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    home: Optional[Path] = None,
) -> Dict[str, str]:
    """Return an environment that cannot route or reconfigure Git."""
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith("GIT_") and key not in PROXY_ENV
    }
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env["HOME"] = str(home) if home is not None else os.devnull
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    return env


def authenticated_git_env(base: Mapping[str, str], token: str) -> Dict[str, str]:
    """Add one process-private GitHub Basic header without putting it in argv."""
    env = hardened_git_env(base, home=Path(base["HOME"]))
    basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


def _called_process_error(
    proc: "subprocess.CompletedProcess[str]", command: List[str], fallback: str
) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        proc.returncode or 1,
        command,
        proc.stdout,
        proc.stderr or fallback,
    )


def _run(
    cwd: Path,
    env: Mapping[str, str],
    *args: str,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        hardened_git_command(*args),
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def _validate_actor_repo(repo_dir: Path, env: Mapping[str, str]) -> Path:
    """Resolve and bind the one actor-owned repository we may read."""
    try:
        requested = repo_dir.resolve(strict=True)
    except OSError as exc:
        raise subprocess.CalledProcessError(
            128, [GIT_BINARY, "rev-parse"], stderr=f"invalid actor repository: {exc}"
        )
    inside = _run(requested, env, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise _called_process_error(
            inside,
            [GIT_BINARY, "rev-parse", "--is-inside-work-tree"],
            f"not inside a git work tree: {requested}",
        )
    git_dir = _run(requested, env, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0 or not git_dir.stdout.strip():
        raise _called_process_error(
            git_dir,
            [GIT_BINARY, "rev-parse", "--absolute-git-dir"],
            "could not resolve actor Git directory",
        )
    resolved_git_dir = Path(git_dir.stdout.strip()).resolve()
    if resolved_git_dir != requested and requested not in resolved_git_dir.parents:
        raise subprocess.CalledProcessError(
            128,
            [GIT_BINARY, "rev-parse", "--absolute-git-dir"],
            git_dir.stdout,
            (
                f"resolved git directory {resolved_git_dir} is outside requested "
                f"repo dir {requested}; refusing ancestor or redirected repository"
            ),
        )
    grafts = resolved_git_dir / "info" / "grafts"
    try:
        if grafts.is_file() and grafts.stat().st_size:
            raise subprocess.CalledProcessError(
                128,
                [GIT_BINARY, "rev-parse"],
                stderr=f"legacy grafts file present at {grafts}; refusing source import",
            )
    except OSError as exc:
        raise subprocess.CalledProcessError(
            128,
            [GIT_BINARY, "rev-parse"],
            stderr=f"cannot verify legacy grafts file {grafts}: {exc}",
        )
    return requested


def resolve_source_commit(
    repo_dir: Path,
    ref: str,
    env: Mapping[str, str],
) -> Tuple[Path, str, str]:
    """Resolve the actor source once to ``(repo, sha, destination)``."""
    requested = _validate_actor_repo(repo_dir, env)
    source, separator, destination = ref.partition(":")
    if not source or source.startswith(("-", "+")):
        raise subprocess.CalledProcessError(
            1, [GIT_BINARY, "rev-parse"], stderr="unsupported push refspec"
        )

    # Resolve the source exactly once. Every later command names this SHA.
    tip = _run(
        requested,
        env,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{source}^{{commit}}",
    )
    if tip.returncode != 0:
        raise _called_process_error(
            tip, [GIT_BINARY, "rev-parse", source], "could not resolve push source"
        )
    sha = tip.stdout.strip().lower()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise subprocess.CalledProcessError(
            1, [GIT_BINARY, "rev-parse", source], tip.stdout,
            f"source did not resolve to a full commit SHA: {sha!r}",
        )

    if not separator:
        branch = _run(requested, env, "rev-parse", "--symbolic-full-name", source)
        destination_ref = branch.stdout.strip()
        if branch.returncode != 0 or not destination_ref.startswith("refs/heads/"):
            raise subprocess.CalledProcessError(
                1,
                [GIT_BINARY, "rev-parse", "--symbolic-full-name", source],
                branch.stdout,
                "push source must name a branch or specify a destination",
            )
        destination = destination_ref[len("refs/heads/"):]
    elif destination.startswith("refs/heads/"):
        destination = destination[len("refs/heads/"):]

    if not destination or destination == "HEAD" or destination.startswith("refs/"):
        raise subprocess.CalledProcessError(
            1, [GIT_BINARY, "check-ref-format"], stderr="unsupported push destination"
        )
    check = _run(requested, env, "check-ref-format", f"refs/heads/{destination}")
    if check.returncode != 0:
        raise _called_process_error(
            check, [GIT_BINARY, "check-ref-format"], "invalid push destination"
        )
    return requested, sha, destination


@dataclass(frozen=True)
class PushPlan:
    paths: List[str]
    source_commit: str
    destination: str
    expected_remote_sha: Optional[str]
    destination_url: str
    guard_repo: Path
    git_env: Mapping[str, str]

    @property
    def destination_ref(self) -> str:
        return f"refs/heads/{self.destination}"


class GuardRepository:
    """A short-lived bare repository owned exclusively by the guard."""

    def __init__(self, root: Path, repo: Path, env: Dict[str, str]) -> None:
        self.root = root
        self.repo = repo
        self.env = env

    @classmethod
    def create(cls) -> "GuardRepository":
        root = Path(tempfile.mkdtemp(prefix="bubble-git-guard-"))
        root.chmod(stat.S_IRWXU)
        home = root / "home"
        home.mkdir(mode=0o700)
        repo = root / "repo.git"
        env = hardened_git_env(home=home)
        init = subprocess.run(
            hardened_git_command("init", "--bare", "--template=", str(repo)),
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            raise _called_process_error(
                init, [GIT_BINARY, "init", "--bare"], "could not create guard repository"
            )
        (repo / "config").write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tfilemode = true\n"
            "\tbare = true\n"
            "\thooksPath = /dev/null\n"
            "\tuseReplaceRefs = false\n"
            "[diff]\n"
            "\trenames = false\n"
            "[http]\n"
            "\tproxy =\n"
            "\tsslVerify = true\n",
            encoding="utf-8",
        )
        return cls(root, repo, env)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run(
        self,
        *args: str,
        token: Optional[str] = None,
    ) -> "subprocess.CompletedProcess[str]":
        env = authenticated_git_env(self.env, token) if token else dict(self.env)
        proc = _run(self.repo, env, *args)
        if token:
            basic = base64.b64encode(
                f"x-access-token:{token}".encode("ascii")
            ).decode("ascii")
            proc.stderr = proc.stderr.replace(token, "<TOKEN-REDACTED>")
            proc.stderr = proc.stderr.replace(basic, "<TOKEN-B64-REDACTED>")
        return proc

    def import_source(self, actor_repo: Path, sha: str) -> None:
        proc = self.run(
            "-c", "protocol.file.allow=always",
            "fetch", "--no-tags", "--force",
            str(actor_repo),
            f"{sha}:{SOURCE_REF}",
        )
        if proc.returncode != 0:
            raise _called_process_error(
                proc,
                [GIT_BINARY, "fetch", "<actor-repo>", sha],
                "could not import resolved actor commit",
            )
        verify = self.run(
            "rev-parse", "--verify", "--end-of-options", f"{SOURCE_REF}^{{commit}}"
        )
        if verify.returncode != 0 or verify.stdout.strip().lower() != sha:
            raise subprocess.CalledProcessError(
                1,
                [GIT_BINARY, "fetch", "<actor-repo>", sha],
                verify.stdout,
                (
                    f"imported source object ({verify.stdout.strip() or 'MISSING'}) "
                    f"does not match resolved SHA {sha}"
                ),
            )

    def remote_sha(
        self,
        destination_url: str,
        destination: str,
        *,
        token: Optional[str] = None,
    ) -> Optional[str]:
        target = f"refs/heads/{destination}"
        proc = self.run("ls-remote", "--exit-code", destination_url, target, token=token)
        if proc.returncode == 2:
            return None
        if proc.returncode != 0:
            raise _called_process_error(
                proc,
                [GIT_BINARY, "ls-remote", destination_url, target],
                "ls-remote against the policy-derived destination failed",
            )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        sha = lines[0].split("\t", 1)[0].strip().lower()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise subprocess.CalledProcessError(
                1,
                [GIT_BINARY, "ls-remote", destination_url, target],
                proc.stdout,
                f"ls-remote returned a malformed SHA for {target!r}: {sha!r}",
            )
        return sha

    def fetch_remote_base(
        self,
        destination_url: str,
        destination: str,
        sha: str,
        *,
        token: Optional[str] = None,
    ) -> None:
        target = f"refs/heads/{destination}"
        by_sha = self.run(
            "fetch", "--no-tags", "--force",
            destination_url,
            f"{sha}:{BASE_REF}",
            token=token,
        )
        if by_sha.returncode != 0:
            by_name = self.run(
                "fetch", "--no-tags", "--force",
                destination_url,
                f"{target}:{BASE_REF}",
                token=token,
            )
            if by_name.returncode != 0:
                raise _called_process_error(
                    by_name,
                    [GIT_BINARY, "fetch", destination_url, target],
                    "could not fetch the policy-derived remote base",
                )
        verify = self.run(
            "rev-parse", "--verify", "--end-of-options", f"{BASE_REF}^{{commit}}"
        )
        if verify.returncode != 0 or verify.stdout.strip().lower() != sha:
            raise subprocess.CalledProcessError(
                1,
                [GIT_BINARY, "fetch", destination_url, target],
                verify.stdout,
                (
                    f"fetched base ({verify.stdout.strip() or 'MISSING'}) does not "
                    f"match ls-remote SHA {sha}"
                ),
            )

    def changed_paths(
        self, remote_sha: Optional[str], *, new_branch_base: bool = False
    ) -> List[str]:
        if remote_sha is None and new_branch_base:
            # New branch: final-tree diff against the CURRENT default-branch tip
            # (two-dot, same as an existing target). Everything that differs
            # from what is live on main is checked, including merge-only paths
            # and reverts hidden behind an older fork point.
            proc = self.run(
                "diff", *DIFF_SAFETY_ARGS,
                f"{BASE_REF}..{SOURCE_REF}",
                "--name-only", "-z",
            )
            command = [GIT_BINARY, "diff"]
        elif remote_sha is None:
            proc = self.run("ls-tree", "-r", "--name-only", "-z", SOURCE_REF)
            command = [GIT_BINARY, "ls-tree"]
        else:
            proc = self.run(
                "diff", *DIFF_SAFETY_ARGS,
                f"{BASE_REF}..{SOURCE_REF}",
                "--name-only", "-z",
            )
            command = [GIT_BINARY, "diff"]
        if proc.returncode != 0:
            raise _called_process_error(proc, command, "could not enumerate pushed paths")
        return list(dict.fromkeys(path for path in proc.stdout.split("\x00") if path))


def is_authentication_error(exc: subprocess.CalledProcessError) -> bool:
    """Whether a clean GitHub read failed specifically for lack of credentials."""
    message = (exc.stderr or "").lower()
    markers = (
        "authentication failed",
        "could not read username",
        "terminal prompts disabled",
        "requested url returned error: 401",
        "requested url returned error: 403",
        "http 401",
        "http 403",
        "repository not found",
    )
    return any(marker in message for marker in markers)


@contextmanager
def prepare_push(
    repo_dir: Path,
    *,
    destination_url: str,
    ref: str = "HEAD",
    read_token: Optional[str] = None,
    read_token_provider: Optional[Callable[[], str]] = None,
    diff_new_branch_against_default: bool = False,
) -> Iterator[PushPlan]:
    """Yield one immutable push plan backed by a temporary bare repository."""
    guard_repo = GuardRepository.create()
    try:
        actor_repo, source_sha, destination = resolve_source_commit(
            Path(repo_dir), ref, guard_repo.env
        )
        guard_repo.import_source(actor_repo, source_sha)
        token = read_token

        def lookup(tok: Optional[str]) -> "tuple[Optional[str], bool]":
            sha = guard_repo.remote_sha(destination_url, destination, token=tok)
            if sha is not None:
                guard_repo.fetch_remote_base(destination_url, destination, sha, token=tok)
                return sha, False
            # Default: a missing destination keeps the conservative full-tree
            # sweep (security review round 3). Only callers that explicitly opt
            # in (settings_pr: always a NEW branch, always a human-reviewed PR)
            # diff against the current default-branch tip instead.
            if not diff_new_branch_against_default or destination == DEFAULT_BASE_BRANCH:
                return None, False
            base_sha = guard_repo.remote_sha(
                destination_url, DEFAULT_BASE_BRANCH, token=tok
            )
            if base_sha is None:
                return None, False
            guard_repo.fetch_remote_base(
                destination_url, DEFAULT_BASE_BRANCH, base_sha, token=tok
            )
            return None, True

        try:
            remote_sha, new_branch_base = lookup(token)
        except subprocess.CalledProcessError as exc:
            if token is not None or read_token_provider is None or not is_authentication_error(exc):
                raise
            # Private repo: mint a separate read-only token, retry both the
            # authoritative lookup and fetch in this same isolated repo, then
            # discard it before policy evaluation and write-token minting.
            token = read_token_provider()
            remote_sha, new_branch_base = lookup(token)
        paths = guard_repo.changed_paths(remote_sha, new_branch_base=new_branch_base)
        token = None
        yield PushPlan(
            paths=paths,
            source_commit=source_sha,
            destination=destination,
            expected_remote_sha=remote_sha,
            destination_url=destination_url,
            guard_repo=guard_repo.repo,
            git_env=dict(guard_repo.env),
        )
    finally:
        guard_repo.close()


def staged_paths_for_push(
    repo_dir: Path,
    *,
    destination_url: str,
    ref: str = "HEAD",
    diff_new_branch_against_default: bool = False,
) -> List[str]:
    """Compatibility helper returning committed paths from the isolated plan."""
    with prepare_push(
        repo_dir, destination_url=destination_url, ref=ref,
        diff_new_branch_against_default=diff_new_branch_against_default,
    ) as plan:
        return plan.paths
