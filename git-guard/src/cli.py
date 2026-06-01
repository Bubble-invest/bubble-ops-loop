"""bubble-git-guard CLI.

Notion v4 references:
  - line 725: paths enforced by "wrapper local / git guard sur Morty"
  - lines 619-622: the 4 token classes the guard maps to
  - lines 716-724: what must NEVER be logged (token, PEM, etc.)

Usage examples:

  # 1. Runtime push (the loop's normal path)
  bubble-git-guard push \\
      --dept fixture \\
      --action runtime_write_own \\
      --repo bubble-ops-fixture \\
      --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml

  # 2. Offline dry-run (no broker call, no git push, no network) — show plan
  bubble-git-guard push \\
      --dept fixture \\
      --action runtime_write_own \\
      --repo bubble-ops-fixture \\
      --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \\
      --dry-run

  # 3. Tony opens a directive PR to a child dept
  bubble-git-guard push \\
      --dept tony \\
      --action open_priority_pr \\
      --repo bubble-ops-fixture \\
      --policy /opt/bubble-token-broker/deploy/policies/tony-policy.yaml \\
      --remote origin \\
      --ref tony/directive/2026-05-20-buy-aapl

The broker binary is looked up in PATH by default (override with --broker).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .guard import Guard
from .policy_loader import KNOWN_ACTIONS, load_policy


_EXAMPLES = """\
Examples:
  bubble-git-guard push --dept fixture --action runtime_write_own \\
      --repo bubble-ops-fixture --policy /etc/bubble/fixture-policy.yaml
  bubble-git-guard push --dept fixture --action runtime_write_own \\
      --repo bubble-ops-fixture --policy /etc/bubble/fixture-policy.yaml --dry-run
  bubble-git-guard push --dept tony --action open_priority_pr \\
      --repo bubble-ops-fixture --policy /etc/bubble/tony-policy.yaml \\
      --ref tony/directive/buy-aapl

Notion v4 doctrine (line 725): GitHub tokens scope by REPO and PERMISSION only;
PATHS are enforced locally by this guard. The token broker (Step 3b) handles
the first half, this guard handles the path dimension.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bubble-git-guard",
        description=(
            "Path-allow-list enforcement at the git-push boundary on Morty.\n"
            "Wraps `git push` with: staged-path detection → policy check → "
            "broker mint → push. Fails closed if ANY path violates the policy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EXAMPLES,
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    p = sub.add_parser(
        "push",
        help="Guarded `git push`: check paths, mint token via broker, push.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EXAMPLES,
    )
    p.add_argument("--dept", required=True, help="Dept slug (e.g. fixture, maya, tony)")
    p.add_argument(
        "--action",
        required=True,
        choices=sorted(KNOWN_ACTIONS),
        help="Token class (Notion v4 §Classes de tokens éphémères)",
    )
    p.add_argument("--repo", required=True, help="Target repo (e.g. bubble-ops-fixture)")
    p.add_argument(
        "--repo-dir",
        default=".",
        help="Path to the local git working tree (default: cwd)",
    )
    p.add_argument(
        "--policy", required=True, help="Path to actor policy YAML (same shape as broker)"
    )
    p.add_argument("--audit-log", default=None, help="Override audit JSONL path")
    p.add_argument(
        "--broker",
        default="bubble-token-broker",
        help="Broker binary (looked up in PATH by default; pass absolute path on Morty)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan; do NOT mint, do NOT push, do NOT touch the network.",
    )
    p.add_argument("--remote", default="origin", help="git push remote (default: origin)")
    p.add_argument("--ref", default="HEAD", help="git push refspec (default: HEAD)")

    return parser


def _cmd_push(args: argparse.Namespace) -> int:
    # Resolve policy first — fail-closed if missing or malformed.
    try:
        policy = load_policy(args.policy)
    except FileNotFoundError as exc:
        print(f"ERROR: policy file missing: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — YAML parse + other malformations
        print(f"ERROR: failed to load policy {args.policy!r}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    audit_path = Path(args.audit_log) if args.audit_log else None
    guard = Guard(
        policy=policy,
        broker_cmd=[args.broker],
        audit_log_path=audit_path,
    )
    return guard.push(
        repo_dir=Path(args.repo_dir),
        dept=args.dept,
        action=args.action,
        repo=args.repo,
        dry_run=args.dry_run,
        remote=args.remote,
        ref=args.ref,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "push":
        return _cmd_push(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
