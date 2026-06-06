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
    "assets/**",          # dept doctrine/voice assets (e.g. Maya's maya-doctrine.md) — mission-like, PR-gated ({{OPERATOR}} 2026-06-06)
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
    """True if the path is structural (settings_pr territory)."""
    return any(_glob_match(path, g) for g in STRUCTURAL_PATH_GLOBS)


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
            if repo != self.own_repo:
                reasons.append(
                    f"repo {repo!r} is not the actor's own_repo ({self.own_repo!r})"
                )
            allowed_paths = self._allowed_paths_for_repo(repo)
            if not allowed_paths:
                reasons.append(f"no write rules declared for repo {repo!r}")
            for p in paths:
                if _is_structural(p):
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
                if not _is_structural(p):
                    reasons.append(
                        f"path {p!r} is not structural; use runtime_write_own instead"
                    )

        return (len(reasons) == 0), reasons

    def _allowed_paths_for_repo(self, repo: str) -> list[str]:
        out: list[str] = []
        for rule in self.write_rules:
            r = rule.get("repo")
            if r == repo or (isinstance(r, str) and fnmatch.fnmatch(repo, r)):
                out.extend(rule.get("allowed_paths") or [])
        return out
