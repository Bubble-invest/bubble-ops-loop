"""
test_skeleton_completeness.py — Step 5 RED/GREEN validator.

Asserts the bubble-ops-fixture repo (cloned at /tmp/bubble-ops-fixture/) carries
the full Notion-v4-aligned skeleton per MVP-ROADMAP v2 §5 + the per-file content
rules from Step 5's brief.

Run:
    python3 -m pytest tests/test_skeleton_completeness.py -v

Conventions:
- All paths absolute.
- No mutation of the fixture repo here — read-only assertions.
- One test per acceptance criterion in the brief.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

# ----------------------------------------------------------------------------
# Constants — pinned absolute paths per the spec.
# ----------------------------------------------------------------------------

FIXTURE_REPO = Path("/tmp/bubble-ops-fixture")
# schemas-draft/ lives at the repo root (this file is at <repo>/tests/).
SCHEMAS_DRAFT = Path(__file__).resolve().parents[1] / "schemas-draft"
DEPT_YAML_CANONICAL = SCHEMAS_DRAFT / "examples" / "dept-ops-leaf-fixture.yaml"
DEPT_SCHEMA = SCHEMAS_DRAFT / "dept.schema.yaml"

# Spec tree from Step 5 brief — every file listed here MUST exist.
# Paths are RELATIVE to FIXTURE_REPO.
REQUIRED_FILES = [
    "README.md",
    ".gitignore",
    "dept.yaml",
    "MANDATE.md",
    "CLAUDE.md",
    "layers/1/PROMPT.md",
    "layers/2/PROMPT.md",
    "layers/3/PROMPT.md",
    "layers/4/PROMPT.md",
    "subagents/data-curator.md",
    "subagents/task-orchestrator.md",
    "subagents/executor.md",
    "subagents/mandate-guardian.md",
    "skills/echo-skill/SKILL.md",
    "tools/echo-tool/tool.py",
    "tools/echo-tool/schema.json",
    "tools/echo-tool/README.md",
    "tests/run.sh",
    "tests/fixtures/tool/echo-input.json",
    "tests/fixtures/skill/echo-context.yaml",
    "tests/fixtures/layer/queue-item.yaml",
    "tests/fixtures/department/dry-run-input.yaml",
    "queues/research/.gitkeep",
    "queues/gates/.gitkeep",
    "queues/management/.gitkeep",
    "queues/improvements/.gitkeep",
    "inbox/decisions/.gitkeep",
    "outputs/.gitkeep",
    ".claude/settings.json",
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _walk_repo_files(root: Path) -> set[Path]:
    """Return every tracked file path relative to `root`, excluding .git/."""
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        # don't descend into .git
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            p = Path(dirpath) / fn
            found.add(p.relative_to(root))
    return found


def _parse_subagent_frontmatter(path: Path) -> dict:
    """Parse the YAML frontmatter block at the top of a markdown file."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path} has no frontmatter (must start with ---)"
    # Split on the first two `---` delimiters.
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"{path} frontmatter delimiter missing closing ---"
    fm_block = parts[1]
    parsed = yaml.safe_load(fm_block)
    assert isinstance(parsed, dict), f"{path} frontmatter must parse to a dict"
    return parsed


def _tool_list(fm_tools) -> list[str]:
    """Subagent `tools:` can be a comma string OR a list. Normalize to list[str]."""
    if isinstance(fm_tools, str):
        return [t.strip() for t in fm_tools.split(",") if t.strip()]
    if isinstance(fm_tools, list):
        return [str(t).strip() for t in fm_tools]
    raise AssertionError(f"unrecognized tools shape: {type(fm_tools)} {fm_tools!r}")


# ----------------------------------------------------------------------------
# 1. Tree completeness
# ----------------------------------------------------------------------------


def test_tree_matches_spec():
    """Every file listed in REQUIRED_FILES exists; no UNEXPECTED files outside spec."""
    assert FIXTURE_REPO.is_dir(), f"fixture repo missing at {FIXTURE_REPO}"
    actual = _walk_repo_files(FIXTURE_REPO)
    required = {Path(p) for p in REQUIRED_FILES}

    missing = required - actual
    assert not missing, f"missing required files: {sorted(str(p) for p in missing)}"

    # Allow-list: spec files + dynamic runtime artifacts (none allowed at commit time).
    # Anything outside REQUIRED_FILES is a violation EXCEPT well-known commit-time
    # files we whitelist explicitly here.
    unexpected = actual - required
    # No unexpected files allowed for a clean skeleton commit.
    assert not unexpected, (
        f"unexpected files outside spec: {sorted(str(p) for p in unexpected)}"
    )


# ----------------------------------------------------------------------------
# 2. dept.yaml validates against schema
# ----------------------------------------------------------------------------


def test_dept_yaml_validates():
    """dept.yaml in fixture repo validates against dept.schema.yaml (Step 0)."""
    import jsonschema
    from jsonschema import Draft7Validator

    dept_yaml_path = FIXTURE_REPO / "dept.yaml"
    schema_path = DEPT_SCHEMA

    assert dept_yaml_path.is_file(), f"missing {dept_yaml_path}"
    assert schema_path.is_file(), f"missing {schema_path}"

    with dept_yaml_path.open() as fh:
        instance = yaml.safe_load(fh)
    with schema_path.open() as fh:
        schema = yaml.safe_load(fh)

    Draft7Validator.check_schema(schema)
    errors = sorted(
        Draft7Validator(schema).iter_errors(instance), key=lambda e: list(e.path)
    )
    assert not errors, "dept.yaml does not validate: " + "; ".join(
        f"{list(e.path)}: {e.message}" for e in errors
    )


def test_dept_yaml_is_byte_identical_to_canonical():
    """dept.yaml in fixture = byte-identical copy of canonical Step-1 example."""
    repo_bytes = (FIXTURE_REPO / "dept.yaml").read_bytes()
    canon_bytes = DEPT_YAML_CANONICAL.read_bytes()
    assert repo_bytes == canon_bytes, (
        f"dept.yaml mismatch with {DEPT_YAML_CANONICAL}: "
        f"len diff {len(repo_bytes)} vs {len(canon_bytes)}"
    )


# ----------------------------------------------------------------------------
# 3. Layer prompts mention 4-file output schema
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("layer", [1, 2, 3, 4])
def test_layer_prompts_have_required_outputs(layer):
    """Each layers/N/PROMPT.md cites the 4 output files per Notion v4."""
    p = FIXTURE_REPO / "layers" / str(layer) / "PROMPT.md"
    assert p.is_file(), f"missing {p}"
    body = p.read_text(encoding="utf-8")
    for needle in ("summary.md", "artifacts/", "logs.jsonl", ".last-run"):
        assert needle in body, f"{p} missing required mention of {needle!r}"


# ----------------------------------------------------------------------------
# 4. Layer 4 produces all 3 outputs
# ----------------------------------------------------------------------------


def test_layer_4_writes_three_outputs():
    """layers/4/PROMPT.md cites all 3 hierarchy outputs (Notion v4 §Layer 4)."""
    p = FIXTURE_REPO / "layers" / "4" / "PROMPT.md"
    body = p.read_text(encoding="utf-8")
    for needle in ("risk-brief.md", "risk-kpis.yaml", "management-export.yaml"):
        assert needle in body, f"layers/4/PROMPT.md missing {needle!r}"


# ----------------------------------------------------------------------------
# 5. Subagent frontmatter shapes
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subagent",
    ["data-curator", "task-orchestrator", "executor", "mandate-guardian"],
)
def test_subagent_frontmatter_valid(subagent):
    """Each subagents/*.md has YAML frontmatter with name + description + tools."""
    p = FIXTURE_REPO / "subagents" / f"{subagent}.md"
    fm = _parse_subagent_frontmatter(p)
    assert "name" in fm, f"{p} frontmatter missing `name`"
    assert "description" in fm, f"{p} frontmatter missing `description`"
    assert "tools" in fm, f"{p} frontmatter missing `tools`"


def test_subagent_perms_data_curator():
    """data-curator: has Read, WebFetch, Bash (Notion §Layer 1 subagent)."""
    fm = _parse_subagent_frontmatter(
        FIXTURE_REPO / "subagents" / "data-curator.md"
    )
    tools = _tool_list(fm["tools"])
    for needed in ("Read", "WebFetch", "Bash"):
        assert needed in tools, f"data-curator missing tool {needed!r}; has {tools}"


def test_subagent_perms_task_orchestrator():
    """task-orchestrator: has Agent (can spawn sub-subagents); permissionMode set."""
    fm = _parse_subagent_frontmatter(
        FIXTURE_REPO / "subagents" / "task-orchestrator.md"
    )
    tools = _tool_list(fm["tools"])
    assert "Agent" in tools, f"task-orchestrator missing Agent tool; has {tools}"
    mode = fm.get("permissionMode")
    assert mode in ("ask", "acceptEdits"), (
        f"task-orchestrator permissionMode={mode!r} must be ask|acceptEdits"
    )


def test_subagent_perms_executor():
    """executor: has Bash; explicitly NO WebFetch / NO WebSearch."""
    fm = _parse_subagent_frontmatter(
        FIXTURE_REPO / "subagents" / "executor.md"
    )
    tools = _tool_list(fm["tools"])
    assert "Bash" in tools, f"executor missing Bash; has {tools}"
    # Either tools allowlist omits Web*, OR disallowedTools explicitly bans them.
    disallow = fm.get("disallowedTools", "")
    disallow_list = _tool_list(disallow) if disallow else []
    for forbidden in ("WebFetch", "WebSearch"):
        in_allow = forbidden in tools
        in_disallow = forbidden in disallow_list
        assert (not in_allow) or in_disallow, (
            f"executor has {forbidden} in tools and not in disallowedTools "
            f"(tools={tools}, disallow={disallow_list})"
        )


def test_subagent_perms_mandate_guardian():
    """mandate-guardian: pure auditor — only Read/Grep/Glob/WebSearch/Write, no Bash/Agent."""
    fm = _parse_subagent_frontmatter(
        FIXTURE_REPO / "subagents" / "mandate-guardian.md"
    )
    tools = _tool_list(fm["tools"])
    # Must include the audit toolkit:
    for needed in ("Read", "Grep", "Glob", "WebSearch"):
        assert needed in tools, f"mandate-guardian missing {needed}; has {tools}"
    # Must NOT include side-effect tools:
    for forbidden in ("Bash", "Agent"):
        assert forbidden not in tools, (
            f"mandate-guardian must not have {forbidden}; has {tools}"
        )


# ----------------------------------------------------------------------------
# 6. echo-tool schema sanity
# ----------------------------------------------------------------------------


def test_echo_tool_has_schema():
    """tools/echo-tool/schema.json parses + has input/output properties."""
    p = FIXTURE_REPO / "tools" / "echo-tool" / "schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "input" in data, f"{p} missing top-level 'input' property"
    assert "output" in data, f"{p} missing top-level 'output' property"


# ----------------------------------------------------------------------------
# 7. echo-skill frontmatter
# ----------------------------------------------------------------------------


def test_echo_skill_has_frontmatter():
    """skills/echo-skill/SKILL.md has YAML frontmatter with name + description."""
    p = FIXTURE_REPO / "skills" / "echo-skill" / "SKILL.md"
    fm = _parse_subagent_frontmatter(p)
    assert "name" in fm and "description" in fm, (
        f"{p} frontmatter must have name + description; got {fm.keys()}"
    )


# ----------------------------------------------------------------------------
# 8. tests/run.sh exists, executable, passes
# ----------------------------------------------------------------------------


def test_tests_runsh_exists_and_executable():
    p = FIXTURE_REPO / "tests" / "run.sh"
    assert p.is_file(), f"{p} missing"
    st = p.stat()
    assert st.st_mode & stat.S_IXUSR, f"{p} not executable for user"


def test_tests_runsh_passes():
    """`bash tests/run.sh` exits 0 with the 4-level PASS line."""
    p = FIXTURE_REPO / "tests" / "run.sh"
    proc = subprocess.run(
        ["bash", str(p)],
        cwd=str(FIXTURE_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, (
        f"run.sh exited {proc.returncode}; output:\n{out}"
    )
    for needle in ("tool: PASS", "skill: PASS", "layer: PASS", "department: PASS"):
        assert needle in out, f"run.sh output missing {needle!r}; full:\n{out}"


# ----------------------------------------------------------------------------
# 9. No secret files
# ----------------------------------------------------------------------------


def test_no_secret_files_committed():
    """No *.pem, *.env, *.key, .tokens.json anywhere in the tree."""
    forbidden_patterns = (
        re.compile(r".*\.pem$"),
        re.compile(r".*\.env$"),
        re.compile(r".*\.key$"),
        re.compile(r"\.tokens\.json$"),
    )
    bad = []
    for rel in _walk_repo_files(FIXTURE_REPO):
        name = rel.name
        for pat in forbidden_patterns:
            if pat.match(name):
                bad.append(str(rel))
    assert not bad, f"forbidden secret files present: {bad}"
