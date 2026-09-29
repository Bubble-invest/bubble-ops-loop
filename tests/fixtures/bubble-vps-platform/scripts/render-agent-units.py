#!/usr/bin/env python3
"""Minimal canonical-renderer contract fixture for deploy-wrapper tests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _yaml_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([^#\n]+)", text, re.MULTILINE)
    return match.group(1).strip().strip("'\"") if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, type=Path)
    parser.add_argument("--dept", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--os-user")
    parser.add_argument("--os-group")
    parser.add_argument("--workdir")
    parser.add_argument("--model")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    dept = args.dept.read_text(encoding="utf-8")
    slug = _yaml_scalar(dept, "slug")
    if not slug:
        parser.error("department.slug is required")
    os_user = args.os_user if args.os_user is not None else (_yaml_scalar(dept, "os_user") or "claude")
    if not os_user or os_user == "root":
        parser.error("--os-user must name a non-root account")
    os_group = args.os_group or os_user
    workdir = args.workdir or (
        f"/home/claude/agents/{slug}" if os_user == "claude" else f"/srv/agents/{slug}"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "bubble-agent@.service").write_text(
        "[Unit]\nDescription=fixture\n[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    prepare = args.output_dir / "bubble-agent-prepare"
    prepare.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    prepare.chmod(0o755)
    dropin = args.output_dir / f"bubble-agent@{slug}.service.d" / f"{slug}.conf"
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(
        f"[Service]\nUser={os_user}\nGroup={os_group}\nWorkingDirectory={workdir}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
