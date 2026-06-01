"""Step-11: 6 non-negotiables observable assertions on the live Morty fixture.

Each test produces a single line of evidence by interrogating the LIVE state
(GitHub API + ssh hetzner). All 6 must pass on a healthy deployment; failure
means a regression in the structural contract.

Source of the 6 non-negotiables: MVP-ROADMAP §0 "Non-negotiables" + the
Notion v4 spec sections cited inline below. Reproduced here verbatim:

  1. `optional_domain_ledger` slot present in `dept.yaml` (even when null).
  2. Tool vs skill distinction (sibling directories `tools/` and `skills/`).
  3. `tests/run.sh` harness covering 4 levels exists and is executable.
  4. `queues/management/` directory present (the cross-hierarchy escalation queue).
  5. Layer 4 produces 3 outputs contract is encoded
     (risk-brief.md / risk-kpis.yaml / management-export.yaml).
  6. `hierarchy:` block present in dept.yaml with level/parent/children/visibility.

These tests are read-only and do not modify any state. Skip gracefully when
ssh or gh CLI is unavailable.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

import pytest


REPO = "vdk888/bubble-ops-fixture"
SSH_HOST = "hetzner"
FIXTURE_DIR_ON_MORTY = "/home/claude/agents/fixture"


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _ssh_ok() -> bool:
    if not _has("ssh"):
        return False
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_HOST, "true"],
            capture_output=True,
            timeout=10,
            check=False,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_GH_OK = _has("gh")
_SSH_OK = _ssh_ok()


def _gh_contents(path: str, ref: str = "main") -> dict:
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{path}?ref={ref}"],
        capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(proc.stdout)


def _gh_dept_yaml(ref: str = "main") -> str:
    data = _gh_contents("dept.yaml", ref=ref)
    return base64.b64decode(data["content"]).decode("utf-8")


def _ssh_run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", SSH_HOST, cmd],
        capture_output=True, text=True, timeout=20, check=False,
    )


# --- Non-negotiable #1 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_1_optional_domain_ledger_slot_present():
    """`optional_domain_ledger:` must be present as a top-level key in dept.yaml,
    even when value is null. Per MVP-ROADMAP non-negotiable #1 and Notion v4
    callout "Doctrine storage corrigée".

    Evidence: `gh api repos/.../contents/dept.yaml | grep optional_domain_ledger`.
    """
    body = _gh_dept_yaml()
    lines = body.splitlines()
    keyed = [ln for ln in lines if ln.strip().startswith("optional_domain_ledger:")]
    assert len(keyed) == 1, (
        f"expected exactly 1 top-level optional_domain_ledger declaration; got {len(keyed)}: {keyed!r}"
    )
    # Value must be null for the fixture (no transactional truth to keep)
    val = keyed[0].split(":", 1)[1].strip().split("#", 1)[0].strip()
    assert val in ("null", "~", ""), (
        f"fixture's optional_domain_ledger should be null; got {val!r}. "
        "Setting it non-null implies the dept owns a domain ledger — fixture has none."
    )


# --- Non-negotiable #2 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_2_tool_vs_skill_distinction():
    """Both `tools/` and `skills/` must exist as sibling top-level directories
    with REAL content (not just .gitkeep). Notion v4 §"Skill vs tool" is
    structurally important — a tool is deterministic, a skill is agentic.

    Evidence: `gh api .../contents/tools/echo-tool` returns tool.py + schema.json;
    `gh api .../contents/skills/echo-skill` returns SKILL.md.
    """
    tools = _gh_contents("tools/echo-tool")
    tool_names = {f["name"] for f in tools}
    assert "tool.py" in tool_names, (
        f"tools/echo-tool/ missing tool.py (deterministic function); got {tool_names!r}"
    )
    assert "schema.json" in tool_names, (
        f"tools/echo-tool/ missing schema.json (I/O contract); got {tool_names!r}"
    )

    skills = _gh_contents("skills/echo-skill")
    skill_names = {f["name"] for f in skills}
    assert "SKILL.md" in skill_names, (
        f"skills/echo-skill/ missing SKILL.md (agentic stub); got {skill_names!r}"
    )


# --- Non-negotiable #3 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_3_tests_run_sh_exists_and_covers_4_levels():
    """`tests/run.sh` must exist and reference the 4 levels (tool / skill / layer /
    department) per Notion v4 line 88-94. The harness existence is the
    non-negotiable; its actual rigor is upgraded as depts mature.

    Evidence: `gh api .../contents/tests/run.sh` returns 200; body cites all 4 tiers.
    """
    data = _gh_contents("tests/run.sh")
    body = base64.b64decode(data["content"]).decode("utf-8").lower()
    for tier in ("tool", "skill", "layer", "department"):
        assert tier in body, (
            f"tests/run.sh does not mention tier {tier!r}; body excerpt:\n{body[:500]}"
        )


# --- Non-negotiable #4 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_4_queues_management_directory_present():
    """`queues/management/` must exist (the cross-hierarchy escalation queue).
    Notion v4 §Queues mandates exactly 4: research, gates, management,
    improvements. This test asserts ALL 4 exist but the management one is
    the load-bearing one (it's how dept-level findings reach the principal).

    Evidence: `gh api .../contents/queues/management` returns 200 with .gitkeep.
    """
    expected_queues = {"research", "gates", "management", "improvements"}
    contents = _gh_contents("queues")
    present = {f["name"] for f in contents if f["type"] == "dir"}
    missing = expected_queues - present
    assert not missing, f"queues/ missing required dirs: {sorted(missing)}; have: {sorted(present)}"

    # And the load-bearing one must have a real entry (at least .gitkeep)
    mgmt = _gh_contents("queues/management")
    assert len(mgmt) >= 1, f"queues/management/ must contain at least .gitkeep; got {mgmt!r}"


# --- Non-negotiable #5 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_5_layer_4_triple_output_contract_encoded():
    """Layer 4 mandate-guardian must be wired to produce 3 outputs:
    risk-brief.md, risk-kpis.yaml, management-export.yaml. This is the
    'hierarchy contract' encoded in the layer prompt + the subagent contract.

    Evidence: `gh api .../contents/layers/4/PROMPT.md` body mentions all 3 names;
    `gh api .../contents/.claude/agents/mandate-guardian.md` mentions all 3 names.
    """
    expected_outputs = {"risk-brief.md", "risk-kpis.yaml", "management-export.yaml"}

    layer4 = _gh_contents("layers/4/PROMPT.md")
    layer4_body = base64.b64decode(layer4["content"]).decode("utf-8")
    missing_in_prompt = expected_outputs - {n for n in expected_outputs if n in layer4_body}
    assert not missing_in_prompt, (
        f"layers/4/PROMPT.md missing output names: {sorted(missing_in_prompt)}"
    )

    # Subagent contract — accept either .claude/agents/ or subagents/ layout
    guardian = None
    for candidate in (".claude/agents/mandate-guardian.md", "subagents/mandate-guardian.md"):
        try:
            guardian = _gh_contents(candidate)
            break
        except subprocess.CalledProcessError:
            continue
    assert guardian is not None, (
        "mandate-guardian subagent file missing from both .claude/agents/ and subagents/"
    )
    guardian_body = base64.b64decode(guardian["content"]).decode("utf-8")
    missing_in_guardian = expected_outputs - {n for n in expected_outputs if n in guardian_body}
    assert not missing_in_guardian, (
        f"mandate-guardian subagent missing output names: {sorted(missing_in_guardian)}"
    )


# --- Non-negotiable #6 -----------------------------------------------------


@pytest.mark.skipif(not _GH_OK, reason="gh CLI not available")
def test_6_hierarchy_block_present_in_dept_yaml():
    """`hierarchy:` block must be a top-level key in dept.yaml with required
    sub-fields: level, parent, children, visibility, directive_policy.
    Per Notion v4 §"Hierarchy" — the hierarchy is what makes the dept addressable
    by Tony / principals.

    Evidence: `gh api .../contents/dept.yaml | jq` shows hierarchy block.
    """
    import yaml as _yaml

    body = _gh_dept_yaml()
    parsed = _yaml.safe_load(body)
    assert isinstance(parsed, dict), f"dept.yaml not a mapping: {type(parsed).__name__}"
    assert "hierarchy" in parsed, f"dept.yaml missing top-level `hierarchy:` block"

    hier = parsed["hierarchy"]
    assert isinstance(hier, dict), f"hierarchy must be a mapping; got {type(hier).__name__}"
    required_subfields = {"level", "parent", "children", "visibility", "directive_policy"}
    missing = required_subfields - set(hier.keys())
    assert not missing, (
        f"hierarchy block missing required sub-fields: {sorted(missing)}; "
        f"present: {sorted(hier.keys())}"
    )
    # Fixture is an ops-leaf: parent=null, children=[]
    assert hier["parent"] is None, f"fixture is leaf; expected parent=null, got {hier['parent']!r}"
    assert hier["children"] == [], (
        f"fixture is leaf; expected children=[], got {hier['children']!r}"
    )


# --- Bonus: Morty deployment surface health ------------------------------


@pytest.mark.skipif(not _SSH_OK, reason=f"ssh {SSH_HOST} not reachable")
def test_morty_fixture_service_active():
    """ops-loop-fixture.service must be active (running) on Morty.

    Evidence: `ssh hetzner sudo systemctl is-active ops-loop-fixture.service`."""
    proc = _ssh_run("sudo systemctl is-active ops-loop-fixture.service")
    assert proc.returncode == 0 and proc.stdout.strip() == "active", (
        f"ops-loop-fixture.service not active: rc={proc.returncode} "
        f"out={proc.stdout.strip()!r} err={proc.stderr.strip()!r}"
    )


@pytest.mark.skipif(not _SSH_OK, reason=f"ssh {SSH_HOST} not reachable")
def test_morty_parent_service_unchanged():
    """claude-agent-morty.service MD5 must remain ecfc78ac20e182ca302e5081e2c80943
    (Step 11 acceptance criterion: morty production service untouched)."""
    proc = _ssh_run("md5sum /etc/systemd/system/claude-agent-morty.service")
    assert proc.returncode == 0, f"md5sum ssh failed: {proc.stderr!r}"
    # Output format: "<md5>  /path"
    md5 = proc.stdout.split()[0]
    expected = "ecfc78ac20e182ca302e5081e2c80943"
    assert md5 == expected, (
        f"morty production service was modified! md5={md5!r}, expected={expected!r}. "
        "Step-11 acceptance violated."
    )
