"""Audit log for the git guard — JSONL, metadata only, NEVER secrets.

Mirrors the token-broker's audit pattern (token-broker/src/audit.py):
  - FORBIDDEN_FIELDS dropped pre-serialization
  - Any value starting with `ghs_` raises ValueError
  - JSONL append-only, parent dir auto-created

Schema for a guard event (one JSON line):
  {
    "ts":                  ISO8601 UTC string,
    "actor":               "ops-loop-<dept>",
    "dept":                str,
    "repo":                str,
    "action":              runtime_read | runtime_write_own | open_priority_pr | settings_pr,
    "status":              allowed | denied | would_allow | pushed | push_failed | mint_failed,
    "paths_count":         int,
    "denied_paths":        list[str]   (only when status=denied),
    "reasons":             list[str]   (only when status=denied),
    "token_ttl_minutes":   int         (only when status in {allowed, pushed}),
    "error":               str         (only when status in {push_failed, mint_failed, denied})
  }

Notion v4 §"Secrets" lines 716-724 explicitly list what must NEVER be logged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Union


REQUIRED_FIELDS = (
    "ts",
    "actor",
    "dept",
    "repo",
    "action",
    "status",
    "paths_count",
)

# Defense-in-depth: even if a caller accidentally passes these names, drop them.
FORBIDDEN_FIELDS = frozenset(
    {"token", "access_token", "pem", "private_key", "jwt", "secret"}
)

# Status values the guard knows how to emit. Anything else triggers a ValueError.
KNOWN_STATUSES = frozenset(
    {
        "allowed",        # paths passed policy, not yet pushed
        "denied",         # paths failed policy
        "would_allow",    # dry-run: paths passed, no token minted
        "pushed",         # full success: paths OK + token minted + git push OK
        "push_failed",    # paths OK + token minted, but git push exited non-zero
        "mint_failed",    # paths OK, but broker failed to mint
    }
)


class GuardAudit:
    """Append-only JSONL audit logger for the git guard.

    Default log path: $XDG_STATE_HOME/bubble-git-guard/audit.jsonl
    (falls back to ~/.local/state/bubble-git-guard/audit.jsonl).
    """

    def __init__(self, log_path: Optional[Union[Path, str]] = None) -> None:
        if log_path is None:
            state = os.environ.get(
                "XDG_STATE_HOME", os.path.expanduser("~/.local/state")
            )
            log_path = Path(state) / "bubble-git-guard" / "audit.jsonl"
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, **event: Any) -> None:
        """Append a structured event line. Strips forbidden fields and enforces schema."""
        # 1. Drop anything secret-shaped even if caller passed it
        sanitized = {k: v for k, v in event.items() if k not in FORBIDDEN_FIELDS}

        # 2. Validate required fields
        missing = [f for f in REQUIRED_FIELDS if f not in sanitized]
        if missing:
            raise ValueError(f"audit event missing required fields: {missing}")

        # 3. Validate status enum
        if sanitized["status"] not in KNOWN_STATUSES:
            raise ValueError(
                f"unknown audit status: {sanitized['status']!r}. "
                f"Must be one of: {sorted(KNOWN_STATUSES)}"
            )

        # 4. Defense-in-depth: reject any value that looks like a token leak
        for k, v in sanitized.items():
            if isinstance(v, str) and v.startswith("ghs_"):
                raise ValueError(
                    f"audit event field {k!r} appears to contain a token value "
                    f"(starts with 'ghs_'); refusing to write to audit log"
                )
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.startswith("ghs_"):
                        raise ValueError(
                            f"audit event field {k!r} contains a token-shaped "
                            f"value in its list; refusing to write"
                        )

        # 5. Append as JSONL
        line = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
