"""bubble-token-broker CLI — mint or check, never persists secrets.

Notion v4 references:
  - §"Token broker Morty" (lines 592-614) — broker flow.
  - §"Classes de tokens éphémères" (lines 616-622) — action classes.
  - §"Secrets" (lines 702-724) — what is allowed vs forbidden to store.

Usage examples:

  # 1. Mint a runtime_write_own token for the bubble-ops-fixture dept
  #    (uses SOPS-encrypted PEM at $GITHUB_APP_PRIVATE_KEY_PATH)
  bubble-token-broker mint \\
      --dept fixture --action runtime_write_own --repo bubble-ops-fixture

  # 2. Mint a read-only token (offline test — no GitHub API call)
  bubble-token-broker mint \\
      --dept fixture --action runtime_read --repo bubble-ops-fixture \\
      --mock-github

  # 3. Policy-only dry-run (no PEM needed, no API call, no token leaves)
  bubble-token-broker check \\
      --dept fixture --action runtime_write_own --repo bubble-ops-fixture \\
      --paths outputs/2026-05-20/1/summary.md
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .audit import Audit
from .broker import MAX_TTL_MINUTES, PERMISSION_CLASSES, Broker, Token
from .policy import Policy


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bubble-token-broker",
        description=(
            "GitHub App installation-token broker for Morty (Notion v4 doctrine).\n\n"
            "Mints short-lived (≤60 min) installation tokens from a GitHub App "
            "private key, in-memory only, NEVER persisted to disk. Audit logs "
            "metadata only (no token, no PEM)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bubble-token-broker mint --dept fixture --action runtime_write_own "
            "--repo bubble-ops-fixture\n"
            "  bubble-token-broker mint --dept fixture --action runtime_read "
            "--repo bubble-ops-fixture --mock-github\n"
            "  bubble-token-broker check --dept fixture --action runtime_write_own "
            "--repo bubble-ops-fixture --paths outputs/2026-05-20/1/summary.md\n"
            "\n"
            "Env vars used by default:\n"
            "  GITHUB_APP_ID                          (e.g. 3782718)\n"
            "  GITHUB_APP_INSTALLATION_ID_<DEPT>      (e.g. _BUBBLE_OPS_FIXTURE=134075326)\n"
            "  GITHUB_APP_PRIVATE_KEY_PATH            (SOPS-encrypted PEM path on Morty)\n"
            "  BUBBLE_TOKEN_BROKER_POLICY             (path to actor policy YAML)\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    # ---- mint ----
    m = sub.add_parser(
        "mint",
        help="Mint an installation token for a dept+action+repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m.add_argument("--dept", required=True, help="Dept slug, e.g. fixture, maya, tony")
    m.add_argument(
        "--action",
        required=True,
        choices=sorted(PERMISSION_CLASSES.keys()),
        help="Token class (Notion v4 §Classes de tokens éphémères)",
    )
    m.add_argument("--repo", required=True, help="Target repo (e.g. bubble-ops-fixture)")
    m.add_argument(
        "--paths",
        nargs="+",
        default=[],
        help="Paths the caller plans to write (policy check; runtime_write_own only)",
    )
    m.add_argument("--app-id", type=int, default=None, help="Override GITHUB_APP_ID env")
    m.add_argument(
        "--installation-id",
        type=int,
        default=None,
        help="Override GITHUB_APP_INSTALLATION_ID_<DEPT> env",
    )
    m.add_argument(
        "--pem-path",
        default=None,
        help="Path to SOPS-encrypted GitHub App private key (default: $GITHUB_APP_PRIVATE_KEY_PATH)",
    )
    m.add_argument(
        "--no-sops",
        action="store_true",
        help="Treat --pem-path as already-plaintext (test/dev only; production always uses SOPS)",
    )
    m.add_argument("--policy", default=None, help="Path to actor policy YAML")
    m.add_argument("--audit-log", default=None, help="Override audit log path")
    m.add_argument(
        "--journal",
        choices=["off", "on", "only"],
        default="off",
        help=(
            "journald audit emission. off=file-only (default), "
            "on=file+journald, only=journald only (no file). "
            "Requires python3-systemd installed when on/only."
        ),
    )
    m.add_argument(
        "--mock-github",
        action="store_true",
        help="Skip the real GitHub API call (offline test); emits a fake ghs_ token",
    )

    # ---- check ----
    c = sub.add_parser(
        "check",
        help="Dry-run policy check, no API call, no mint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("--dept", required=True)
    c.add_argument("--action", required=True, choices=sorted(PERMISSION_CLASSES.keys()))
    c.add_argument("--repo", required=True)
    c.add_argument("--paths", nargs="+", default=[])
    c.add_argument("--policy", default=None)

    return parser


# --- PEM sourcing ---------------------------------------------------------


def _make_pem_provider(pem_path: str, use_sops: bool):
    """Return a callable[[], bytes] that yields PEM bytes in memory only.

    When `use_sops` is True (production default), shells out to:
        sops --decrypt --input-type binary --output-type binary <pem_path>
    and returns stdout bytes. The SOPS_AGE_KEY_FILE env var must be set
    (Morty defaults to /etc/age/key.txt). No temp file is created.

    When `use_sops` is False (test/dev), reads the file directly. Production
    code paths should never see `use_sops=False`.
    """
    if not use_sops:
        # Direct read — used by tests with an in-memory-generated PEM.
        return lambda p=pem_path: Path(p).read_bytes()

    def provider() -> bytes:
        proc = subprocess.run(
            [
                "sops",
                "--decrypt",
                "--input-type",
                "binary",
                "--output-type",
                "binary",
                pem_path,
            ],
            capture_output=True,
            check=True,
        )
        return proc.stdout

    return provider


def _resolve_installation_id(args, dept: str) -> int:
    if args.installation_id is not None:
        return int(args.installation_id)
    env_name = f"GITHUB_APP_INSTALLATION_ID_{dept.upper().replace('-', '_')}"
    val = os.environ.get(env_name)
    if val:
        return int(val)
    # Fallback: bubble-ops-fixture is the v1 target, so accept this canonical name.
    val = os.environ.get("GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE")
    if val:
        return int(val)
    raise SystemExit(
        f"Could not resolve installation ID. Set {env_name} or pass --installation-id."
    )


def _resolve_app_id(args) -> int:
    if args.app_id is not None:
        return int(args.app_id)
    val = os.environ.get("GITHUB_APP_ID")
    if not val:
        raise SystemExit("Missing GITHUB_APP_ID env or --app-id.")
    return int(val)


def _resolve_pem_path(args) -> str:
    if args.pem_path is not None:
        return args.pem_path
    val = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if not val:
        raise SystemExit("Missing GITHUB_APP_PRIVATE_KEY_PATH env or --pem-path.")
    return val


def _resolve_policy_path(args) -> str | None:
    if args.policy is not None:
        return args.policy
    return os.environ.get("BUBBLE_TOKEN_BROKER_POLICY")


# --- Mock-GitHub support --------------------------------------------------


def _fake_token(action: str, repo: str) -> Token:
    """Build a Token object without hitting the real GitHub API."""
    rand = secrets.token_hex(20)
    return Token(
        value=f"ghs_MOCK{rand}",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=MAX_TTL_MINUTES),
        permissions=dict(PERMISSION_CLASSES[action]),
        repositories=[repo] if repo else [],
        repository_selection="selected",
    )


# --- Command handlers -----------------------------------------------------


def _cmd_mint(args) -> int:
    # Translate CLI --journal {off,on,only} to Audit's journal {False, True, "only"}
    journal_arg = getattr(args, "journal", "off")
    journal_mode = {"off": False, "on": True, "only": "only"}[journal_arg]
    audit = Audit(
        log_path=Path(args.audit_log) if args.audit_log else None,
        journal=journal_mode,
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    actor = f"ops-loop-{args.dept}"

    # Policy check (if a policy file was provided / discoverable)
    policy_path = _resolve_policy_path(args)
    if policy_path:
        policy = Policy.from_yaml(policy_path)
        allowed, reasons = policy.enforce(
            actor=actor, repo=args.repo, action=args.action, paths=list(args.paths)
        )
        if not allowed:
            audit.log(
                ts=ts,
                dept=args.dept,
                repo=args.repo,
                action=args.action,
                permissions=_perm_list(args.action),
                actor=actor,
                token_ttl_minutes=MAX_TTL_MINUTES,
                status="failed",
                error=f"policy_denied: {'; '.join(reasons)}",
            )
            print(f"DENIED: {'; '.join(reasons)}", file=sys.stderr)
            return 2

    # Mint
    if args.mock_github:
        token = _fake_token(args.action, args.repo)
    else:
        app_id = _resolve_app_id(args)
        installation_id = _resolve_installation_id(args, args.dept)
        pem_path = _resolve_pem_path(args)
        pem_provider = _make_pem_provider(pem_path, use_sops=not args.no_sops)
        broker = Broker(
            app_id=app_id, installation_id=installation_id, pem_provider=pem_provider
        )
        try:
            token = broker.mint(dept=args.dept, action=args.action, repo=args.repo)
        except Exception as exc:  # noqa: BLE001
            audit.log(
                ts=ts,
                dept=args.dept,
                repo=args.repo,
                action=args.action,
                permissions=_perm_list(args.action),
                actor=actor,
                token_ttl_minutes=MAX_TTL_MINUTES,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3

    audit.log(
        ts=ts,
        dept=args.dept,
        repo=args.repo,
        action=args.action,
        permissions=_perm_list(args.action),
        actor=actor,
        token_ttl_minutes=MAX_TTL_MINUTES,
        status="issued",
    )
    # Token to stdout — single line, no newline-after-token-then-newline pattern.
    sys.stdout.write(token.value)
    return 0


def _cmd_check(args) -> int:
    policy_path = _resolve_policy_path(args)
    if not policy_path:
        print("ERROR: no policy file provided (--policy or BUBBLE_TOKEN_BROKER_POLICY)", file=sys.stderr)
        return 1
    policy = Policy.from_yaml(policy_path)
    actor = f"ops-loop-{args.dept}"
    allowed, reasons = policy.enforce(
        actor=actor, repo=args.repo, action=args.action, paths=list(args.paths)
    )
    if allowed:
        print(f"ALLOWED: actor={actor} repo={args.repo} action={args.action}")
        return 0
    print(f"DENIED: {'; '.join(reasons)}")
    return 2


def _perm_list(action: str) -> list[str]:
    return [f"{k}:{v}" for k, v in PERMISSION_CLASSES[action].items()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "mint":
        return _cmd_mint(args)
    if args.cmd == "check":
        return _cmd_check(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
