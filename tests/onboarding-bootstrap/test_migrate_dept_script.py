"""
Sprint H+I Fix 7 — migrate-dept.sh for brownfield depts.

bootstrap-dept.sh is for greenfield depts. Brownfield depts (Maya
already exists at ~/claude-workspaces/Maya_Sales) have an existing
config.yaml + CLAUDE.md + skills/ tree. We need a parallel command
that does the same scaffolding but pre-seeds with what we already know.

migrate-dept.sh:
  1. Takes --source=<path> + --slug=<slug>
  2. Creates bubble-ops-<slug>/ in the clone parent dir
  3. Ingests source's config.yaml -> populates dept.yaml.draft fields
  4. Ingests source's CLAUDE.md -> preserves it as MANDATE.md base
  5. Pre-seeds onboarding/STATE.yaml with status=Drafting and
     validated_steps=[mandate, missions] (these are reasonably inferrable)
  6. Emits a summary: mapped X fields, M fields need operator review
  7. Idempotent: re-running on existing target fails clearly
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "migrate-dept.sh"


@pytest.fixture
def fake_maya_source(tmp_path: Path) -> Path:
    """Fabricate a Maya-like source workspace with config.yaml + CLAUDE.md."""
    src = tmp_path / "Maya_Sales"
    src.mkdir(parents=True)
    (src / "config.yaml").write_text(
        textwrap.dedent("""\
            # Maya — agent configuration
            backend: notion
            notion:
              pool_database_id: "593366d4-e2ed-48de-af51-224582500c05"
            account_used_default: "Joris"
            quotas:
              daily_drafts: 5
            """),
        encoding="utf-8",
    )
    (src / "CLAUDE.md").write_text(
        textwrap.dedent("""\
            # Maya Workspace

            Mindset : Tu es la dept-manager prospection de Bubble Invest.

            ## Qui est Maya

            Maya pilote l'outreach LinkedIn de bout en bout — discovery,
            research, scoring, warming, draft, follow-up.

            ## Filesystem map
            ~/claude-workspaces/Maya_Sales/
            """),
        encoding="utf-8",
    )
    return src


@pytest.fixture
def run_migrate(tmp_clone_dir: Path):
    """Callable: run migrate-dept.sh with the given source + slug."""
    def _run(source: Path, slug: str, display_name: str = "Maya",
             owner: str = "joris", expect_fail: bool = False, extra_args=None):
        env = os.environ.copy()
        env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
        args = [
            "bash", str(SCRIPT),
            f"--source={source}",
            f"--slug={slug}",
            f"--display-name={display_name}",
            f"--owner={owner}",
        ]
        if extra_args:
            args.extend(extra_args)
        res = subprocess.run(args, env=env, capture_output=True, text=True)
        if not expect_fail and res.returncode != 0:
            raise AssertionError(
                f"migrate-dept.sh failed: rc={res.returncode}\n"
                f"stdout={res.stdout}\nstderr={res.stderr}"
            )
        return res
    return _run


# ---------------------------------------------------------------------------
# Existence + basic exec
# ---------------------------------------------------------------------------

def test_migrate_script_exists_and_executable() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_migrate_fails_on_missing_source(run_migrate, tmp_path: Path) -> None:
    res = run_migrate(
        source=tmp_path / "does_not_exist",
        slug="ghost",
        display_name="Ghost",
        expect_fail=True,
    )
    assert res.returncode != 0
    combined = (res.stdout + res.stderr).lower()
    assert "source" in combined or "does not exist" in combined or \
        "introuvable" in combined, (
        f"failure must mention missing source; got:\n{combined}"
    )


def test_migrate_fails_when_source_missing_config_yaml(
    run_migrate, tmp_path: Path,
) -> None:
    src = tmp_path / "broken_source"
    src.mkdir()
    (src / "CLAUDE.md").write_text("# orphan", encoding="utf-8")
    # No config.yaml.
    res = run_migrate(source=src, slug="broken", expect_fail=True)
    assert res.returncode != 0
    combined = (res.stdout + res.stderr).lower()
    assert "config.yaml" in combined, (
        f"failure must mention missing config.yaml; got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Happy path — ingest config.yaml + CLAUDE.md
# ---------------------------------------------------------------------------

def test_migrate_creates_target_and_ingests_config(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    res = run_migrate(source=fake_maya_source, slug="maya")
    target = tmp_clone_dir / "bubble-ops-maya"
    assert target.exists(), f"target dir missing: {target}"
    assert (target / "dept.yaml.draft").exists()
    assert (target / "MANDATE.md").exists()
    assert (target / "onboarding" / "STATE.yaml").exists()
    assert (target / "CLAUDE.md").exists()
    # The migrate output must mention the source.
    assert str(fake_maya_source) in (res.stdout + res.stderr)


def test_migrate_dept_yaml_inherits_mandate_from_claude_md(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """dept.yaml.draft::department.mandate should be pre-seeded with a
    sentence extracted/inferred from the source CLAUDE.md."""
    run_migrate(source=fake_maya_source, slug="maya")
    target = tmp_clone_dir / "bubble-ops-maya"
    doc = yaml.safe_load((target / "dept.yaml.draft").read_text(encoding="utf-8"))
    mandate = doc["department"]["mandate"]
    # Must be a non-trivial sentence — neither the bare placeholder nor empty.
    assert len(mandate) >= 10, f"mandate too short: {mandate!r}"
    assert "TBD-by-operator" not in mandate, (
        f"mandate must be inherited, not the bootstrap placeholder; got: {mandate!r}"
    )


def test_migrate_state_yaml_pre_validates_inferrable_steps(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """The pre-seeded STATE.yaml must have validated_steps populated for
    the steps we can reasonably infer from the existing source
    (mandate, missions). Status should be Drafting (further than Idea
    since we have a mandate)."""
    run_migrate(source=fake_maya_source, slug="maya")
    target = tmp_clone_dir / "bubble-ops-maya"
    state = yaml.safe_load(
        (target / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    )
    validated = state.get("validated_steps", [])
    assert "mandate" in validated, (
        f"mandate must be pre-validated for a brownfield migration; got: {validated}"
    )
    # Status must be at least Configuring (post-mandate).
    assert state["status"] in {"Configuring", "Drafting", "Needs validation"}, (
        f"status must reflect pre-validated mandate; got: {state['status']}"
    )


def test_migrate_preserves_source_claude_md_as_mandate_md(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """The original CLAUDE.md content must survive somewhere — typically
    as the body of MANDATE.md so the operator can review what the
    inheritance produced."""
    run_migrate(source=fake_maya_source, slug="maya")
    target = tmp_clone_dir / "bubble-ops-maya"
    mandate_md = (target / "MANDATE.md").read_text(encoding="utf-8")
    # Must contain a sentence that was in the source CLAUDE.md.
    assert "prospection" in mandate_md.lower() or \
        "outreach" in mandate_md.lower() or \
        "linkedin" in mandate_md.lower(), (
        f"MANDATE.md must inherit content from source CLAUDE.md; got:\n{mandate_md}"
    )


def test_migrate_target_claude_md_has_auto_driving_header(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """The NEW CLAUDE.md in the target must still have the auto-driving
    prefix (auto-eclosure instructions) so the agent boots in the right
    mode — even though we inherited content from the source."""
    run_migrate(source=fake_maya_source, slug="maya")
    target = tmp_clone_dir / "bubble-ops-maya"
    text = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "department-onboarding-guide" in text, (
        "target CLAUDE.md must include the auto-driving header"
    )
    assert "autonom" in text.lower(), (
        "target CLAUDE.md must declare autonomy"
    )


# ---------------------------------------------------------------------------
# Summary report — mapped fields + unmapped fields
# ---------------------------------------------------------------------------

def test_migrate_emits_mapping_report(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """The output must summarize: mapped X fields from Y; M fields need
    operator review."""
    res = run_migrate(source=fake_maya_source, slug="maya")
    combined = res.stdout + res.stderr
    low = combined.lower()
    assert "mapped" in low or "mappé" in low or "champs" in low, (
        f"output must include a mapping summary; got:\n{combined}"
    )
    # Should name specific source fields we couldn't map (e.g. notion db id,
    # account_used_default, quotas — none of these have a canonical home in
    # the bubble-ops dept.yaml shape yet).
    assert "notion" in low or "account_used_default" in low or \
        "quotas" in low or "review" in low or "non mappé" in low or \
        "unmapped" in low, (
        f"output must list unmapped source fields needing operator review; got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Idempotency — running twice fails (or no-ops cleanly)
# ---------------------------------------------------------------------------

def test_migrate_refuses_to_clobber_existing_target(
    run_migrate, fake_maya_source: Path, tmp_clone_dir: Path,
) -> None:
    """Running migrate twice for the same slug must refuse rather than
    overwrite the existing target."""
    run_migrate(source=fake_maya_source, slug="maya")
    # Second invocation must fail (refusal to overwrite).
    res = run_migrate(source=fake_maya_source, slug="maya", expect_fail=True)
    assert res.returncode != 0, (
        f"second migrate must refuse; got rc=0 (would have clobbered)\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    combined = (res.stdout + res.stderr).lower()
    assert "exist" in combined or "déjà" in combined or "refuse" in combined, (
        f"refusal message must mention existing target; got:\n{combined}"
    )
