"""The cockpit must read post-isolation agents from their canonical root."""
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


def test_canonical_unprefixed_departments_and_compat_symlinks_are_deduped(
        monkeypatch, tmp_path):
    from console import settings
    from console.services import dept_registry

    ben = _make_dept(tmp_path, "ben")
    _make_dept(tmp_path, "tony")
    (tmp_path / "bubble-ops-ben").symlink_to(ben, target_is_directory=True)
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))

    depts = dept_registry.list_departments()

    assert [d.slug for d in depts] == ["ben", "tony"]
    assert dept_registry.repo_path("ben").resolve() == ben.resolve()


def test_production_unit_uses_canonical_agent_root():
    template = (Path(__file__).resolve().parents[1] / "deploy" /
                "bubble-ops-console.service.template").read_text()

    assert 'Environment="READ_FROM_DISK=/srv/agents"' in template
    assert 'Environment="READ_FROM_DISK=/home/claude/agents"' not in template
