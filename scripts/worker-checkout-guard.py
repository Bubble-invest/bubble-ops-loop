#!/usr/bin/env python3
"""Refuse worker targets in managed live checkouts (board #1306).

Read-only preflight for dispatchers; not an OS sandbox or a Git hook.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

MANAGED_ROOTS = (
    Path("/home/claude/bubble-ops-loop"),
    Path("/opt/bubble-ops-loop"),
    Path("/srv/agents"),
)


def is_managed_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return any(root.resolve() == resolved or root.resolve() in resolved.parents
               for root in MANAGED_ROOTS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--scratch-base", action="store_true",
                        help="also refuse scratch inside any existing Git checkout")
    args = parser.parse_args()
    try:
        blocked = is_managed_checkout(args.path)
        if args.scratch_base and not blocked:
            # A scratch clone nested in a shared checkout is still in its live
            # tree. Never write a task brief there, even through a symlink.
            result = subprocess.run(
                ["git", "-C", str(args.path.resolve()), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=False,
            )
            blocked = result.returncode == 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: cannot verify worker checkout: {exc}", file=sys.stderr)
        return 2
    if blocked:
        print("ERROR: worker target is a live/shared checkout. Use an isolated "
              "git worktree under /home/claude/worktrees/issue-N or a private "
              "scratch clone outside managed checkouts.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
