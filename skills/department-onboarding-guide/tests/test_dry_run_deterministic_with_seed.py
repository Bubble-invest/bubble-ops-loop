"""
test_dry_run_deterministic_with_seed.py — UX-4

Two runs with the same seed MUST produce byte-identical artifacts. This
guarantees test repeatability + reproducible CI gates.
"""
from __future__ import annotations

import hashlib

from skill_lib.dry_run import run_dry_run_full


def _hash_dir(p):
    """Hash all regular files under p deterministically by relative path."""
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            # We exclude the ts-bearing .last-run sentinel only — everything else
            # must be byte-identical between runs given the same seed.
            rel = f.relative_to(p)
            if rel.name == ".last-run":
                continue
            h.update(str(rel).encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
    return h.hexdigest()


def test_same_seed_produces_same_artifacts(tmp_dept_repo, stub_agent_context, tmp_path):
    ctx = stub_agent_context("step6_dry_run")
    # Use a separate dept root for the second run so paths don't collide.
    other_root = tmp_path / "dept-repo-2"
    for sub in [
        "queues/research", "queues/management", "outputs", "inbox/decisions",
        "missions", "layers/1", "layers/2", "layers/3", "layers/4",
        "skills", "tools", "tests",
    ]:
        (other_root / sub).mkdir(parents=True, exist_ok=True)
    (other_root / ".git").mkdir(exist_ok=True)

    r1 = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    r2 = run_dry_run_full(
        dept_root=other_root,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )

    # Compare the per-step artifact tree byte-for-byte (excluding .last-run).
    h1 = _hash_dir(r1.artifacts_dir)
    h2 = _hash_dir(r2.artifacts_dir)
    assert h1 == h2, "byte-identical artifacts required for the same seed"
