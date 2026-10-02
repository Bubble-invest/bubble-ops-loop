"""Core git-push guard: path-allow-list enforcement at the push boundary.

Notion v4 line 725 (verbatim):
  "GitHub ne fournit pas un vrai path-scope au niveau token contents:write.
   Les paths autorisés sont donc appliqués par wrapper local / git guard sur
   Morty, CI path guard, branch protection et audit Layer 4."

This module IS the "wrapper local / git guard sur Morty". Flow:

  1. Caller invokes Guard.push(repo_dir, dept, action, repo).
  2. Guard resolves the source SHA once, imports it into a guard-owned temporary
     bare repo, and derives paths against the literal policy destination there.
  3. Guard runs policy.enforce() for each path.
     - If ANY denied → audit `status:denied`, return 1, NO broker call.
     - Existing destination ref: REFUSE unless the authoritative destination
       SHA is an ancestor of the checked SHA (fast-forward only), audit
       `status:denied` reason `non_fast_forward`, return 1, NO broker call.
  4. If dry_run → audit `status:would_allow`, print plan, return 0.
  5. Else → subprocess-invoke the broker to mint a token.
     - If broker exits non-zero → audit `status:mint_failed`, return 1, NO push.
  6. Push exactly the checked SHA under a force-with-lease bound to the
     authoritative destination SHA (the lease only closes the check/push
     race; it never authorizes an overwrite, fast-forward is enforced in 3), with the token injected via
     `http.extraheader` ONLY for this single command, set via env
     (GIT_CONFIG_* triad — #923), never argv. Token never echoed, never
     logged, never persisted.
  7. Audit `status:pushed` or `status:push_failed`. Return 0 or 1 accordingly.

Design invariants (enforced by tests):
  - Token NEVER reaches audit log (audit.py FORBIDDEN_FIELDS + ghs_ raise).
  - Token NEVER reaches stdout/stderr (we capture broker stdout into a local
    variable and never `print()` it).
  - NO fallback to env GITHUB_TOKEN, PAT, or other token source.
  - Actor Git config/hooks/env never participate in remote reads or push.
  - Atomicity: if ONE path denied, ALL denied (no partial push).
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .audit import GuardAudit
from .policy_loader import KNOWN_ACTIONS
from .staging import (
    PushPlan,
    authenticated_git_env,
    hardened_git_command,
    prepare_push,
)


# Hard cap (matches broker MAX_TTL_MINUTES — Notion v4 audit example line 612).
DEFAULT_TOKEN_TTL_MINUTES = 60

# Board #1552: the fleet-standard install location for the broker binary
# (matches token-broker/deploy/INSTALL-ON-MORTY.md and the systemd unit's own
# `Environment=PATH=` prepend — bubble-vps-platform's systemd/bubble-agent@.service,
# board #1150). This is the LAST-RESORT fallback when the bare name isn't
# resolvable on PATH at all — see `resolve_broker_binary()`.
DEFAULT_BROKER_NAME = "bubble-token-broker"
DEFAULT_BROKER_ABS_PATH = "/opt/bubble-token-broker/bin/bubble-token-broker"

# #1619 step 2: an `agent-<slug>` OS user must NOT mint with an App key of its
# own. Its guard mints through the claude-side shim, which re-execs the
# root-owned wrapper under `sudo -n` (own-dept only, policy-forced, per-repo
# token). One default for the whole fleet: no per-dept config, no env flag.
DEFAULT_AGENT_MINT_SHIM = "/usr/local/bin/bubble-broker-mint-settings.sh"
AGENT_OS_USER_PREFIX = "agent-"


def _agent_mint_shim() -> Optional[str]:
    """The sudo mint shim when the current OS user is a dept ``agent-<slug>``
    and the shim is installed; otherwise None (Macs, `claude`, CI, tests)."""
    try:
        user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return None
    if not user.startswith(AGENT_OS_USER_PREFIX):
        return None
    shim = DEFAULT_AGENT_MINT_SHIM
    if os.path.isfile(shim) and os.access(shim, os.X_OK):
        return shim
    return None

# The broker installation and repository policy are for this GitHub
# organization. This constant is guard-owned; no actor config or CLI URL is
# consulted when building a destination.
GITHUB_ORG = "Bubble-invest"
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def github_repo_url(repo: str) -> str:
    """Return the sole network destination the guard may use for ``repo``."""
    if not _REPO_NAME_RE.fullmatch(repo) or repo in {".", ".."}:
        raise ValueError(f"invalid policy repository name: {repo!r}")
    return f"https://github.com/{GITHUB_ORG}/{repo}.git"


def resolve_broker_binary(broker: Optional[str] = None) -> str:
    """Resolve which broker binary to invoke.

    Board #1552 root cause: the golden `claude-settings.json` env.PATH used
    by a dept's live Claude Code session listed `/opt/bubble-token-broker/bin`
    nowhere at all, AND started with `/home/claude/.bun/bin` — a directory
    untraversable by every `agent-<slug>` OS user since board #1120. Python's
    `execvp`-family PATH search (used by `subprocess.run(["bubble-token-broker", ...])`)
    treats a directory-traversal EACCES as if the FILE itself were access-denied,
    and — per POSIX semantics — reports that EACCES as the terminal error
    instead of ENOENT, even though the broker was never actually present in
    that directory. So a bare-name exec surfaced a confusing PermissionError
    instead of "command not found", and (with the golden PATH now fixed to
    prepend the broker dir — see bubble-vps-platform PR) callers on an
    UNPATCHED or stale settings.json would still fail the same way.

    This function is the guard-side half of the fix: it gives any caller that
    invokes the guard directly (bypassing `force_commit_and_push`'s own PATH
    prepend, e.g. Ben's fresh post-rotation session calling `bubble-git-guard
    push` per CLAUDE.md STEP E) a safety net that does not depend on PATH
    being correct.

    Resolution order:
      1. An EXPLICIT `broker` (e.g. `--broker /some/path`) is never
         second-guessed — used verbatim.
      1b. (#1619 step 2) Running as an `agent-<slug>` OS user with the sudo
         mint shim installed: the SHIM, never the in-process broker with an
         agent-readable App key.
      2. `shutil.which(DEFAULT_BROKER_NAME)` — a PATH search that (unlike the
         raw execvp path) simply treats an inaccessible directory as "not
         found there" per-entry (it uses `os.access(..., os.X_OK)`, which
         does not raise) and keeps looking, so a merely-misordered PATH still
         resolves correctly here even before any settings.json fix rolls out.
      3. The fleet-standard absolute install path, IF it exists and is
         executable — the same fallback board #1150 already wired into the
         systemd unit's own `Environment=PATH=`.
      4. Otherwise, return the bare name unchanged — `Guard.push()`'s
         FileNotFoundError/PermissionError handling takes it from there with
         a legible error instead of a bare traceback.
    """
    if broker:
        return broker
    shim = _agent_mint_shim()
    if shim:
        return shim
    found = shutil.which(DEFAULT_BROKER_NAME)
    if found:
        return found
    if os.path.isfile(DEFAULT_BROKER_ABS_PATH) and os.access(DEFAULT_BROKER_ABS_PATH, os.X_OK):
        return DEFAULT_BROKER_ABS_PATH
    return DEFAULT_BROKER_NAME


class Guard:
    """Orchestrates the path-check → broker-mint → git-push pipeline.

    Construct once per process (the policy and broker_cmd are immutable for
    the guard's lifetime). Call `push()` per intended `git push`.
    """

    def __init__(
        self,
        policy: Any,
        broker_cmd: Optional[List[str]] = None,
        audit_log_path: Optional[Path] = None,
        default_remote: str = "origin",
        default_branch: str = "HEAD",
    ) -> None:
        self.policy = policy
        # Path to the broker binary. Default = resolve_broker_binary()'s
        # PATH-lookup-then-fleet-standard-absolute-path fallback (board #1552).
        # Tests inject an absolute path to a stub via broker_cmd=[...], which
        # is always respected verbatim (never re-resolved).
        self.broker_cmd = list(broker_cmd) if broker_cmd else [resolve_broker_binary()]
        self.audit = GuardAudit(log_path=audit_log_path)
        self.default_remote = default_remote
        self.default_branch = default_branch

    # ------------------------------------------------------------------ paths

    def check_paths(
        self,
        paths: List[str],
        action: str,
        repo: str,
    ) -> Tuple[bool, List[str], List[str]]:
        """Run each path through the policy. Returns (all_allowed, ok_paths, denied_reasons).

        - `all_allowed` is True iff EVERY path passes AND `paths` is non-empty.
        - `ok_paths` is the subset that individually passed (informational only).
        - `denied_reasons` is the list of denial reasons (atomicity: if non-empty,
          the entire batch is denied at the caller).
        """
        if action not in KNOWN_ACTIONS:
            return False, [], [f"unknown action class: {action!r} (not in {sorted(KNOWN_ACTIONS)})"]
        if not paths:
            return False, [], ["empty path set: no paths staged to push"]

        actor = _actor_for_dept_via_policy(self.policy)
        ok_paths: List[str] = []
        all_reasons: List[str] = []

        # Per-path enforcement so we can surface PER-PATH reasons (the broker's
        # Policy.enforce returns reasons for the whole batch). Calling it with
        # paths=[p] gives us isolated per-path verdicts.
        for p in paths:
            allowed, reasons = self.policy.enforce(
                actor=actor, repo=repo, action=action, paths=[p]
            )
            if allowed:
                ok_paths.append(p)
            else:
                # Tag reasons with the path that caused them, so denied_paths
                # callers can extract the offending path from each reason.
                for r in reasons:
                    if p not in r:
                        all_reasons.append(f"{p}: {r}")
                    else:
                        all_reasons.append(r)

        return (len(all_reasons) == 0 and len(ok_paths) == len(paths)), ok_paths, all_reasons

    # ------------------------------------------------------------------ push

    def push(
        self,
        repo_dir: Path,
        dept: str,
        action: str,
        repo: str,
        *,
        dry_run: bool = False,
        remote: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> int:
        """Full guarded-push flow. Returns process exit code (0 = success)."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        actor = f"ops-loop-{dept}"
        remote = remote or self.default_remote
        ref = ref or self.default_branch
        if remote != "origin":
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=0, denied_paths=[],
                reasons=["named Git remotes are not trusted; only the policy-derived URL is supported"],
            )
            print(
                "DENIED: --remote is no longer a routing input; use the policy-derived destination",
                file=__import__("sys").stderr,
            )
            return 1
        try:
            destination_url = github_repo_url(repo)
        except ValueError as exc:
            print(f"DENIED: {exc}", file=__import__("sys").stderr)
            return 1

        try:
            # The settings_pr push mints its pre-push read token with the
            # push's own action (#543's base fetch). Runtime pushes use
            # runtime_read, which the root wrapper (bubble-broker-mint-settings-
            # root.sh) serves for the caller's own dept since #1619 step 1.
            read_action = "settings_pr" if action == "settings_pr" else "runtime_read"

            def mint_read_token() -> str:
                token = self._mint_token(
                    ts=ts, actor=actor, dept=dept, action=read_action,
                    repo=repo, paths=[],
                )
                if token is None:
                    raise subprocess.CalledProcessError(
                        1,
                        [self.broker_cmd[0], "mint", "--action", read_action],
                        stderr="read-token mint failed for private repository",
                    )
                return token

            with prepare_push(
                Path(repo_dir), destination_url=destination_url, ref=ref,
                read_token_provider=mint_read_token,
                diff_new_branch_against_default=(action == "settings_pr"),
            ) as plan:
                return self._execute_plan(
                    plan, ts=ts, actor=actor, dept=dept, action=action,
                    repo=repo, dry_run=dry_run,
                )
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or "git operation failed").strip()
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=0, denied_paths=[],
                reasons=[f"isolated git preparation failed: {error}"],
            )
            print(f"DENIED: git error: {error}", flush=True, file=__import__("sys").stderr)
            return 1

    def _execute_plan(
        self,
        plan: PushPlan,
        *,
        ts: str,
        actor: str,
        dept: str,
        action: str,
        repo: str,
        dry_run: bool,
    ) -> int:
        """Authorize and execute a plan while its isolated repo still exists."""
        paths = plan.paths
        all_ok, _ok_paths, denied_reasons = self.check_paths(
            paths, action=action, repo=repo
        )
        if not all_ok:
            denied_paths = _extract_offending_paths(denied_reasons, paths)
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=len(paths),
                denied_paths=denied_paths, reasons=denied_reasons,
            )
            import sys
            print(f"DENIED ({len(denied_paths)} path(s) failed policy):", file=sys.stderr)
            for reason in denied_reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 1

        if not self._is_fast_forward(plan):
            reason = (
                "non_fast_forward: the remote branch has commits the pushed "
                "source does not contain; a force-with-lease bound to the "
                "current remote SHA would silently overwrite them"
            )
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=len(paths),
                denied_paths=[], reasons=[reason],
            )
            import sys
            print(
                f"DENIED: {reason}. Pull/rebase onto the remote first "
                "(safe_pull), then retry. Your local commits are untouched.",
                file=sys.stderr,
            )
            return 1

        if dry_run:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="would_allow", paths_count=len(paths),
                token_ttl_minutes=DEFAULT_TOKEN_TTL_MINUTES,
            )
            import sys
            print(
                f"[dry-run] Would mint token (action={action}, repo={repo}) and "
                f"push {len(paths)} path(s):",
                file=sys.stderr,
            )
            for path in paths:
                print(f"  + {path}", file=sys.stderr)
            return 0

        token = self._mint_token(
            ts=ts, actor=actor, dept=dept, action=action, repo=repo, paths=paths
        )
        if token is None:
            return 1
        push_rc, push_stderr = self._run_git_push(plan=plan, token=token)
        del token

        if push_rc != 0:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="push_failed", paths_count=len(paths),
                token_ttl_minutes=DEFAULT_TOKEN_TTL_MINUTES,
                error=f"git push exit {push_rc}: {push_stderr.strip()[:200]}",
            )
            print(
                f"ERROR: git push failed (exit {push_rc}): {push_stderr.strip()}",
                file=__import__("sys").stderr,
            )
            return 1

        self._safe_audit(
            ts=ts, actor=actor, dept=dept, repo=repo, action=action,
            status="pushed", paths_count=len(paths),
            token_ttl_minutes=DEFAULT_TOKEN_TTL_MINUTES,
        )
        return 0

    @staticmethod
    def _is_fast_forward(plan: PushPlan) -> bool:
        """True for a new ref, or when the destination SHA is an ancestor of
        the checked SHA. Any git error fails closed (not a fast-forward)."""
        if not plan.expected_remote_sha:
            return True
        proc = subprocess.run(
            hardened_git_command(
                "merge-base", "--is-ancestor",
                plan.expected_remote_sha, plan.source_commit,
            ),
            cwd=str(plan.guard_repo),
            capture_output=True,
            text=True,
            env=dict(plan.git_env),
            check=False,
        )
        return proc.returncode == 0

    def _mint_token(
        self, *, ts: str, actor: str, dept: str, action: str,
        repo: str, paths: List[str],
    ) -> Optional[str]:
        """Mint one token, keeping its value out of logs and diagnostics."""
        try:
            result = subprocess.run(
                [*self.broker_cmd, "mint", "--dept", dept, "--action", action,
                 "--repo", repo, *_paths_arg(paths)],
                capture_output=True, text=True, check=False,
            )
        except (FileNotFoundError, PermissionError) as exc:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error=f"broker exec failed: {type(exc).__name__}",
            )
            print(
                f"ERROR: permission denied or broker missing while executing "
                f"{self.broker_cmd[0]!r}: {exc}. Check the configured binary and PATH "
                f"({os.environ.get('PATH', '')!r}).",
                file=__import__("sys").stderr,
            )
            return None
        if result.returncode != 0:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error=f"broker exit {result.returncode}: {result.stderr.strip()[:200]}",
            )
            print(
                f"ERROR: broker mint failed (exit {result.returncode}): {result.stderr.strip()}",
                file=__import__("sys").stderr,
            )
            return None
        token = result.stdout.strip()
        if not token.startswith("ghs_"):
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error="broker stdout did not contain a ghs_ token",
            )
            print("ERROR: broker did not return a valid token shape", file=__import__("sys").stderr)
            return None
        return token

    # ------------------------------------------------------------------ helpers

    def _safe_audit(self, **event: Any) -> None:
        """Wrap GuardAudit.log so an audit failure can't take down the guard."""
        try:
            self.audit.log(**event)
        except Exception as exc:  # pragma: no cover
            # If audit refuses (e.g. token leak detected), surface to stderr.
            import sys
            print(f"WARNING: audit refused event: {exc}", file=sys.stderr)

    def _run_git_push(
        self,
        plan: PushPlan,
        token: str,
    ) -> Tuple[int, str]:
        """Push from the guard-owned bare repo to the policy-derived URL.

        We use the `http.extraheader` mechanism per GitHub App docs. The
        header VALUE travels via the process ENVIRONMENT — the
        `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>`
        triad (supported since git 2.31; this fleet runs 2.50+) — NOT via
        `-c` on argv. `env=` is not recorded in `/proc/<pid>/cmdline` (unlike
        argv), so the token never appears in process listings or
        bash-command transcripts. git sends the IDENTICAL `Authorization`
        header either way — only WHERE the token travels changes; the HTTP
        request git makes to github.com is unchanged by this fix.

        Board #923 (final site of this class, same as #921/#311 — see
        scripts/lib/dispatch_helpers.py's `_env_with_bearer_auth_header`,
        live-validated on the VPS with a real broker token): the previous
        form here was `-c http.extraheader=Authorization: Basic <b64>` on
        argv, which IS visible via /proc/<pid>/cmdline on Linux — the exact
        leak this function's own comment used to flag. Fixed by moving the
        header value into env instead of argv.

        `-c credential.helper=` (empty string; not a secret, fine on argv)
        is added to argv to neutralize any ambient credential-helper chain,
        so the explicit `extraHeader` set below (via env) is unambiguously
        what git uses to authenticate — matching the fleet-standard pattern.

        Auth header form: GitHub's git smart-HTTP endpoint (github.com)
        requires HTTP Basic auth using `x-access-token` as the username and
        the installation token as the password. The `Authorization: Bearer`
        form works for the REST API (api.github.com) but is REJECTED with
        401 by the git push endpoint (empirically verified 2026-05-20 on
        Morty, Step 7 deployment smoke).
        """
        # Bind the authorization decision to exactly the object and remote
        # state that were inspected before token minting. Never pass `HEAD`
        # (or any other symbolic source) here: it may have moved meanwhile.
        # An empty expected value means the destination was verified absent;
        # force-with-lease then rejects creation if another actor won the race.
        lease = (
            f"--force-with-lease={plan.destination_ref}:"
            f"{plan.expected_remote_sha or ''}"
        )
        refspec = f"{plan.source_commit}:{plan.destination_ref}"
        cmd = hardened_git_command(
            "push",
            # Bypass pre-push explicitly; core.hooksPath=/dev/null in
            # hardened_git_command() independently prevents every client-side
            # hook (including reference-transaction) from being discovered.
            "--no-verify",
            lease,
            plan.destination_url,
            refspec,
        )
        env = authenticated_git_env(plan.git_env, token)

        proc = subprocess.run(
            cmd,
            cwd=str(plan.guard_repo),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        # IMPORTANT: do NOT log `env` (it carries the token via GIT_CONFIG_*
        # — see above). `cmd` itself is token-free after #923, but we still
        # scrub stderr defensively in case git ever echoes the header/token
        # (e.g. verbose curl tracing) into its own error output.
        stderr_redacted = proc.stderr
        if token:
            import base64
            basic_b64 = base64.b64encode(
                f"x-access-token:{token}".encode("ascii")
            ).decode("ascii")
            stderr_redacted = stderr_redacted.replace(token, "<TOKEN-REDACTED>")
            stderr_redacted = stderr_redacted.replace(basic_b64, "<TOKEN-B64-REDACTED>")
        return proc.returncode, stderr_redacted


# --- module helpers -------------------------------------------------------


def _actor_for_dept_via_policy(policy: Any) -> str:
    """Return the actor string the policy expects.

    The broker's Policy.from_yaml stores `actor` directly. We trust the YAML
    file's `actor` field as the source of truth (the CLI's --dept is just a
    handle; the actor is policy-defined).
    """
    return getattr(policy, "actor", "")


def _paths_arg(paths: List[str]) -> List[str]:
    """Build the `--paths a b c` argv suffix (empty if no paths)."""
    if not paths:
        return []
    return ["--paths", *paths]


def _extract_offending_paths(reasons: List[str], all_paths: List[str]) -> List[str]:
    """Heuristic: a reason that starts with "<path>:" identifies that path
    as the offender. Falls back to "all paths" when ambiguous."""
    offending: List[str] = []
    for r in reasons:
        for p in all_paths:
            if r.startswith(f"{p}:") or f"path {p!r}" in r:
                if p not in offending:
                    offending.append(p)
                break
    return offending or list(all_paths)
