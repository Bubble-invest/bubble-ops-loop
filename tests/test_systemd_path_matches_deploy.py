"""
test_systemd_path_matches_deploy.py — Sprint H+I Fix 1, extended by board #1120.

The systemd unit template declares a WorkingDirectory= path that MUST
match the path where scripts/deploy-to-morty.sh clones the dept repo
on Morty. Otherwise the service would start in a directory that has
no repo and crash-loop.

Board #1120 (per-dept OS-user isolation) made this path OS_USER-dependent:
the template now declares `WorkingDirectory=${WORKDIR}` (a placeholder,
substituted by deploy-to-morty.sh) rather than a hardcoded literal, because
a per-dept-user dept's workdir lives at /srv/agents/<slug> (decoupled from
any home dir) while the LEGACY (os_user=claude) convention stays
/home/claude/agents/<slug> — unchanged from before #1120.

So this file now asserts the invariant BEHAVIOURALLY (render the script's
--dry-run output and check WorkingDirectory / REMOTE_REPO_PATH / the clone
target always agree) instead of via a single static literal, for BOTH the
legacy default and a per-dept-user override — and separately anchors that
the LEGACY default is still byte-identical to the pre-#1120 convention.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.scaffold import render_systemd_unit  # noqa: E402

TEMPLATE = PROJECT_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy-to-morty.sh"

LEGACY_CANONICAL_REPO_PATH = "/home/claude/agents/eliot"
PER_USER_CANONICAL_REPO_PATH = "/srv/agents/eliot"


def _extract_working_directory(template_text: str) -> str:
    for line in template_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WorkingDirectory="):
            return stripped.split("=", 1)[1].strip()
    raise AssertionError("template has no WorkingDirectory= directive")


def _dry_run(*extra_args: str) -> str:
    res = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--slug=eliot", "--remote=claude@morty", "--dry-run", *extra_args],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"dry-run failed: {res.stderr}"
    return res.stdout + res.stderr


def test_template_working_directory_is_the_os_user_aware_placeholder():
    """Board #1120: WorkingDirectory= must be the ${WORKDIR} placeholder
    (substituted per-dept by deploy-to-morty.sh), not a hardcoded literal —
    a hardcoded literal cannot vary by --os-user."""
    template_text = TEMPLATE.read_text(encoding="utf-8")
    working_dir = _extract_working_directory(template_text)
    assert working_dir == "${WORKDIR}", (
        f"WorkingDirectory= must be the ${{WORKDIR}} placeholder, got: {working_dir}"
    )


def test_legacy_default_working_directory_unchanged():
    """No --os-user flag -> WorkingDirectory stays the pre-#1120 canonical
    /home/claude/agents/<slug> path (byte-identical legacy behaviour)."""
    combined = _dry_run()
    assert f"WorkingDirectory={LEGACY_CANONICAL_REPO_PATH}" in combined
    assert f"test -d {LEGACY_CANONICAL_REPO_PATH}" in combined
    assert f"git clone https://github.com/vdk888/bubble-ops-eliot {LEGACY_CANONICAL_REPO_PATH}" in combined


def test_onboarding_scaffold_renders_all_os_user_placeholders_to_legacy_defaults():
    """The bootstrap scaffold is a second template consumer.  New departments
    stay on the legacy user until an explicit per-user deploy migration."""
    rendered = render_systemd_unit("eliot")
    assert "User=claude" in rendered
    assert "Group=claude" in rendered
    assert f"WorkingDirectory={LEGACY_CANONICAL_REPO_PATH}" in rendered
    for placeholder in ("${OS_USER}", "${OS_GROUP}", "${WORKDIR}"):
        assert placeholder not in rendered


def test_per_user_working_directory_moves_to_srv_agents():
    """--os-user=agent-eliot -> WorkingDirectory + clone target both move to
    /srv/agents/<slug> (decoupled from any home dir), and both still agree."""
    combined = _dry_run("--os-user=agent-eliot")
    assert f"WorkingDirectory={PER_USER_CANONICAL_REPO_PATH}" in combined
    assert f"test -d {PER_USER_CANONICAL_REPO_PATH}" in combined
    assert f"git clone https://github.com/vdk888/bubble-ops-eliot {PER_USER_CANONICAL_REPO_PATH}" in combined
    # The literal legacy path (with THIS slug substituted in) must not appear
    # anywhere in a per-user render's action lines (WorkingDirectory=, the
    # clone/chown commands). The template's own doc-comment header uses the
    # generic "<slug>" placeholder text, not the substituted slug, so it can
    # never collide with this check.
    assert "/home/claude/agents/eliot" not in combined


def test_working_directory_and_clone_target_never_drift_legacy():
    combined = _dry_run()
    match_wd = re.search(r"WorkingDirectory=(\S+)", combined)
    match_clone = re.search(r"test -d (\S+) \|\|", combined)
    assert match_wd and match_clone
    assert match_wd.group(1) == match_clone.group(1), (
        "WorkingDirectory and the deploy clone target drifted apart:\n"
        f"  WorkingDirectory= {match_wd.group(1)}\n"
        f"  clone target      {match_clone.group(1)}"
    )


def test_working_directory_and_clone_target_never_drift_per_user():
    combined = _dry_run("--os-user=agent-eliot")
    match_wd = re.search(r"WorkingDirectory=(\S+)", combined)
    match_clone = re.search(r"test -d (\S+) \|\|", combined)
    assert match_wd and match_clone
    assert match_wd.group(1) == match_clone.group(1), (
        "WorkingDirectory and the deploy clone target drifted apart:\n"
        f"  WorkingDirectory= {match_wd.group(1)}\n"
        f"  clone target      {match_clone.group(1)}"
    )
