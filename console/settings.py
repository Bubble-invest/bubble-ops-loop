"""Console settings — paths + env-var resolution."""
from __future__ import annotations

import os
from pathlib import Path

# ---- Project paths ---------------------------------------------------

CONSOLE_DIR = Path(__file__).parent
TEMPLATES_DIR = CONSOLE_DIR / "templates"
STATIC_DIR = CONSOLE_DIR / "static"
PROJECT_ROOT = CONSOLE_DIR.parent  # bubble-ops-loop/
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
BOOTSTRAP_SCRIPT = SCRIPTS_DIR / "bootstrap-dept.sh"

# loop-backup event log — the safety-net timer (loop-backup.sh) appends one
# JSON line per dept per fire here; the cockpit reads it back to surface the
# result in the front end (Joris msg 1171). Keep the default in sync with
# loop-backup.sh's BUBBLE_BACKUP_LOG.
BACKUP_LOG_PATH = Path(
    os.environ.get("BUBBLE_BACKUP_LOG", str(PROJECT_ROOT / "state" / "loop-backup.jsonl"))
)

# ---- Env-var driven knobs --------------------------------------------

# Bearer token — operator MUST set this in prod. Tests inject via conftest.
BEARER_TOKEN = os.environ.get("CONSOLE_BEARER_TOKEN", "")

# Read mode: if READ_FROM_DISK is set, the services read repo content from
# this directory (each subdir = a bubble-ops-<slug> repo). Otherwise they
# call `gh api` to read from github.
READ_FROM_DISK = os.environ.get("READ_FROM_DISK", "")

# GitHub org that hosts bubble-ops-<slug> repos.
GITHUB_ORG = os.environ.get("BUBBLE_OPS_GITHUB_ORG", "vdk888")

# Cache TTL for `gh api` calls (seconds).
GH_CACHE_TTL_SECONDS = int(os.environ.get("GH_CACHE_TTL", "60"))

# Where to bind in prod. Operator sets this; default is 127.0.0.1:8642
# (Notion v5 line 1011 — Tailscale-only, not clearnet).
BIND_HOST = os.environ.get("CONSOLE_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("CONSOLE_BIND_PORT", "8642"))


def disk_mode() -> bool:
    """True iff READ_FROM_DISK is set (test + local-dev mode)."""
    return bool(READ_FROM_DISK)


def disk_root() -> Path:
    """Return Path(READ_FROM_DISK). Caller must check disk_mode() first."""
    return Path(READ_FROM_DISK)
