"""The report must read post-isolation agents from their canonical root."""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_dept(root: Path, slug: str) -> Path:
    repo = root / slug
    repo.mkdir()
    (repo / "dept.yaml").write_text("department:\n  slug: " + slug + "\n")
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(yaml.safe_dump({
        "slug": slug,
        "display_name": slug.title(),
        "status": "Live",
        "validated_steps": ["mandate"],
    }))
    return repo


def test_report_prefers_canonical_workdir_without_moving_general_disk_root(
        monkeypatch, tmp_path):
    from console import settings
    from console.services import dept_registry

    legacy_root = tmp_path / "legacy"
    canonical_root = tmp_path / "canonical"
    legacy_root.mkdir()
    canonical_root.mkdir()
    legacy = _make_dept(legacy_root, "bubble-ops-ben")
    canonical = _make_dept(canonical_root, "ben")
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(legacy_root))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(canonical_root))

    assert dept_registry.repo_path("ben") == legacy.resolve()
    assert dept_registry.runtime_repo_path("ben") == canonical.resolve()


def test_report_falls_back_to_legacy_workdir(monkeypatch, tmp_path):
    from console import settings
    from console.services import dept_registry

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy = _make_dept(legacy_root, "bubble-ops-ben")
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(legacy_root))
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(tmp_path / "absent"))

    assert dept_registry.runtime_repo_path("ben") == legacy.resolve()
