"""Console settings — paths + env-var resolution."""
from __future__ import annotations

import json
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
# result in the front end ({{OPERATOR}} msg 1171). Keep the default in sync with
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

# GitHub org that hosts bubble-ops-<slug> repos. The live dept repos are under
# Bubble-invest (e.g. Bubble-invest/bubble-ops-content); the old "vdk888" default
# meant host=local decision PUTs targeted the wrong org (404). Override with
# BUBBLE_OPS_GITHUB_ORG if a deployment hosts the repos elsewhere.
GITHUB_ORG = os.environ.get("BUBBLE_OPS_GITHUB_ORG", "Bubble-invest")

# Cache TTL for `gh api` calls (seconds).
GH_CACHE_TTL_SECONDS = int(os.environ.get("GH_CACHE_TTL", "60"))

# Weekly budget envelope (USD, real-equivalent non-cache cost). The DENOMINATOR for
# "% of weekly budget" on cards/depts/projects (board #358). Anchored at ~last week's
# actual real-work spend; tweak via env as it drifts from real usage. A card budgeted
# at $45 ≈ 1% of the week. NOT Anthropic's subscription % (unreadable) — our own envelope.
WEEKLY_BUDGET_USD = float(os.environ.get("BUBBLE_WEEKLY_BUDGET_USD", "4500"))

# Where to bind in prod. Operator sets this; default is 127.0.0.1:8642
# (Notion v5 line 1011 — Tailscale-only, not clearnet).
BIND_HOST = os.environ.get("CONSOLE_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("CONSOLE_BIND_PORT", "8642"))

# Per-agent WEEKLY OPERATING ENVELOPE (USD, real-equivalent non-cache cost).
# The DENOMINATOR for /costs' per-agent "Enveloppe (hebdo)" column (board
# #524d follow-up — Joris's "Option B" fix, 2026-07-05). This is deliberately
# NOT the same thing as a mission's `budget_usd` in dept.yaml: the /costs
# numerator is an agent's WHOLE 7-day session spend (interactive + operating +
# dev), not just its recurring missions, so the right denominator is a
# per-agent-session weekly envelope, not a Σ of one daily mission cycle's
# budget. Keyed by the exact agent key the cost report uses (e.g.
# "tony (local)" — includes the disambiguation suffix, unlike the dept-budget
# rollup which strips it). An agent key NOT in this map has no envelope
# (defined=False, renders "—") — same graceful degradation as the old
# mission-budget lookup.
#
# Defaults are Rick's proposal from measured actuals + ~30-50% headroom —
# tune via OPERATING_ENVELOPE_JSON (a JSON object merged OVER these defaults)
# without a code change, e.g.:
#   OPERATING_ENVELOPE_JSON='{"tony (local)": 200, "newagent": 50}'
_OPERATING_ENVELOPE_WEEKLY_USD_DEFAULTS: dict[str, float] = {
    "rick": 800,
    "miranda (jade-mac)": 200,
    "claudette": 150,
    "tony": 150,
    "tony (local)": 150,
    "maya": 130,
    "accountant": 100,
    "ben": 90,
    "morty": 60,
    "ellie": 30,
    "eliot (mac-legacy)": 20,
    "maya (mac-legacy)": 20,
}


def _load_operating_envelope() -> dict[str, float]:
    """Defaults merged with an optional OPERATING_ENVELOPE_JSON override.

    The env var, if set, must decode to a JSON object of {agent_key: USD};
    anything else (bad JSON, non-dict, non-numeric values) is ignored for
    that key (or the whole override) so a malformed env var degrades to the
    defaults rather than crashing the console on boot.
    """
    envelope = dict(_OPERATING_ENVELOPE_WEEKLY_USD_DEFAULTS)
    raw = os.environ.get("OPERATING_ENVELOPE_JSON", "")
    if not raw:
        return envelope
    try:
        override = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return envelope
    if not isinstance(override, dict):
        return envelope
    for key, val in override.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            envelope[str(key)] = float(val)
    return envelope


OPERATING_ENVELOPE_WEEKLY_USD: dict[str, float] = _load_operating_envelope()


# Per-DEPT-SLUG WEEKLY OPERATING ENVELOPE (USD, real-equivalent non-cache cost).
# The DENOMINATOR for the HOME page's "Coûts — budget par collègue" section
# (board #550 — the SAME apples-to-oranges bug as #524d/#546, a DIFFERENT code
# path). home.py's `_dept_budgets` compares a dept's WEEK total spend against
# this envelope — it must NOT use cost_tracker.mission_budget_total (Σ
# budget_usd over ONE daily mission cycle in dept.yaml), because that produced
# nonsense like tony $185.92 spent / $8 mission budget = 2324%.
#
# Keyed by dept SLUG (not agent-key — home is per-dept, unlike /costs which is
# per-agent-session). A dept slug NOT in this map has no envelope (defined=
# False, renders "budget non défini") — same graceful degradation as before.
#
# Defaults are Rick's proposal from measured home-page actuals + ~30% headroom
# — tune via OPERATING_ENVELOPE_BY_DEPT_JSON (a JSON object merged OVER these
# defaults) without a code change, e.g.:
#   OPERATING_ENVELOPE_BY_DEPT_JSON='{"tony": 250, "newdept": 50}'
_OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT_DEFAULTS: dict[str, float] = {
    "tony": 220,
    "content": 200,
    "maya": 100,
    "ben": 100,
    "accountant": 110,
    "security": 40,
    "cgp": 60,
}


def _load_operating_envelope_by_dept() -> dict[str, float]:
    """Defaults merged with an optional OPERATING_ENVELOPE_BY_DEPT_JSON override.

    Mirrors `_load_operating_envelope` exactly: the env var, if set, must
    decode to a JSON object of {dept_slug: USD}; anything else (bad JSON,
    non-dict, non-numeric values) is ignored for that key (or the whole
    override) so a malformed env var degrades to the defaults rather than
    crashing the console on boot.
    """
    envelope = dict(_OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT_DEFAULTS)
    raw = os.environ.get("OPERATING_ENVELOPE_BY_DEPT_JSON", "")
    if not raw:
        return envelope
    try:
        override = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return envelope
    if not isinstance(override, dict):
        return envelope
    for key, val in override.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            envelope[str(key)] = float(val)
    return envelope


OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT: dict[str, float] = _load_operating_envelope_by_dept()


def disk_mode() -> bool:
    """True iff READ_FROM_DISK is set (test + local-dev mode)."""
    return bool(READ_FROM_DISK)


def disk_root() -> Path:
    """Return Path(READ_FROM_DISK). Caller must check disk_mode() first."""
    return Path(READ_FROM_DISK)
