"""Core git-push guard: path-allow-list enforcement at the push boundary.

Notion v4 line 725 (verbatim):
  "GitHub ne fournit pas un vrai path-scope au niveau token contents:write.
   Les paths autorisés sont donc appliqués par wrapper local / git guard sur
   Morty, CI path guard, branch protection et audit Layer 4."

This module IS the "wrapper local / git guard sur Morty". Flow:

  1. Caller invokes Guard.push(repo_dir, dept, action, repo).
  2. Guard prepares one immutable push plan: checked source SHA, authoritative
     remote destination SHA, and staged + unpushed-commit paths.
  3. Guard runs policy.enforce() for each path.
     - If ANY denied → audit `status:denied`, return 1, NO broker call.
  4. If dry_run → audit `status:would_allow`, print plan, return 0.
  5. Else → subprocess-invoke the broker to mint a token.
     - If broker exits non-zero → audit `status:mint_failed`, return 1, NO push.
  6. Push exactly the checked SHA under a force-with-lease bound to the
     authoritative destination SHA, with the token injected via
     `http.extraheader` ONLY for this single command, set via env
     (GIT_CONFIG_* triad — #923), never argv. Token never echoed, never
     logged, never persisted.
  7. Audit `status:pushed` or `status:push_failed`. Return 0 or 1 accordingly.

Design invariants (enforced by tests):
  - Token NEVER reaches audit log (audit.py FORBIDDEN_FIELDS + ghs_ raise).
  - Token NEVER reaches stdout/stderr (we capture broker stdout into a local
    variable and never `print()` it).
  - NO fallback to env GITHUB_TOKEN, PAT, or other token source.
  - Atomicity: if ONE path denied, ALL denied (no partial push).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .audit import GuardAudit
from .policy_loader import KNOWN_ACTIONS
from .staging import prepare_push


# Hard cap (matches broker MAX_TTL_MINUTES — Notion v4 audit example line 612).
DEFAULT_TOKEN_TTL_MINUTES = 60

# Board #1552: the fleet-standard install location for the broker binary
# (matches token-broker/deploy/INSTALL-ON-MORTY.md and the systemd unit's own
# `Environment=PATH=` prepend — bubble-vps-platform's systemd/bubble-agent@.service,
# board #1150). This is the LAST-RESORT fallback when the bare name isn't
# resolvable on PATH at all — see `resolve_broker_binary()`.
DEFAULT_BROKER_NAME = "bubble-token-broker"
DEFAULT_BROKER_ABS_PATH = "/opt/bubble-token-broker/bin/bubble-token-broker"


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

        # Step 1: compute staged paths
        try:
            plan = prepare_push(Path(repo_dir), remote=remote, ref=ref)
            paths = plan.paths
        except subprocess.CalledProcessError as exc:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=0,
                denied_paths=[], reasons=[f"git error reading staged paths: {exc.stderr.strip()}"],
            )
            print(f"DENIED: git error: {exc.stderr.strip()}", flush=True, file=__import__("sys").stderr)
            return 1

        # Step 2: path check
        all_ok, ok_paths, denied_reasons = self.check_paths(paths, action=action, repo=repo)
        if not all_ok:
            denied_paths = _extract_offending_paths(denied_reasons, paths)
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="denied", paths_count=len(paths),
                denied_paths=denied_paths, reasons=denied_reasons,
            )
            import sys
            print(
                f"DENIED ({len(denied_paths)} path(s) failed policy):",
                file=sys.stderr,
            )
            for r in denied_reasons:
                print(f"  - {r}", file=sys.stderr)
            return 1

        # Step 3: dry-run short-circuit
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
            for p in paths:
                print(f"  + {p}", file=sys.stderr)
            return 0

        # Step 4: invoke broker to mint a token
        try:
            broker_result = subprocess.run(
                [
                    *self.broker_cmd, "mint",
                    "--dept", dept,
                    "--action", action,
                    "--repo", repo,
                    *_paths_arg(paths),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error=f"broker not found: {self.broker_cmd[0]}",
            )
            import sys
            print(f"ERROR: broker binary not found: {self.broker_cmd[0]} ({exc})", file=sys.stderr)
            return 1
        except PermissionError as exc:
            # Board #1552: a bare-name exec (e.g. "bubble-token-broker") whose
            # PATH search hits an UNTRAVERSABLE directory before ever finding
            # the binary surfaces as PermissionError, not FileNotFoundError —
            # execvp reports the directory-traversal EACCES as the terminal
            # error rather than ENOENT (POSIX semantics), which reads exactly
            # like "found it, but denied" even though the broker was never in
            # that directory at all. Name the PATH problem explicitly so this
            # doesn't look like a bare, unexplained traceback.
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error=f"broker exec permission denied: {self.broker_cmd[0]}",
            )
            import sys
            print(
                f"ERROR: permission denied executing broker binary "
                f"{self.broker_cmd[0]!r} ({exc}). This usually means a "
                f"directory earlier in PATH is untraversable by this user "
                f"(e.g. a shared home dir locked down for other users) — "
                f"execvp reports that as \"permission denied\" instead of "
                f"\"not found\", even when the broker isn't actually in that "
                f"directory. Current PATH={os.environ.get('PATH', '')!r}. "
                f"Pass --broker with an absolute path to bypass PATH search "
                f"entirely.",
                file=sys.stderr,
            )
            return 1

        if broker_result.returncode != 0:
            # NEVER log broker stdout (might contain partial token). Only stderr,
            # which by contract is policy-denial reason or error text.
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error=f"broker exit {broker_result.returncode}: {broker_result.stderr.strip()[:200]}",
            )
            import sys
            print(
                f"ERROR: broker mint failed (exit {broker_result.returncode}): "
                f"{broker_result.stderr.strip()}",
                file=sys.stderr,
            )
            return 1

        # IMPORTANT: capture the token into a LOCAL variable. Never log it,
        # never print it, never put it into self.audit.
        _token = broker_result.stdout.strip()
        if not _token.startswith("ghs_"):
            # Defense-in-depth: broker contract says stdout = the token. If it
            # doesn't look like one, treat as mint failure.
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="mint_failed", paths_count=len(paths),
                error="broker stdout did not contain a ghs_ token",
            )
            import sys
            print("ERROR: broker did not return a valid token shape", file=sys.stderr)
            return 1

        # Step 5: run `git push` with the token injected via http.extraheader
        # for THIS process only. We do NOT export GITHUB_TOKEN globally.
        push_rc, push_stderr = self._run_git_push(
            repo_dir=Path(repo_dir),
            remote=remote,
            source_commit=plan.source_commit,
            destination_ref=plan.destination_ref,
            expected_remote_sha=plan.expected_remote_sha,
            token=_token,
        )

        # Drop our reference promptly (Python can't truly wipe but at least
        # release the binding).
        del _token

        if push_rc != 0:
            self._safe_audit(
                ts=ts, actor=actor, dept=dept, repo=repo, action=action,
                status="push_failed", paths_count=len(paths),
                token_ttl_minutes=DEFAULT_TOKEN_TTL_MINUTES,
                error=f"git push exit {push_rc}: {push_stderr.strip()[:200]}",
            )
            import sys
            print(
                f"ERROR: git push failed (exit {push_rc}): {push_stderr.strip()}",
                file=sys.stderr,
            )
            return 1

        self._safe_audit(
            ts=ts, actor=actor, dept=dept, repo=repo, action=action,
            status="pushed", paths_count=len(paths),
            token_ttl_minutes=DEFAULT_TOKEN_TTL_MINUTES,
        )
        return 0

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
        repo_dir: Path,
        remote: str,
        source_commit: str,
        destination_ref: str,
        expected_remote_sha: Optional[str],
        token: str,
    ) -> Tuple[int, str]:
        """Invoke `git push` with the token injected ONLY for this command.

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
        import base64
        basic_b64 = base64.b64encode(
            f"x-access-token:{token}".encode("ascii")
        ).decode("ascii")
        # credential.helper="" is NOT a secret — safe to keep on argv. It
        # disables any ambient helper chain so the explicit extraHeader
        # (set via env below, never argv) is what git actually uses.
        # Bind the authorization decision to exactly the object and remote
        # state that were inspected before token minting. Never pass `HEAD`
        # (or any other symbolic source) here: it may have moved meanwhile.
        # An empty expected value means the destination was verified absent;
        # force-with-lease then rejects creation if another actor won the race.
        lease = f"--force-with-lease={destination_ref}:{expected_remote_sha or ''}"
        refspec = f"{source_commit}:{destination_ref}"
        cmd = [
            "git",
            "-c", "credential.helper=",
            "push",
            lease,
            remote,
            refspec,
        ]
        # Scrubbed env: keep PATH but DO NOT propagate any GITHUB_TOKEN
        # the caller might have set (fail-closed against PAT fallback).
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        # /bin/true exits 0 with empty stdout — guarantees git CANNOT obtain
        # any credential via the askpass fallback. /dev/null cannot be exec'd
        # on Linux (it's a char device), which would itself raise an error.
        env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "/bin/true")
        env["GIT_TERMINAL_PROMPT"] = "0"
        # #923: the auth header travels via the GIT_CONFIG_* env triad,
        # NEVER via argv (see docstring). Append at the next free index
        # rather than clobbering index 0, in case an ambient GIT_CONFIG_*
        # entry is already present in the environment (defensive — no
        # caller of this method currently sets one).
        try:
            gc_count = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            gc_count = 0
        env[f"GIT_CONFIG_KEY_{gc_count}"] = "http.extraheader"
        env[f"GIT_CONFIG_VALUE_{gc_count}"] = f"Authorization: Basic {basic_b64}"
        env["GIT_CONFIG_COUNT"] = str(gc_count + 1)

        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
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
