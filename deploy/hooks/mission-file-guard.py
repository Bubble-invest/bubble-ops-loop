#!/usr/bin/env python3
"""mission-file-guard.py — PreToolUse hook that STOPS a dept agent from editing
its own mission-definition files, at the moment of action (before the Edit/Write
or git commit even runs).

Governance fix 2026-06-01 (Joris msg 3597/3599). Pairs with the push-time
credential-helper lock ([[mission-file-lock-gap]]). The lock alone only fires at
`git push`, so an agent could still Edit + commit a mission file locally and
falsely report "done" (Maya did exactly this with MANDATE.md). This hook closes
that gap: it DENIES the tool call up front and feeds Claude a reason it can act
on — routing the agent to propose a PR instead of self-editing.

Per the Claude Code hooks doc (PreToolUse):
  - stdin = JSON with tool_name + tool_input (+ cwd).
  - We return permissionDecision "deny" with permissionDecisionReason (shown to
    Claude) when the target is a STRUCTURAL (mission) path.
  - For anything else we exit 0 with NO json -> normal permission flow (the
    agent edits WORKING_MEMORY.md / outputs/ / queues/ untouched).

Structural-path list is SINGLE-SOURCED from the broker policy.py
(STRUCTURAL_PATH_GLOBS) — same list the push-time lock uses, so the two layers
never disagree.

FAIL-OPEN by design: if we cannot parse the input or load the policy, we exit 0
(allow) rather than block legitimate work. The push-time helper remains the hard
enforcement; this hook is the early, visible guidance layer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

# Where the broker policy lives (single source of the structural globs).
_POLICY_CANDIDATES = [
    os.environ.get("BUBBLE_BROKER_POLICY_PY", ""),
    "/opt/bubble-token-broker/src/policy.py",
]


def _load_is_structural():
    for cand in _POLICY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("bubble_broker_policy", str(p))
            mod = importlib.util.module_from_spec(spec)
            sys.modules["bubble_broker_policy"] = mod
            spec.loader.exec_module(mod)
            return mod._is_structural  # noqa: SLF001 — single-sourced glob check
        except Exception:
            continue
    return None


def _rel_to_repo(path_str: str, cwd: str) -> str | None:
    """Best-effort repo-relative path. The structural globs are repo-relative
    (e.g. 'layers/1/PROMPT.md'), so an absolute Edit file_path must be made
    relative to the dept repo root. We anchor on the cwd (the agent runs from
    its repo root) and also try walking up to the nearest .git."""
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        # already relative — assume relative to repo root. Strip a leading
        # "./" prefix only (NOT leading dots — ".claude/..." must survive).
        rel = path_str
        while rel.startswith("./"):
            rel = rel[2:]
        return rel
    # find the git root at/above cwd
    base = Path(cwd) if cwd else Path.cwd()
    root = base
    for cand in [base, *base.parents]:
        if (cand / ".git").exists():
            root = cand
            break
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except Exception:
        # not under the repo root — fall back to the basename-anchored tail
        return str(p)


def _bash_targets(command: str) -> list[str]:
    """Extract file paths a git command would WRITE to a mission file via the
    index/worktree: git add / commit -a / mv / rm of explicit paths. Best-effort
    — we only need to catch the common ways an agent stages a structural file.
    Non-git commands return []."""
    try:
        # handle && / ; chains
        out: list[str] = []
        for seg in re.split(r"&&|\|\||;", command):
            toks = shlex.split(seg.strip())
            if not toks or toks[0] != "git":
                continue
            if len(toks) < 2:
                continue
            sub = toks[1]
            if sub in ("add", "mv", "rm", "stage"):
                # everything after the subcommand that isn't a flag is a path
                out += [t for t in toks[2:] if not t.startswith("-")]
            elif sub == "commit":
                # `git commit -a` / `git commit -am ...` stages all tracked
                # changes — we can't enumerate them here, so flag the whole
                # commit as needing the structural check at the worktree level.
                if any(a.startswith("-a") or a == "--all" or "a" in a.lstrip("-")
                       for a in toks[2:] if a.startswith("-")):
                    out.append("__COMMIT_ALL__")
                # explicit paths after commit (git commit path...)
                out += [t for t in toks[2:] if not t.startswith("-") and "=" not in t]
        return out
    except Exception:
        return []


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


DENY_MSG = (
    "🔒 «{path}» est un fichier de MISSION (spec figée) — tu ne peux pas le "
    "modifier toi-même. Ta mission ne change que via une pull request que Joris "
    "ou Jade valident. Si c'est un sujet TEMPORAIRE, écris-le dans "
    "WORKING_MEMORY.md. Si c'est un vrai changement de mission (même approuvé "
    "par Joris en chat), NE l'applique PAS toi-même : propose-le en PR et "
    "demande à Joris/Jade de la merger. Ne déclare jamais « c'est officiel » "
    "tant que la PR n'est pas mergée."
)
DENY_COMMIT_ALL = (
    "🔒 `git commit -a/--all` peut committer un fichier de MISSION (spec figée). "
    "Stage explicitement seulement tes fichiers runtime (WORKING_MEMORY.md, "
    "outputs/, queues/, inbox/) avec `git add <chemin>` puis commit. Les "
    "fichiers de mission ne changent que via une PR mergée par Joris/Jade."
)


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail-open
    tool = data.get("tool_name", "")
    tin = data.get("tool_input", {}) or {}
    cwd = data.get("cwd", "") or os.getcwd()

    is_structural = _load_is_structural()
    if is_structural is None:
        return 0  # fail-open: can't load policy -> don't block (push-lock still guards)

    targets: list[str] = []
    if tool in ("Edit", "Write", "NotebookEdit"):
        fp = tin.get("file_path") or tin.get("notebook_path") or ""
        rel = _rel_to_repo(fp, cwd)
        if rel:
            targets.append(rel)
    elif tool == "Bash":
        for t in _bash_targets(tin.get("command", "")):
            if t == "__COMMIT_ALL__":
                _deny(DENY_COMMIT_ALL)
            rel = _rel_to_repo(t, cwd)
            if rel:
                targets.append(rel)
    else:
        return 0  # other tools — not our concern

    for t in targets:
        if is_structural(t):
            _deny(DENY_MSG.format(path=t))

    return 0  # nothing structural -> allow (no JSON)


if __name__ == "__main__":
    raise SystemExit(main())
