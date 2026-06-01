"""
Sprint H+I Fix 4 — Telegram bot handle validation.

Telegram bot usernames are global + must be ≤ 32 chars (BotFather hard
limit). The convention `bubbleops<slug-no-dashes>_bot` produces:

  len('bubbleops') + len(slug_no_dashes) + len('_bot') = 13 + slug

So slug-no-dashes length must be ≤ 19 chars.

  - slug-no-dashes 19 chars  -> handle = 32 chars  -> OK + warning
  - slug-no-dashes 20+ chars -> handle > 32        -> FAIL fast at bootstrap

Plus: even when length is OK, the operator must be warned that handles
are globally unique on Telegram and the BotFather pick may collide.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_bootstrap_dry_run_fails_when_slug_produces_too_long_handle(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """A slug whose compact form is > 19 chars MUST cause bootstrap to
    fail with a clear error mentioning the 32-char limit + suggesting
    a shorter slug."""
    # 20 alpha chars compact form -> handle = 'bubbleops' + 20 + '_bot' = 33.
    slug = "averyverylongdeptname"   # 21 chars, compact = 21 -> 13+21 = 34 > 32
    script = scripts_dir / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
    res = subprocess.run(
        [
            "bash", str(script),
            f"--slug={slug}",
            "--display-name=AVeryLongName",
            "--owner=joris",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode != 0, (
        f"bootstrap must fail for over-long slug; got rc=0\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    combined = (res.stdout + res.stderr).lower()
    assert "32" in combined, (
        f"failure message must cite the 32-char Telegram limit; got:\n{combined}"
    )
    assert "shorter" in combined or "raccourcir" in combined or "trop long" in combined, (
        f"failure message must suggest a shorter slug; got:\n{combined}"
    )


def test_bootstrap_dry_run_warns_about_global_uniqueness_when_handle_ok(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """A slug that produces a fit-within-limits handle still emits a
    one-liner warning that Telegram handles are globally unique."""
    slug = "miranda"   # compact = 'miranda' (7) -> handle = 20 chars, well under 32
    script = scripts_dir / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
    res = subprocess.run(
        [
            "bash", str(script),
            f"--slug={slug}",
            "--display-name=Miranda",
            "--owner=joris",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"bootstrap should succeed for normal slug; got rc={res.returncode}\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    combined = (res.stdout + res.stderr).lower()
    assert "global" in combined or "uniqueness" in combined or "déjà pris" in combined \
        or "already taken" in combined or "globally" in combined, (
        f"output must warn about global handle uniqueness; got:\n{combined}"
    )


def test_bootstrap_dry_run_succeeds_at_exact_limit_handle(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """A slug whose compact form is exactly 19 chars -> handle = 32 chars
    exactly should succeed (boundary check)."""
    # 19 alpha-num chars compact form.
    slug = "abcdefghijklmnopqrs"   # 19 chars, no dashes
    assert len(slug.replace("-", "")) == 19
    expected_handle = f"bubbleops{slug}_bot"
    assert len(expected_handle) == 32

    script = scripts_dir / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
    res = subprocess.run(
        [
            "bash", str(script),
            f"--slug={slug}",
            "--display-name=Boundary",
            "--owner=joris",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"bootstrap should succeed at exact 32-char boundary; got rc={res.returncode}\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )


def test_bootstrap_dry_run_fails_one_char_over_limit(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """One char over the 32-char limit must fail."""
    slug = "abcdefghijklmnopqrst"   # 20 chars, no dashes -> handle = 33 chars
    expected_handle = f"bubbleops{slug}_bot"
    assert len(expected_handle) == 33

    script = scripts_dir / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
    res = subprocess.run(
        [
            "bash", str(script),
            f"--slug={slug}",
            "--display-name=OneOver",
            "--owner=joris",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode != 0, (
        f"bootstrap must fail one char over limit; got rc=0\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
