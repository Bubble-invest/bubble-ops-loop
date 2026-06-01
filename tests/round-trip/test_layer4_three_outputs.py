"""
test_layer4_three_outputs.py — Step 9 validation: Layer 4 produces all
three mandatory outputs per Notion v4 §"Layer 4 — Risk Control"
(line 465 in /tmp/notion_final.txt).

Strict RED → GREEN TDD discipline. Before the manual trigger (or the
22:00 UTC daily fire), every assertion in this file should fail because
no Layer 4 output exists yet on the fixture repo. After the fire, every
assertion should pass.

Notion v4 line 465 (verbatim, ASCII transliteration):
    Layer 4 - Risk Control. Subagent mandate-guardian. Writes:
      outputs/<date>/4/risk-kpis.yaml
      outputs/<date>/4/risk-brief.md
      outputs/<date>/management-export.yaml      (note: NOT in 4/)
      + items in queues/improvements/

Plus the standard 4-file output schema for Layer 4 itself, per Notion v4
§"Output layer" (replicated in every layer's PROMPT.md):
      outputs/<date>/4/summary.md
      outputs/<date>/4/artifacts/.gitkeep
      outputs/<date>/4/logs.jsonl
      outputs/<date>/4/.last-run

The verifier looks at:
  1. The local clone at /tmp/bubble-ops-fixture (after `git pull`), OR
  2. The GitHub repo vdk888/bubble-ops-fixture via `gh api` if the local
     clone is stale.

To run:
    cd /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
    pytest tests/round-trip/test_layer4_three_outputs.py -v --tb=short

Override the date under test (default = today UTC) with:
    BUBBLE_OPS_LAYER4_DATE=2026-05-20 pytest tests/round-trip/...

Override the fixture root with:
    BUBBLE_OPS_FIXTURE_ROOT=/path/to/fixture pytest tests/round-trip/...
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft7Validator

# ---------------------------------------------------------------------------
# Configuration (env-overridable for CI / replay)
# ---------------------------------------------------------------------------

DEFAULT_FIXTURE_ROOT = Path("/tmp/bubble-ops-fixture")
FIXTURE_ROOT = Path(os.environ.get("BUBBLE_OPS_FIXTURE_ROOT", str(DEFAULT_FIXTURE_ROOT)))

# Schema lives in this repo (the contract authority).
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas-draft"
MGMT_EXPORT_SCHEMA = SCHEMAS_DIR / "management-export.schema.yaml"

# Date under test (UTC). Notion v4 says Layer 4 fires daily at 22:00 UTC,
# so the date the agent writes under is always the UTC date of the tick.
RUN_DATE = os.environ.get(
    "BUBBLE_OPS_LAYER4_DATE",
    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_git_pull(repo: Path) -> None:
    """Best-effort `git pull` on the local clone. Silent on failure;
    we still fall back to gh api for GitHub-truth assertions below."""
    if not (repo / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "-C", str(repo), "pull", "--quiet"],
            check=False,
            timeout=30,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _gh_path_exists(remote_path: str) -> bool:
    """Check if a path exists in vdk888/bubble-ops-fixture via gh api.

    Returns False on any error (no `gh` installed, no network, 404, ...).
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/vdk888/bubble-ops-fixture/contents/{remote_path}",
                "--silent",
            ],
            check=False,
            timeout=20,
            capture_output=True,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return False


def _file_exists_local_or_remote(rel_path: str) -> bool:
    """File exists either in the local clone OR on GitHub.

    The fixture agent commits + pushes after each Layer 4 run, so GitHub
    is the source of truth. Local clone is checked first for speed.
    """
    local = FIXTURE_ROOT / rel_path
    if local.is_file():
        return True
    return _gh_path_exists(rel_path)


def _read_file_local_or_remote(rel_path: str) -> str:
    """Read a file's contents from local clone or GitHub. Raises if both
    miss — call _file_exists_local_or_remote first to short-circuit."""
    local = FIXTURE_ROOT / rel_path
    if local.is_file():
        return local.read_text(encoding="utf-8")
    # Fall back to gh api raw download.
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/vdk888/bubble-ops-fixture/contents/{rel_path}",
            "--jq",
            ".content",
        ],
        check=True,
        timeout=20,
        capture_output=True,
        text=True,
    )
    import base64

    return base64.b64decode(result.stdout.strip()).decode("utf-8")


def _load_schema() -> dict[str, Any]:
    """Load the management-export schema (YAML format, JSON-Schema content)."""
    with MGMT_EXPORT_SCHEMA.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _refresh_local_clone() -> None:
    """Pull the local clone once per module so subsequent assertions
    see fresh state if Joris triggered Layer 4 between test runs."""
    _try_git_pull(FIXTURE_ROOT)


@pytest.fixture(scope="module")
def mgmt_export_schema() -> dict[str, Any]:
    return _load_schema()


# ---------------------------------------------------------------------------
# Path constants (per Notion v4 line 465)
# ---------------------------------------------------------------------------

LAYER4_DIR = f"outputs/{RUN_DATE}/4"
RISK_BRIEF = f"{LAYER4_DIR}/risk-brief.md"
RISK_KPIS = f"{LAYER4_DIR}/risk-kpis.yaml"
MGMT_EXPORT = f"outputs/{RUN_DATE}/management-export.yaml"  # NOT in 4/
SUMMARY_MD = f"{LAYER4_DIR}/summary.md"
ARTIFACTS_DIR_GITKEEP = f"{LAYER4_DIR}/artifacts/.gitkeep"
LOGS_JSONL = f"{LAYER4_DIR}/logs.jsonl"
LAST_RUN = f"{LAYER4_DIR}/.last-run"


# ---------------------------------------------------------------------------
# Assertions — the 3 mandatory hierarchy outputs
# ---------------------------------------------------------------------------


def test_risk_brief_md_exists() -> None:
    """Notion v4 L465: Layer 4 writes outputs/<date>/4/risk-brief.md."""
    assert _file_exists_local_or_remote(RISK_BRIEF), (
        f"Mandatory output missing: {RISK_BRIEF} "
        f"(neither in local clone {FIXTURE_ROOT} nor on github vdk888/bubble-ops-fixture). "
        f"Layer 4 has not produced its qualitative narrative yet. "
        f"If today's tick (22:00 UTC) has not fired, this is EXPECTED RED; "
        f"send the Step-9 trigger message to @bubtiktikbot to force a run."
    )


def test_risk_kpis_yaml_exists() -> None:
    """Notion v4 L465: Layer 4 writes outputs/<date>/4/risk-kpis.yaml."""
    assert _file_exists_local_or_remote(RISK_KPIS), (
        f"Mandatory output missing: {RISK_KPIS}. "
        f"Layer 4 has not produced its structured KPI snapshot yet."
    )


def test_management_export_yaml_exists_at_dept_level() -> None:
    """Notion v4 L465: management-export.yaml lives at
    outputs/<date>/management-export.yaml — at the DEPT level (sibling to
    1/, 2/, 3/, 4/), NOT inside 4/. This is the file Tony's CEO loop
    scans across all bubble-ops-* repos."""
    assert _file_exists_local_or_remote(MGMT_EXPORT), (
        f"Mandatory output missing: {MGMT_EXPORT} "
        f"(this file must live at the DEPT level — outputs/<date>/management-export.yaml — "
        f"NOT inside outputs/<date>/4/). "
        f"Layer 4 has not produced the hierarchy export yet."
    )


# ---------------------------------------------------------------------------
# Assertions — schema validation
# ---------------------------------------------------------------------------


def test_risk_kpis_yaml_parses(mgmt_export_schema: dict[str, Any]) -> None:
    """risk-kpis.yaml must parse as YAML. We do not lock its schema yet
    (it has no dedicated schema in schemas-draft/), but it should at
    minimum be a non-empty mapping."""
    assert _file_exists_local_or_remote(RISK_KPIS), (
        f"{RISK_KPIS} missing — cannot parse a file that does not exist"
    )
    raw = _read_file_local_or_remote(RISK_KPIS)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), (
        f"{RISK_KPIS} must be a YAML mapping at top level; got {type(parsed).__name__}"
    )
    assert parsed, f"{RISK_KPIS} is empty — Layer 4 must emit at least one KPI"


def test_management_export_validates_against_schema(
    mgmt_export_schema: dict[str, Any],
) -> None:
    """management-export.yaml must validate against
    schemas-draft/management-export.schema.yaml (the contract for what
    every dept publishes to Tony's CEO loop)."""
    assert _file_exists_local_or_remote(MGMT_EXPORT), (
        f"{MGMT_EXPORT} missing — cannot validate schema of a file that does not exist"
    )
    raw = _read_file_local_or_remote(MGMT_EXPORT)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), (
        f"{MGMT_EXPORT} must be a YAML mapping at top level"
    )
    validator = Draft7Validator(mgmt_export_schema)
    errors = sorted(validator.iter_errors(parsed), key=lambda e: e.path)
    assert not errors, (
        f"{MGMT_EXPORT} fails management-export schema validation:\n"
        + "\n".join(
            f"  - {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )
    )


def test_management_export_dept_and_date_match(
    mgmt_export_schema: dict[str, Any],
) -> None:
    """The dept slug must be 'fixture' (per dept.yaml) and date must
    equal RUN_DATE. Cheap regression catch for a wrong-day commit."""
    assert _file_exists_local_or_remote(MGMT_EXPORT), (
        f"{MGMT_EXPORT} missing — cannot check dept/date of a file that does not exist"
    )
    parsed = yaml.safe_load(_read_file_local_or_remote(MGMT_EXPORT))
    assert parsed.get("dept") == "fixture", (
        f"{MGMT_EXPORT} dept={parsed.get('dept')!r}, expected 'fixture'"
    )
    assert parsed.get("date") == RUN_DATE, (
        f"{MGMT_EXPORT} date={parsed.get('date')!r}, expected {RUN_DATE!r}"
    )


# ---------------------------------------------------------------------------
# Assertions — the standard 4-file output schema for Layer 4 itself
# ---------------------------------------------------------------------------


def test_layer4_summary_md_exists() -> None:
    """Standard output schema: outputs/<date>/4/summary.md."""
    assert _file_exists_local_or_remote(SUMMARY_MD), (
        f"4-file schema violation: {SUMMARY_MD} missing"
    )


def test_layer4_artifacts_dir_present() -> None:
    """Standard output schema: outputs/<date>/4/artifacts/ exists.
    We check for either the .gitkeep marker OR any real artifact."""
    if _file_exists_local_or_remote(ARTIFACTS_DIR_GITKEEP):
        return
    # Fallback: dir is non-empty (gh api on a dir returns its listing)
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/vdk888/bubble-ops-fixture/contents/{LAYER4_DIR}/artifacts",
            ],
            check=False,
            timeout=20,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("["):
            return
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    pytest.fail(
        f"4-file schema violation: {LAYER4_DIR}/artifacts/ missing or empty "
        f"(checked .gitkeep and dir listing)"
    )


def test_layer4_logs_jsonl_exists_and_nonempty() -> None:
    """Standard output schema: outputs/<date>/4/logs.jsonl with >=1
    valid JSON object per line."""
    assert _file_exists_local_or_remote(LOGS_JSONL), (
        f"4-file schema violation: {LOGS_JSONL} missing"
    )
    raw = _read_file_local_or_remote(LOGS_JSONL)
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert lines, f"{LOGS_JSONL} exists but has zero non-empty lines"
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(f"{LOGS_JSONL} line {i + 1} is not valid JSON: {e}")
        assert isinstance(obj, dict), (
            f"{LOGS_JSONL} line {i + 1} must be a JSON object; got {type(obj).__name__}"
        )


def test_layer4_last_run_iso_timestamp() -> None:
    """Standard output schema: outputs/<date>/4/.last-run contains an
    ISO 8601 timestamp."""
    assert _file_exists_local_or_remote(LAST_RUN), (
        f"4-file schema violation: {LAST_RUN} missing"
    )
    raw = _read_file_local_or_remote(LAST_RUN).strip()
    assert raw, f"{LAST_RUN} exists but is empty"
    # Be permissive on trailing Z vs +00:00; just require fromisoformat parses
    # after normalising 'Z'.
    normalised = raw.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalised)
    except ValueError as e:
        pytest.fail(
            f"{LAST_RUN} content {raw!r} is not ISO 8601 parseable: {e}"
        )


# ---------------------------------------------------------------------------
# Bonus assertion — autonomy_readiness (per Notion v4 Q4 update + schema v3)
# ---------------------------------------------------------------------------


def test_autonomy_readiness_block_if_present_validates() -> None:
    """OPTIONAL block per schema v3. If risk-kpis.yaml OR management-export.yaml
    carries autonomy_readiness, it must validate. Layer 4 may legitimately
    skip this on day-1 (no rolling window yet) — that case is GREEN.

    This is a 'don't break the contract if you include it' assertion, not
    a 'must include it' assertion.
    """
    if not _file_exists_local_or_remote(MGMT_EXPORT):
        pytest.skip(f"{MGMT_EXPORT} not present yet — covered by earlier test")
    parsed = yaml.safe_load(_read_file_local_or_remote(MGMT_EXPORT))
    ar = parsed.get("autonomy_readiness")
    if ar is None:
        pytest.skip(
            "autonomy_readiness not present in management-export.yaml — "
            "legitimately optional on day-1 (no 14/30 day window yet)"
        )
    # If present, schema validation already covers it via
    # test_management_export_validates_against_schema. Sanity-check key fields:
    assert ar.get("window_days") in {14, 30}, (
        f"autonomy_readiness.window_days must be 14 or 30, got {ar.get('window_days')!r}"
    )
    assert isinstance(ar.get("action_classes"), list), (
        "autonomy_readiness.action_classes must be a list"
    )
