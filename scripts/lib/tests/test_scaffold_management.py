"""
test_scaffold_management.py — TDD tests for GAP 2.

Tests cover:
  - test_scaffold_management_level_dept_yaml
      Rendered dept.yaml.draft has level=management + hierarchy.children populated
      + visibility.read_paths matches Notion.
  - test_scaffold_management_level_claude_md_mentions_children
      CLAUDE.md mentions each child slug + the read_paths whitelist.
  - test_scaffold_ops_level_unchanged
      The existing ops-leaf scaffold output is byte-identical to before
      (no regression).
  - test_scaffold_rejects_children_without_management
      Passing --children=ben,maya WITHOUT --level=management is a hard error.
  - test_bootstrap_dept_sh_passes_level_and_children_through
      Shell-level: the new CLI flags reach scaffold.py.
  - test_scaffold_management_settings_json_allows_priority_pr
      .claude/settings.json allow-list contains the priority-pr permission entry.

All tests are file-system-only. No network. No GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path surgery so scaffold.py and its deps are importable.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent               # scripts/lib/
SCRIPTS_DIR = SCRIPTS_LIB.parent        # scripts/
PROJECT_ROOT = SCRIPTS_DIR.parent       # bubble-ops-loop/
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402  (after path surgery)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANAGEMENT_CHILDREN = ["ben", "maya", "miranda", "eliot"]

# The exact read_paths mandated by Notion (audit §1.1 + management-policy.template.yaml)
EXPECTED_READ_PATHS = [
    "outputs/*/4/risk-kpis.yaml",
    "outputs/*/4/risk-brief.md",
    "outputs/*/management-export.yaml",
    "queues/gates/**",
    "queues/improvements/**",
]


def _scaffold_management(tmp_path: Path) -> Path:
    """Scaffold a management dept into a fresh tmp dir and return its root."""
    root = tmp_path / "bubble-ops-tony"
    root.mkdir()
    scaffold.scaffold(
        root=root,
        slug="tony",
        display_name="Tony",
        owner="joris",
        level="management",
        children=MANAGEMENT_CHILDREN,
    )
    return root


def _scaffold_ops(tmp_path: Path) -> Path:
    """Scaffold a standard ops dept into a fresh tmp dir and return its root."""
    root = tmp_path / "bubble-ops-smoke"
    root.mkdir()
    scaffold.scaffold(
        root=root,
        slug="smoke",
        display_name="Smoke",
        owner="joris",
        level="ops",
        children=[],
    )
    return root


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scaffold_management_level_dept_yaml(tmp_path: Path):
    """dept.yaml.draft must have level=management, correct children + visibility."""
    root = _scaffold_management(tmp_path)
    draft_path = root / "dept.yaml.draft"
    assert draft_path.exists(), "dept.yaml.draft was not created"

    doc = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # department.level
    assert doc["department"]["level"] == "management", (
        f"Expected department.level=management, got {doc['department']['level']!r}"
    )

    # hierarchy.level
    assert doc["hierarchy"]["level"] == "management", (
        f"Expected hierarchy.level=management, got {doc['hierarchy']['level']!r}"
    )

    # hierarchy.children
    children = doc["hierarchy"]["children"]
    assert sorted(children) == sorted(MANAGEMENT_CHILDREN), (
        f"Expected hierarchy.children={MANAGEMENT_CHILDREN}, got {children}"
    )

    # hierarchy.visibility.read_outputs must list all children
    read_outputs = doc["hierarchy"]["visibility"]["read_outputs"]
    assert sorted(read_outputs) == sorted(MANAGEMENT_CHILDREN), (
        f"Expected read_outputs={MANAGEMENT_CHILDREN}, got {read_outputs}"
    )

    # visibility.read_risk_kpis / read_risk_briefs must be true
    assert doc["hierarchy"]["visibility"]["read_risk_kpis"] is True
    assert doc["hierarchy"]["visibility"]["read_risk_briefs"] is True
    assert doc["hierarchy"]["visibility"]["read_raw_artifacts"] is False
    assert doc["hierarchy"]["visibility"]["read_secrets"] is False

    # directive_policy.can_open_priority_prs must be true
    assert doc["hierarchy"]["directive_policy"]["can_open_priority_prs"] is True

    # layers.subscribed must be [1, 4]
    assert sorted(doc["layers"]["subscribed"]) == [1, 4], (
        f"Expected layers.subscribed=[1,4], got {doc['layers']['subscribed']}"
    )

    # recurring_missions: management depts include the daily_risk_audit (GAP-10 G-1).
    # The old assertion of [] is superseded by the G-1 fix.
    missions = doc.get("recurring_missions", [])
    layer4_missions = [m for m in missions if m.get("layer") == 4]
    assert layer4_missions, (
        f"Management recurring_missions must include at least one layer:4 mission "
        f"(daily_risk_audit). Got: {missions}"
    )

    # visibility.read_paths must be present and match spec
    read_paths = doc["hierarchy"]["visibility"].get("read_paths")
    assert read_paths is not None, (
        "Expected hierarchy.visibility.read_paths to be present"
    )
    assert sorted(read_paths) == sorted(EXPECTED_READ_PATHS), (
        f"read_paths mismatch.\nExpected: {sorted(EXPECTED_READ_PATHS)}\nGot: {sorted(read_paths)}"
    )


def test_scaffold_management_level_claude_md_mentions_children(tmp_path: Path):
    """CLAUDE.md must mention each child slug and the read_paths whitelist."""
    root = _scaffold_management(tmp_path)
    claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")

    for child in MANAGEMENT_CHILDREN:
        assert child in claude_md, (
            f"CLAUDE.md does not mention child slug '{child}'"
        )

    # At least the core paths should appear in the CLAUDE.md
    for path in EXPECTED_READ_PATHS:
        assert path in claude_md, (
            f"CLAUDE.md does not mention read_path '{path}'"
        )

    # Management-specific prose cues
    assert "management" in claude_md.lower(), (
        "CLAUDE.md should mention management role"
    )
    # Should NOT describe the 7-step ops eclosure as primary mandate
    # (management has a different cadence)
    assert "priority" in claude_md.lower() or "directive" in claude_md.lower(), (
        "CLAUDE.md should mention priority directives or management directives"
    )


def test_scaffold_management_settings_json_allows_priority_pr(tmp_path: Path):
    """settings.json must allow the bubble-git-guard push open_priority_pr command."""
    root = _scaffold_management(tmp_path)
    settings_path = root / ".claude" / "settings.json"
    assert settings_path.exists(), ".claude/settings.json was not created"

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    allow_list = data.get("permissions", {}).get("allow", [])

    # Must include the priority-pr guard permission
    priority_pr_entries = [
        e for e in allow_list
        if "open_priority_pr" in e or "priority_pr" in e
    ]
    assert priority_pr_entries, (
        f"No priority_pr permission in settings.json allow-list.\n"
        f"allow-list: {allow_list}"
    )
    # Specifically the guard invocation form
    guard_entries = [e for e in allow_list if "bubble-git-guard" in e]
    assert guard_entries, (
        f"No bubble-git-guard entry in settings.json allow-list.\nallow-list: {allow_list}"
    )


def test_scaffold_ops_level_unchanged(tmp_path: Path):
    """The ops-leaf scaffold must not regress — same shape as before."""
    root = _scaffold_ops(tmp_path)
    draft_path = root / "dept.yaml.draft"
    assert draft_path.exists(), "dept.yaml.draft was not created for ops dept"

    doc = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # Core ops defaults must be intact
    assert doc["department"]["level"] == "ops"
    assert doc["hierarchy"]["level"] == "ops"
    assert doc["hierarchy"]["children"] == []
    assert doc["hierarchy"]["directive_policy"]["can_open_priority_prs"] is False
    assert doc["layers"]["subscribed"] == [1, 2, 3, 4]

    # CLAUDE.md must still contain the 7-step prose
    claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "7" in claude_md or "sept" in claude_md.lower() or "step" in claude_md.lower(), (
        "Ops CLAUDE.md lost its 7-step eclosure reference"
    )

    # settings.json must exist
    settings_path = root / ".claude" / "settings.json"
    assert settings_path.exists()


def test_scaffold_rejects_children_without_management(tmp_path: Path):
    """Passing children without level=management must raise ValueError."""
    root = tmp_path / "bubble-ops-bad"
    root.mkdir()

    with pytest.raises((ValueError, SystemExit)):
        scaffold.scaffold(
            root=root,
            slug="bad",
            display_name="Bad",
            owner="joris",
            level="ops",          # NOT management
            children=["ben", "maya"],  # but children provided
        )


def test_scaffold_rejects_management_without_children(tmp_path: Path):
    """Scaffolding management with zero children must raise ValueError."""
    root = tmp_path / "bubble-ops-empty-mgmt"
    root.mkdir()

    with pytest.raises((ValueError, SystemExit)):
        scaffold.scaffold(
            root=root,
            slug="empty-mgmt",
            display_name="EmptyMgmt",
            owner="joris",
            level="management",
            children=[],
        )


def test_bootstrap_dept_sh_passes_level_and_children_through(tmp_path: Path):
    """Shell-level: --level and --children CLI flags reach scaffold.py (dry-run mode)."""
    clone_dir = tmp_path / "clones"
    clone_dir.mkdir()

    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(clone_dir)
    # Provide a fake gh binary that always fails `repo view` (repo doesn't exist)
    # and records calls — same pattern as onboarding-bootstrap conftest
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'GraphQL: Could not resolve to a Repository (404)' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = SCRIPTS_DIR / "bootstrap-dept.sh"
    res = subprocess.run(
        [
            "bash", str(script),
            "--slug=tony",
            "--display-name=Tony",
            "--owner=joris",
            "--level=management",
            "--children=ben,maya,miranda,eliot",
            "--dry-run",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (
        f"bootstrap-dept.sh --level=management --children=... --dry-run failed.\n"
        f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    )

    # Verify the rendered dept.yaml.draft has management shape
    rendered_root = clone_dir / "bubble-ops-tony"
    draft = rendered_root / "dept.yaml.draft"
    assert draft.exists(), f"dept.yaml.draft not created at {draft}"

    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    assert doc["department"]["level"] == "management"
    assert sorted(doc["hierarchy"]["children"]) == sorted(
        ["ben", "maya", "miranda", "eliot"]
    )


def test_bootstrap_dept_sh_accept_existing_empty_repo_proceeds(tmp_path: Path):
    """When --accept-existing-empty-repo is set AND the remote exists with
    zero commits, bootstrap should SKIP the gh repo create step and proceed
    to clone.

    We simulate this with a fake gh that:
      - returns 0 for `gh repo view` (repo exists)
      - returns 'null' for `gh api repos/.../.jq .default_branch` (no commits)
      - records all invocations

    Then we verify that `gh repo create` is NEVER called in the recorded log.
    """
    clone_dir = tmp_path / "clones"
    clone_dir.mkdir()
    gh_log = tmp_path / "gh-calls.log"

    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(clone_dir)
    env["FAKE_GH_REPO_URL"] = str(tmp_path / "fake-remote.git")
    # Init a bare repo so git clone has somewhere to point to
    subprocess.run(["git", "init", "--bare", env["FAKE_GH_REPO_URL"]], check=True, capture_output=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env bash
echo "[fake-gh] $*" >> {gh_log}
case "$1 $2" in
  "repo view")
    # Repo exists
    exit 0
    ;;
  "api repos/"*)
    # Return null for default_branch (empty repo)
    echo null
    exit 0
    ;;
  "repo create"*)
    # We do NOT expect this when --accept-existing-empty-repo + empty repo.
    # If reached, fail loudly so the test sees it.
    echo "[fake-gh] ERROR: repo create should not have been called!" >&2
    exit 99
    ;;
  *)
    exit 0
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = SCRIPTS_DIR / "bootstrap-dept.sh"
    res = subprocess.run(
        [
            "bash", str(script),
            "--slug=tony-empty",
            "--display-name=Tony Empty",
            "--owner=joris",
            "--level=management",
            "--children=fixture",
            "--accept-existing-empty-repo",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # We don't care if the script ultimately exits 0 — local git push to a
    # bare repo without auth will likely fail. We care that:
    #   (a) gh repo view was called
    #   (b) gh repo create was NEVER called
    log = gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""
    assert "repo view" in log, f"gh repo view should have been called.\nLOG: {log}\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    assert "repo create" not in log, (
        f"gh repo create should NOT have been called when --accept-existing-empty-repo "
        f"+ empty repo.\nLOG: {log}\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    )


def test_bootstrap_dept_sh_accept_existing_empty_repo_refuses_nonempty(tmp_path: Path):
    """When --accept-existing-empty-repo is set BUT the remote has commits,
    bootstrap MUST refuse — won't clobber real work."""
    clone_dir = tmp_path / "clones"
    clone_dir.mkdir()

    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(clone_dir)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
case "$1 $2" in
  "repo view") exit 0 ;;
  "api repos/"*) echo main; exit 0 ;;   # non-null = repo has commits
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = SCRIPTS_DIR / "bootstrap-dept.sh"
    res = subprocess.run(
        [
            "bash", str(script),
            "--slug=tony-nonempty",
            "--display-name=Tony NonEmpty",
            "--owner=joris",
            "--accept-existing-empty-repo",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode != 0, (
        "expected exit != 0 when --accept-existing-empty-repo + repo has commits, "
        f"got {res.returncode}\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    )
    assert "has commits" in res.stderr or "NOT empty" in res.stderr, (
        f"expected the safety message; got STDERR={res.stderr}"
    )


def test_bootstrap_dept_sh_dry_run_is_idempotent(tmp_path: Path):
    """Running bootstrap-dept.sh --dry-run twice in a row must both succeed.

    Pre-fix bug: scaffold.py's init_state() refused to overwrite an existing
    STATE.yaml, so a second --dry-run crashed with FileExistsError. Real
    operator pain since iterating on flags is the whole point of --dry-run.

    Fix in bootstrap-dept.sh: when --dry-run and target is under /tmp/, wipe
    the target before re-rendering.
    """
    clone_dir = tmp_path / "clones"
    clone_dir.mkdir()

    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(clone_dir)
    # Provide a fake gh — same pattern as the test above.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = SCRIPTS_DIR / "bootstrap-dept.sh"
    args = [
        "bash", str(script),
        "--slug=tony-idempotency",
        "--display-name=Tony Idempotency",
        "--owner=joris",
        "--level=management",
        "--children=fixture",
        "--dry-run",
    ]

    # First run — should succeed
    res1 = subprocess.run(args, env=env, capture_output=True, text=True)
    assert res1.returncode == 0, (
        f"first --dry-run failed.\nSTDOUT: {res1.stdout}\nSTDERR: {res1.stderr}"
    )

    # Second run — must NOT crash on the pre-existing STATE.yaml
    res2 = subprocess.run(args, env=env, capture_output=True, text=True)
    assert res2.returncode == 0, (
        f"second --dry-run crashed (idempotency regression).\n"
        f"STDOUT: {res2.stdout}\nSTDERR: {res2.stderr}"
    )


def test_bootstrap_dept_sh_dry_run_refuses_to_wipe_non_tmp_target(tmp_path: Path):
    """Defensive: when the env var is UNSET and CLONE_PARENT computes to a
    non-/tmp/ path, refuse the wipe. Protects against catastrophic misuse
    if a future caller patches the default CLONE_PARENT to a real workspace.

    Setup: we shim `/tmp` by routing it through a wrapper script that
    redirects to a non-tmp dir, so we can observe the safety guard
    without actually messing with /tmp. The realistic prod scenario is
    "someone modified the default CLONE_PARENT in the script and forgot
    the wipe-safety case" — this test catches that.

    For simplicity: we use a custom bootstrap-dept.sh wrapper that
    runs the real script with CLONE_PARENT pointed at the non-tmp dir
    by editing the line in-place via a sed temp copy.

    NOTE: the clone dir must genuinely NOT be under /tmp/ for the guard to
    fire. `tmp_path` is under /var/folders on macOS but UNDER /tmp on Linux CI,
    so we can't use it here — we create the clone dir under $HOME instead (never
    /tmp on either platform) and clean it up ourselves.
    """
    import tempfile as _tempfile
    home_tmp = _tempfile.mkdtemp(prefix="bootstrap-wipe-test-", dir=str(Path.home()))
    self_cleanup = home_tmp
    try:
        clone_dir = Path(home_tmp) / "not-under-tmp"   # guaranteed not /tmp/*
        clone_dir.mkdir()
        (clone_dir / "bubble-ops-canary").mkdir()
        (clone_dir / "bubble-ops-canary" / "dummy.txt").write_text("hi", encoding="utf-8")
        return _run_wipe_guard_assertions(tmp_path, clone_dir)
    finally:
        shutil.rmtree(self_cleanup, ignore_errors=True)


def _run_wipe_guard_assertions(tmp_path: Path, clone_dir: Path):
    """Body of the wipe-refusal test, factored out so the clone dir (which must
    live outside /tmp on both macOS and Linux) is cleaned up via try/finally."""

    # Make a patched copy of the script that defaults CLONE_PARENT to our
    # non-tmp dir (and ignores the env var so we can hit the unsafe branch).
    patched_script = tmp_path / "bootstrap-dept-patched.sh"
    original = (SCRIPTS_DIR / "bootstrap-dept.sh").read_text(encoding="utf-8")
    patched = original.replace(
        'CLONE_PARENT="${BUBBLE_BOOTSTRAP_CLONE_DIR:-/tmp}"',
        f'CLONE_PARENT="{clone_dir}"',
    )
    # Also strip the env-var fallback so the safety check fires.
    patched = patched.replace(
        'if [[ -n "${BUBBLE_BOOTSTRAP_CLONE_DIR:-}" ]]; then',
        'if false; then',
    )
    patched_script.write_text(patched, encoding="utf-8")
    patched_script.chmod(0o755)

    env = os.environ.copy()
    env.pop("BUBBLE_BOOTSTRAP_CLONE_DIR", None)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    # Try the dry-run twice. The second run should fail with the safety
    # message because the target dir is non-tmp + env var is unset.
    args = ["bash", str(patched_script),
            "--slug=canary", "--display-name=Canary", "--owner=joris", "--dry-run"]
    res2 = subprocess.run(args, env=env, capture_output=True, text=True)

    assert res2.returncode != 0, (
        "expected exit != 0 (refuse to wipe non-/tmp/ target without env var), "
        f"got {res2.returncode}\nSTDOUT: {res2.stdout}\nSTDERR: {res2.stderr}"
    )
    assert "refusing to wipe" in res2.stderr or "refusing to wipe" in res2.stdout, (
        f"expected the safety message; got STDERR={res2.stderr}"
    )

    # The original dummy.txt must STILL exist (we refused to wipe)
    assert (clone_dir / "bubble-ops-canary" / "dummy.txt").exists(), (
        "defensive wipe-refusal failed — content was deleted anyway"
    )


# ---------------------------------------------------------------------------
# G-1 — Layer-4 recurring mission in templates (TDD: RED first, then GREEN)
# ---------------------------------------------------------------------------

def test_ops_dept_yaml_has_daily_risk_audit_mission(tmp_path: Path):
    """G-1: Ops dept.yaml.draft must include a layer:4 cadence:daily mission."""
    root = _scaffold_ops(tmp_path)
    doc = yaml.safe_load((root / "dept.yaml.draft").read_text(encoding="utf-8"))

    missions = doc.get("recurring_missions", [])
    layer4_daily = [
        m for m in missions
        if m.get("layer") == 4 and m.get("cadence") == "daily"
    ]
    assert layer4_daily, (
        f"Ops dept.yaml.draft must have at least one layer:4 cadence:daily "
        f"recurring mission (daily_risk_audit). Got: {missions}"
    )
    ids = [m.get("id") for m in layer4_daily]
    assert "daily_risk_audit" in ids, (
        f"Expected 'daily_risk_audit' mission id in layer:4 missions. Got ids: {ids}"
    )
    # Verify the canonical time field
    mission = next(m for m in layer4_daily if m.get("id") == "daily_risk_audit")
    assert mission.get("time") == "22:00", (
        f"daily_risk_audit must have time='22:00' (UTC). Got: {mission.get('time')!r}"
    )


def test_management_dept_yaml_has_daily_risk_audit_mission(tmp_path: Path):
    """G-1: Management dept.yaml.draft must also include a layer:4 cadence:daily mission."""
    root = _scaffold_management(tmp_path)
    doc = yaml.safe_load((root / "dept.yaml.draft").read_text(encoding="utf-8"))

    missions = doc.get("recurring_missions", [])
    layer4_daily = [
        m for m in missions
        if m.get("layer") == 4 and m.get("cadence") == "daily"
    ]
    assert layer4_daily, (
        f"Management dept.yaml.draft must have at least one layer:4 cadence:daily "
        f"recurring mission (daily_risk_audit). Got: {missions}"
    )
    ids = [m.get("id") for m in layer4_daily]
    assert "daily_risk_audit" in ids, (
        f"Expected 'daily_risk_audit' mission id in layer:4 missions. Got ids: {ids}"
    )
    mission = next(m for m in layer4_daily if m.get("id") == "daily_risk_audit")
    assert mission.get("time") == "22:00", (
        f"daily_risk_audit must have time='22:00' (UTC). Got: {mission.get('time')!r}"
    )


# ---------------------------------------------------------------------------
# G-2 — CLAUDE.md STEP C cadence check (TDD: RED first, then GREEN)
# ---------------------------------------------------------------------------

def test_ops_claude_md_has_step_c_layer4_time_window(tmp_path: Path):
    """G-2: Ops CLAUDE.md must wire Step C to the canonical dispatch helper (PR #47).

    #47 moved the Layer-4 timing OUT of literal CLAUDE.md prose and INTO
    `decide_dispatch` (the single fleet-wide source of truth for *when* each layer
    fires), so the dept never hand-rolls the schedule. The CLAUDE.md must therefore
    delegate to the helper and reference the L4 eligibility window (19:00 Paris),
    rather than hardcode a literal '22:00' branch (the pre-#47 anti-pattern).
    """
    root = _scaffold_ops(tmp_path)
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")

    assert "decide_dispatch" in text, (
        "Ops CLAUDE.md must delegate Step C to the canonical decide_dispatch helper (PR #47)"
    )
    assert "19:00 Paris" in text, (
        "Ops CLAUDE.md must reference the Layer-4 eligibility window (19:00 Paris)"
    )
    assert "Layer 4" in text or "l4" in text.lower(), (
        "Ops CLAUDE.md must mention Layer 4 in the dispatch description"
    )


def test_management_claude_md_has_step_c_layer4_time_window(tmp_path: Path):
    """G-2: Management CLAUDE.md must contain the explicit 22:00 UTC Layer-4 dispatch branch."""
    root = _scaffold_management(tmp_path)
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")

    assert "22:00" in text, (
        "Management CLAUDE.md must mention '22:00' (UTC dispatch window for Layer 4)"
    )
    assert ".last-run" in text, (
        "Management CLAUDE.md must mention '.last-run' (idempotency guard for Layer-4 dispatch)"
    )
    assert "Layer 4" in text or "layer 4" in text.lower(), (
        "Management CLAUDE.md must mention 'Layer 4' in the dispatch branch"
    )


# ---------------------------------------------------------------------------
# Broker policy render (2026-06-05: closes the hand-copy drift that lost
# WORKING_MEMORY.md from maya/tony allow-lists and 403'd their pushes).
# ---------------------------------------------------------------------------


def test_scaffold_writes_broker_policy_ops(tmp_path: Path):
    """An ops-leaf scaffold writes deploy/policies/<slug>-policy.yaml rendered
    from the canonical leaf template — WORKING_MEMORY.md present, no stray
    placeholders, valid YAML."""
    root = _scaffold_ops(tmp_path)
    pol = root / "deploy" / "policies" / "smoke-policy.yaml"
    assert pol.is_file(), "broker policy not written into the dept tree"
    ga = yaml.safe_load(pol.read_text(encoding="utf-8"))["github_access"]
    assert ga["actor"] == "ops-loop-smoke"
    assert ga["own_repo"] == "bubble-ops-smoke"
    allowed = ga["write"][0]["allowed_paths"]
    assert "WORKING_MEMORY.md" in allowed, (
        "WORKING_MEMORY.md MUST be in allowed_paths — its absence was the "
        "2026-06-05 push-block"
    )
    # leaf depts never open cross-dept PRs
    assert ga["pull_requests"]["can_open_to"] == []


def test_scaffold_writes_broker_policy_management_with_children(tmp_path: Path):
    """A management scaffold renders the management template: every child is
    expanded into read: and pull_requests.can_open_to:, target_paths intact,
    WORKING_MEMORY.md present."""
    root = _scaffold_management(tmp_path)
    pol = root / "deploy" / "policies" / "tony-policy.yaml"
    assert pol.is_file(), "management broker policy not written"
    ga = yaml.safe_load(pol.read_text(encoding="utf-8"))["github_access"]
    child_repos = [f"bubble-ops-{c}" for c in MANAGEMENT_CHILDREN]
    # read: = own + shared-wiki + every child (order: own, wiki, then children)
    assert ga["read"] == ["bubble-ops-tony", "bubble-shared-wiki"] + child_repos
    # can_open_to: = exactly the children
    assert ga["pull_requests"]["can_open_to"] == child_repos
    # the priority-directive path is preserved
    assert ga["pull_requests"]["target_paths"] == ["queues/management/**"]
    assert "WORKING_MEMORY.md" in ga["write"][0]["allowed_paths"]


def test_render_broker_policy_management_without_children_raises():
    """A management dept with no children is a config error (it would render a
    policy with empty read/can_open_to child lists)."""
    with pytest.raises(ValueError):
        scaffold.render_broker_policy("x", level="management", children=[])


def test_broker_policy_has_no_unrendered_active_placeholders(tmp_path: Path):
    """No active (non-comment) line in either rendered policy may keep a
    <DEPT_SLUG>/<CHILD_SLUG_N> placeholder."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for root, name in (
        (_scaffold_ops(tmp_path / "a"), "smoke-policy.yaml"),
        (_scaffold_management(tmp_path / "b"), "tony-policy.yaml"),
    ):
        text = (root / "deploy" / "policies" / name).read_text(encoding="utf-8")
        for ln in text.splitlines():
            if ln.lstrip().startswith("#"):
                continue
            assert "<DEPT_SLUG>" not in ln and "<CHILD_SLUG_" not in ln, (
                f"unrendered placeholder in active line: {ln!r}"
            )
