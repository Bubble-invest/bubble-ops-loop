"""Policy enforcement for the token broker.

Notion v4 §"Classes de tokens éphémères" + §"Policies par type d'acteur"
(lines 616-694). This module implements the four action classes:

  - runtime_read       — read declared repos only
  - runtime_write_own  — write only to allowed_paths in actor's own_repo
  - open_priority_pr   — PR (not direct push) to queues/management/** in a
                          child repo listed under pull_requests.can_open_to
  - settings_pr        — PR required for any structural path
                          (dept.yaml, prompts, subagents, skills, tools,
                          policies, .claude/settings.json)

Crucially (Notion line 715): GitHub does NOT provide a true `contents:write`
path-scope at the token level. The token only constrains REPO and
PERMISSION CLASS. The PATH allow-list is enforced by THIS module (broker
wrapper) and the Morty git guard (Step 3c, separate component).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# Patterns that are considered "structural" — these are always settings_pr
# territory regardless of allowed_paths. Per Notion v4 line 622.
#
# These are the MISSION-DEFINITION files: an agent may NOT push them directly
# (the box-side credential helper mints a read-only token when it detects any of
# these in an un-pushed delta). They change only via a PR {{OPERATOR}}/{{OPERATOR_2}} merges.
# Governance fix 2026-06-01 ({{OPERATOR}} msg 3582) ADDED the top-level dept mission
# entry-points (CLAUDE.md, MANDATE.md, skills_manifest.yaml, config.yaml,
# gate_policy.yaml) — the original list missed them, which is exactly how Tony
# was able to bake the transient "IPO Watch" topic into layers/1/PROMPT.md.
# NB: top-level CLAUDE.md is the dept's mission entry-point on the box (NOT
# .claude/CLAUDE.md). whiteboard.yaml and WORKING_MEMORY.md are deliberately
# ABSENT — they are writable runtime/working-memory state.
STRUCTURAL_PATH_GLOBS: tuple[str, ...] = (
    "dept.yaml",
    "CLAUDE.md",            # dept mission entry-point (top-level on the box)
    "MANDATE.md",           # dept doctrine
    "skills_manifest.yaml",  # declares which skills the dept runs
    "config.yaml",          # dept config (Maya)
    "gate_policy.yaml",     # autonomy/gate boundaries
    "layers/**",
    ".claude/agents/**",
    ".claude/settings.json",
    ".claude/CLAUDE.md",
    "skills/**",
    "tools/**",
    "subagents/**",
    "policies/**",
    "templates/**",
    "missions/**",
    "assets/**",
    "db/schema.sql",       # canonical DB schema — PR-only (Ben exception #7, 2026-06-06). db/fund.sqlite stays runtime-writable.          # dept doctrine/voice assets (e.g. Maya's maya-doctrine.md) — mission-like, PR-gated ({{OPERATOR}} 2026-06-06)
)

# FRAMEWORK-REPO-ONLY structural paths (governance fix 2026-06-09, {{OPERATOR}}).
#
# These protect the FRAMEWORK's OWN SOURCE in the canonical repo (bubble-ops-loop):
# the scaffolder, the loop/dispatch library, CI, and the token-broker/git-guard
# security code itself. A direct push rewriting any of these silently degrades the
# whole fleet (e.g. commit f3213b7 reverted scaffold.py + PR #47's canonical loop
# protocol, undetected, because the framework source was NOT structural-locked).
#
# Why a SEPARATE list (not folded into STRUCTURAL_PATH_GLOBS): the dept repos VENDOR
# `scripts/lib/**` (ben/maya carry dispatch_helpers.py etc.) and sync it at runtime.
# A blanket `scripts/lib/**` in the shared list would 403 those legitimate dept
# lib-syncs. So these globs apply ONLY when the repo being pushed is the framework
# repo — see `is_structural_for_repo()`.
FRAMEWORK_REPO_NAME: str = "bubble-ops-loop"
FRAMEWORK_STRUCTURAL_PATH_GLOBS: tuple[str, ...] = (
    "scripts/lib/**",      # the scaffolder + canonical loop/dispatch library
    "scripts/bootstrap-dept.sh",
    "scripts/sync-dispatch-lib.sh",
    "scripts/**",          # #961 (2026-09-03): broadened from the three globs
                            # above to cover ALL of scripts/ in the framework
                            # repo, not just lib/. Card incident (commit
                            # 5513824) touched scripts/lib/budget.py specifically,
                            # but scripts/ also holds dept lifecycle + deploy
                            # tooling (activate-dept.sh, migrate-dept.sh,
                            # retire-dept.sh, revendor-all-depts.sh,
                            # dispatch_directives.py, the install-*.sh cron
                            # installers, ...) that is just as capable of
                            # silently degrading the fleet if rewritten
                            # unreviewed. Kept the three narrower globs above
                            # (now redundant, left for the historical f3213b7/
                            # #55 attribution) rather than removing them — only
                            # ADDING protection here, per #961 scope. No actor
                            # policy in deploy/policies/ has own_repo (or a
                            # write rule) targeting bubble-ops-loop itself, so
                            # this repo is never a legitimate runtime_write_own
                            # target — broadening to all of scripts/** does not
                            # block any observed automated workflow (verified by
                            # grep across deploy/policies/*.yaml and
                            # .github/workflows/ — no push/commit step touches
                            # scripts/ in CI). Flagged for reviewer anyway since
                            # it is a broader lock than the literal #961 gap.
    ".github/**",          # CI workflows (a bad edit here can mask regressions)
    "token-broker/**",     # the push chokepoint's own source
    "git-guard/**",        # the guard's own source
)

# Paths that are ELIGIBLE for a settings_pr WITHOUT being STRUCTURAL.
#
# #913 (2026-08-12, Joris-approved): landing test files was blocked on BOTH
# routes at once — runtime_write_own (tests/ absent from every actor's
# allowed_paths) AND settings_pr (tests/ correctly fails `_is_structural()`,
# since a test file is not a mission-definition file). See #773/#891/#888.
#
# Deliberately a SEPARATE list from STRUCTURAL_PATH_GLOBS, not folded into it:
# `enforce()`'s runtime_write_own branch denies a structural path OUTRIGHT,
# before ever consulting allowed_paths (see the `if _is_structural(p):
# ... continue` below) — so marking tests/ structural would silently CLOSE
# the direct-commit route the moment it's opened via allowed_paths. Keeping
# the lists disjoint lets tests/ be BOTH direct-push-eligible (once an
# actor's policy allowed_paths includes tests/**, see deploy/policies/
# *.template.yaml) AND settings-PR-eligible (for a dept whose policy hasn't
# been redeployed yet, or a test an agent wants human review on before it
# lands) without touching mission-file (STRUCTURAL_PATH_GLOBS) semantics at
# all. Scope is intentionally narrow — tests/ only, not a general loosening.
SETTINGS_PR_EXTRA_GLOBS: tuple[str, ...] = (
    "tests/**",
)

KNOWN_ACTIONS: frozenset[str] = frozenset(
    {"runtime_read", "runtime_write_own", "open_priority_pr", "settings_pr"}
)


def _glob_match(path: str, pattern: str) -> bool:
    """Match a path against a glob pattern with `**` semantics.

    Examples:
      _glob_match("outputs/2026-05-20/1/summary.md", "outputs/**") -> True
      _glob_match("dept.yaml", "layers/**") -> False
    """
    if "**" not in pattern:
        return fnmatch.fnmatch(path, pattern)
    # Recursive: split on `**` and compare prefix.
    prefix = pattern.split("**", 1)[0]
    if prefix and not path.startswith(prefix):
        return False
    return True


def _is_structural(path: str) -> bool:
    """True if the path is structural (settings_pr territory).

    Repo-agnostic: applies the dept-mission globs that are structural in EVERY
    repo. For framework-repo-only protection use is_structural_for_repo().
    """
    return any(_glob_match(path, g) for g in STRUCTURAL_PATH_GLOBS)


def _is_settings_pr_eligible(path: str, repo_name: str | None = None) -> bool:
    """True if `path` may be the target of a settings_pr (#913).

    = structural for the target repo (shared mission globs in every repo,
      PLUS the framework-only globs — scripts/lib/**, .github/**,
      token-broker/**, git-guard/** — when `repo_name` is bubble-ops-loop;
      see is_structural_for_repo()), always PR-only, never runtime_write_own
      OR in SETTINGS_PR_EXTRA_GLOBS (currently just tests/** — PR-eligible
      WITHOUT being locked out of runtime_write_own; see the comment above
      SETTINGS_PR_EXTRA_GLOBS for why this is a separate list).

    #961 (2026-09-03): repo-aware so a framework-only structural path (e.g.
    scripts/lib/budget.py in bubble-ops-loop) is still ELIGIBLE for the PR
    route even though enforce()'s runtime_write_own branch now denies it
    outright (see is_structural_for_repo() call below) — otherwise a
    framework-lib change would have NO push path at all.
    """
    return is_structural_for_repo(path, repo_name) or any(
        _glob_match(path, g) for g in SETTINGS_PR_EXTRA_GLOBS
    )


def is_structural_for_repo(path: str, repo_name: str | None = None) -> bool:
    """True if `path` is structural in the context of the repo being pushed.

    - The shared STRUCTURAL_PATH_GLOBS (dept mission files) apply to every repo.
    - The FRAMEWORK_STRUCTURAL_PATH_GLOBS (the framework's own source) apply ONLY
      when `repo_name` is the framework repo (bubble-ops-loop). This is what stops
      an agent silently rewriting scaffold.py while leaving the depts' legitimate
      vendored-lib syncs (scripts/lib/**) untouched.

    `repo_name` may be a bare name ("bubble-ops-loop") or "org/name"
    ("Bubble-invest/bubble-ops-loop"); only the trailing path segment is compared.
    """
    if _is_structural(path):
        return True
    if repo_name:
        bare = repo_name.rstrip("/").split("/")[-1]
        if bare == FRAMEWORK_REPO_NAME:
            return any(_glob_match(path, g) for g in FRAMEWORK_STRUCTURAL_PATH_GLOBS)
    return False


@dataclass
class Policy:
    """In-memory representation of a single actor's github_access block."""

    actor: str
    own_repo: str | None
    read_repos: list[str]
    write_rules: list[dict[str, Any]]  # repo, allowed_paths, mode
    can_open_to: list[str]
    pr_target_paths: list[str]
    can_open_settings_pr: bool

    # --- Construction ----------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path | str) -> Policy:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        block = (doc or {}).get("github_access") or {}
        actor = str(block.get("actor", ""))
        own_repo = block.get("own_repo")
        read = list(block.get("read") or [])
        write_rules: list[dict[str, Any]] = []
        for entry in block.get("write") or []:
            if not isinstance(entry, dict):
                continue
            write_rules.append(
                {
                    "repo": entry.get("repo"),
                    "allowed_paths": list(entry.get("allowed_paths") or []),
                    "mode": entry.get("mode"),
                }
            )
        prs = block.get("pull_requests") or {}
        return cls(
            actor=actor,
            own_repo=own_repo,
            read_repos=read,
            write_rules=write_rules,
            can_open_to=list(prs.get("can_open_to") or []),
            pr_target_paths=list(prs.get("target_paths") or ["queues/management/**"]),
            can_open_settings_pr=bool(prs.get("can_open_settings_pr", False)),
        )

    # --- Enforcement -----------------------------------------------------

    def enforce(
        self,
        actor: str,
        repo: str,
        action: str,
        paths: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Return (allowed, reasons).

        - `actor` is used for an additional sanity check (must match the policy
          file's actor).
        - `repo` is the target repository.
        - `action` is one of the four classes in KNOWN_ACTIONS.
        - `paths` is the list of file paths the caller plans to touch; only
          consulted for the *_write_* / *_pr action classes.
        """
        paths = paths or []
        reasons: list[str] = []

        if action not in KNOWN_ACTIONS:
            return False, [f"unknown action class: {action!r}"]

        if actor != self.actor:
            reasons.append(
                f"actor mismatch: policy is for {self.actor!r}, request is {actor!r}"
            )

        if action == "runtime_read":
            if repo not in self.read_repos:
                reasons.append(f"repo {repo!r} not in policy read list: {self.read_repos}")
        elif action == "runtime_write_own":
            # A repo is writable under runtime_write_own if it is the actor's
            # own_repo OR it carries an explicit write-rule in the policy. The
            # write: list has always been keyed by repo (see _allowed_paths_for_repo),
            # i.e. the schema was already designed for a dept to own more than one
            # write target — a dept whose vault/data lives in a SECOND repo it owns
            # (e.g. ben -> bubble-ben-vault, split out 2026-06-03) must be able to
            # push it. The path allow-list below still fully constrains WHAT it may
            # write in that repo; this only decides WHICH repos are eligible.
            # (#benvault: prior code hard-required repo == own_repo, which silently
            # blocked every vault push — the migration moved the repo but never
            # granted its push-policy. Fail only when the repo is neither.)
            allowed_paths = self._allowed_paths_for_repo(repo)
            if repo != self.own_repo and not allowed_paths:
                reasons.append(
                    f"repo {repo!r} is not the actor's own_repo ({self.own_repo!r}) "
                    f"and has no write rules declared"
                )
            elif not allowed_paths:
                reasons.append(f"no write rules declared for repo {repo!r}")
            for p in paths:
                # #961 (2026-09-03, board incident: commit 5513824 pushed
                # scripts/lib/budget.py directly to bubble-ops-loop/main with
                # no review): MUST be repo-aware here, not just _is_structural().
                # is_structural_for_repo() ORs in FRAMEWORK_STRUCTURAL_PATH_GLOBS
                # (scripts/lib/**, .github/**, token-broker/**, git-guard/**)
                # when `repo` is the framework repo (bubble-ops-loop), while
                # leaving dept repos' legitimate vendored-lib syncs (the SAME
                # scripts/lib/** path in bubble-ops-<slug>) untouched — see the
                # FRAMEWORK_STRUCTURAL_PATH_GLOBS comment above. The function
                # already existed (added #55/ce90bb2) but was never wired into
                # this enforcement path — that's the exact gap #961 closes.
                if is_structural_for_repo(p, repo):
                    reasons.append(
                        f"path {p!r} is structural; use action=settings_pr instead"
                    )
                    continue
                if not any(_glob_match(p, g) for g in allowed_paths):
                    reasons.append(
                        f"path {p!r} not in allowed_paths {allowed_paths}"
                    )
        elif action == "open_priority_pr":
            if repo not in self.can_open_to:
                reasons.append(
                    f"repo {repo!r} not in can_open_to: {self.can_open_to}"
                )
            if not paths:
                reasons.append(
                    "open_priority_pr requires at least one path (target queues/management/**)"
                )
            for p in paths:
                if not any(_glob_match(p, g) for g in self.pr_target_paths):
                    reasons.append(
                        f"path {p!r} not under PR target_paths {self.pr_target_paths} "
                        f"(must be queues/management/**)"
                    )
        elif action == "settings_pr":
            # Settings PRs are always allowed for the actor's own_repo by default;
            # bubble-ops-console additionally has can_open_settings_pr=True for any repo.
            same_own = self.own_repo is not None and repo == self.own_repo
            if not (same_own or self.can_open_settings_pr):
                reasons.append(
                    f"actor cannot open settings_pr against {repo!r}; "
                    f"own_repo={self.own_repo}, can_open_settings_pr={self.can_open_settings_pr}"
                )
            if not paths:
                reasons.append("settings_pr requires at least one path")
            for p in paths:
                if not _is_settings_pr_eligible(p, repo):
                    reasons.append(
                        f"path {p!r} is not structural and not settings-PR-eligible "
                        f"(see SETTINGS_PR_EXTRA_GLOBS); use runtime_write_own instead"
                    )

        return (len(reasons) == 0), reasons

    def _allowed_paths_for_repo(self, repo: str) -> list[str]:
        out: list[str] = []
        for rule in self.write_rules:
            r = rule.get("repo")
            if r == repo or (isinstance(r, str) and fnmatch.fnmatch(repo, r)):
                out.extend(rule.get("allowed_paths") or [])
        return out
