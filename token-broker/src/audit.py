"""Audit log for the token broker — metadata only, never secrets.

Notion v4 §"Token broker Morty" (lines 603-614) defines the canonical audit
schema. The token VALUE and the PEM bytes never appear here — only metadata
about WHO requested WHAT for WHICH repo, with what permissions, and the
outcome.

Schema (one JSON line per event):
  {
    "ts":                  ISO8601 UTC string,
    "dept":                str,
    "repo":                str,
    "action":              str   (runtime_read | runtime_write_own | open_priority_pr | settings_pr | ...),
    "permissions":         list[str]   (e.g. ["contents:write", "pull_requests:write"]),
    "actor":               str   (e.g. "ops-loop-fixture"),
    "token_ttl_minutes":   int,
    "status":              str   ("issued" | "failed" | "denied"),
    "error":               str   (only when status != "issued")
  }

Output channels (controlled by `journal` constructor arg):
  - False (default): file-only JSONL, backward-compatible
  - True:            file AND journald (dual, for cross-service correlation)
  - "only":          journald only (ephemeral hosts, no persistent file)

Per {{OPERATOR}}'s Step 3b Q3 review: opt-in flag, default file-only. Activated
when Layer 4 mandate-guardian needs `journalctl SYSLOG_IDENTIFIER=bubble-token-broker`
filtering across systemd services on Morty.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Union


REQUIRED_FIELDS = (
    "ts",
    "dept",
    "repo",
    "action",
    "permissions",
    "actor",
    "token_ttl_minutes",
    "status",
)

# Defense-in-depth: even if a caller accidentally passes these names, drop them.
FORBIDDEN_FIELDS = frozenset({"token", "access_token", "pem", "private_key", "jwt", "secret"})

# journald identifier — appears in `journalctl SYSLOG_IDENTIFIER=...` filter
SYSLOG_IDENTIFIER = "bubble-token-broker"

JournalMode = Union[bool, Literal["only"]]


class Audit:
    """Append-only JSONL audit logger with optional journald emission.

    Default log path: $XDG_STATE_HOME/bubble-token-broker/audit.jsonl
    (falls back to ~/.local/state/bubble-token-broker/audit.jsonl).

    Args:
        log_path: file destination for JSONL audit. Ignored if journal="only".
        journal: False (file-only, default), True (file+journald), "only" (journald only).
            When True or "only", `systemd.journal` lib must be importable at __init__
            time — fail-loud ImportError otherwise (so a misconfigured deployment is
            obvious immediately, not silent).
    """

    def __init__(
        self,
        log_path: Path | str | None = None,
        journal: JournalMode = False,
    ) -> None:
        # Validate journal mode at __init__ time (fail-loud per design)
        if journal not in (False, True, "only"):
            raise ValueError(f"journal must be False, True, or 'only'; got {journal!r}")
        self.journal_mode: JournalMode = journal
        self._journal_send = None

        if journal is not False:
            # Import systemd.journal at init time so misconfiguration fails loud
            try:
                from systemd import journal as _journal  # type: ignore[import-not-found]

                self._journal_send = _journal.send
            except ImportError as e:
                raise ImportError(
                    "systemd.journal is required when journal=True or 'only'. "
                    "Install via `apt-get install python3-systemd` on Debian/Ubuntu, "
                    "or `pip install systemd-python`. Original: " + str(e)
                ) from e

        # File path setup (skipped if journal-only, but keep for backward-compat
        # if caller later flips mode)
        if log_path is None:
            state = os.environ.get(
                "XDG_STATE_HOME", os.path.expanduser("~/.local/state")
            )
            log_path = Path(state) / "bubble-token-broker" / "audit.jsonl"
        self.log_path = Path(log_path)
        # Only create the directory if we'll actually write a file
        if self.journal_mode != "only":
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, **event: Any) -> None:
        """Append a structured event line. Strips forbidden fields and enforces schema.

        Routes per journal_mode:
          - False:  file only
          - True:   file AND journald
          - "only": journald only

        journald failures (OSError, e.g. socket unreachable) degrade gracefully:
        the file write still happens (when journal=True), so the durable audit
        channel survives a missing journald daemon.
        """
        # Drop anything secret-shaped even if caller passed it
        sanitized = {k: v for k, v in event.items() if k not in FORBIDDEN_FIELDS}
        # Validate required fields are present
        missing = [f for f in REQUIRED_FIELDS if f not in sanitized]
        if missing:
            raise ValueError(f"audit event missing required fields: {missing}")
        # Reject any value that looks like a GitHub installation token leak
        for k, v in sanitized.items():
            if isinstance(v, str) and v.startswith("ghs_"):
                raise ValueError(
                    f"audit event field {k!r} appears to contain a token value (starts with 'ghs_')"
                )

        # File write (unless journal-only)
        if self.journal_mode != "only":
            line = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        # journald write (when enabled). Degrade gracefully on OSError.
        if self._journal_send is not None:
            try:
                # journald convention: MESSAGE = human-readable summary,
                # remaining kwargs = structured fields (UPPER_CASE)
                message = (
                    f"{sanitized.get('action', '?')} "
                    f"dept={sanitized.get('dept', '?')} "
                    f"repo={sanitized.get('repo', '?')} "
                    f"status={sanitized.get('status', '?')}"
                )
                # Build structured kwargs: every field becomes UPPER_CASE
                # journald field. Lists serialize to JSON, ints/strs as-is.
                structured: dict[str, str] = {"SYSLOG_IDENTIFIER": SYSLOG_IDENTIFIER}
                for k, v in sanitized.items():
                    key = k.upper()
                    if isinstance(v, (list, dict)):
                        structured[key] = json.dumps(v, separators=(",", ":"))
                    else:
                        structured[key] = str(v)
                self._journal_send(MESSAGE=message, **structured)
            except OSError:
                # journald socket unreachable, daemon down, etc.
                # File write already happened (when journal=True) — that's the
                # durable channel. Silently degrade.
                pass
