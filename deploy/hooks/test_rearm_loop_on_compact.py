"""Tests for rearm-loop-on-compact.py SessionStart hook (#754).

Feeds the exact SessionStart stdin JSON + env and asserts the inject-file
re-arm decision. The hook re-arms ONLY when source is compact/resume AND
OPS_LOOP_BOOT_REARM=1 — so it can never fire in a human interactive session.

Run: python3 -m pytest deploy/hooks/test_rearm_loop_on_compact.py -q
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # bubble-ops-loop/
HOOK = REPO_ROOT / "deploy" / "hooks" / "rearm-loop-on-compact.py"


def _mk_inject(home, channel="socials", seed=None):
    """Create $HOME/.claude/channels/telegram-<channel>/inject; return its Path."""
    d = Path(home) / ".claude" / "channels" / f"telegram-{channel}"
    d.mkdir(parents=True, exist_ok=True)
    inject = d / "inject"
    if seed is not None:
        inject.write_text(seed)
    return inject


def _run(source, *, boot_rearm=None, home):
    """Invoke the hook with a SessionStart payload + env in a temp HOME.
    Path resolution is by GLOB of telegram-*/inject, so no dept env is passed.
    Returns exit_code (inspect the inject file yourself)."""
    env = dict(os.environ, HOME=str(home))
    env.pop("OPS_LOOP_BOOT_REARM", None)
    if boot_rearm is not None:
        env["OPS_LOOP_BOOT_REARM"] = boot_rearm
    payload = {"hook_event_name": "SessionStart", "source": source}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, check=False,
    )
    return proc.returncode


def test_compact_gated_on_appends_rearm(tmp_path):
    inject = _mk_inject(tmp_path, seed="")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    body = inject.read_text()
    assert "re-arm your /loop" in body.lower()
    assert body.endswith("\n")


def test_no_env_never_fires(tmp_path):
    # Human interactive /compact: OPS_LOOP_BOOT_REARM unset -> inject untouched.
    inject = _mk_inject(tmp_path, seed="")
    code = _run("compact", boot_rearm=None, home=tmp_path)
    assert code == 0
    assert inject.read_text() == ""


def test_startup_source_does_not_fire(tmp_path):
    # boot_rearm.ts already covers startup; this hook is compact/resume only.
    inject = _mk_inject(tmp_path, seed="")
    code = _run("startup", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert inject.read_text() == ""


def test_resume_source_fires(tmp_path):
    inject = _mk_inject(tmp_path, seed="")
    code = _run("resume", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert "re-arm your /loop" in inject.read_text().lower()


def test_non_empty_inject_no_double_append(tmp_path):
    inject = _mk_inject(tmp_path, seed="pending prior turn\n")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert inject.read_text() == "pending prior turn\n"  # untouched, no stacking


def test_no_channel_dir_no_op(tmp_path):
    # No telegram-*/inject exists at all -> fail-closed, exit 0, nothing created.
    (tmp_path / ".claude" / "channels").mkdir(parents=True, exist_ok=True)
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert not list((tmp_path / ".claude" / "channels").glob("telegram-*/inject"))


def test_ambiguous_multiple_channels_no_op(tmp_path):
    # Two telegram-*/inject dirs -> ambiguous -> fail-closed, neither touched.
    a = _mk_inject(tmp_path, channel="socials", seed="")
    b = _mk_inject(tmp_path, channel="other", seed="")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert a.read_text() == "" and b.read_text() == ""


def test_malformed_stdin_exits_zero(tmp_path):
    _mk_inject(tmp_path, seed="")
    env = dict(os.environ, HOME=str(tmp_path), OPS_LOOP_BOOT_REARM="1")
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0  # never break session start


def test_valid_json_non_dict_payload_exits_zero(tmp_path):
    # Valid JSON that parses to a non-dict (list, null) must not crash on .get().
    # A SessionStart hook that exits non-zero breaks startup for EVERY agent.
    inject = _mk_inject(tmp_path, seed="")
    env = dict(os.environ, HOME=str(tmp_path), OPS_LOOP_BOOT_REARM="1")
    for body in ("[1,2,3]", "null", '"just a string"', "42"):
        proc = subprocess.run([sys.executable, str(HOOK)], input=body,
                              capture_output=True, text=True, env=env, check=False)
        assert proc.returncode == 0, f"non-dict payload {body!r} broke exit 0"
        assert inject.read_text() == "", f"non-dict payload {body!r} wrongly re-armed"


def test_rearm_text_shares_boot_rearm_core_phrasing():
    """The compact hook and boot_rearm.ts must give the agent the SAME instruction
    so the two re-arm paths don't drift. Assert the load-bearing phrases match."""
    hook_src = HOOK.read_text()
    boot = (REPO_ROOT / "deploy" / "telegram-plugin" / "boot_rearm.ts").read_text()
    for phrase in [
        "re-arm your /loop, SELF-PACED",
        "run CronList first and",
        "NEVER a bare slash-command",
        "Never hardcode an hourly cron",
        "floor timers remain the safety net",
    ]:
        assert phrase in hook_src, f"hook missing shared phrase: {phrase}"
        assert phrase in boot, f"boot_rearm missing shared phrase: {phrase}"
