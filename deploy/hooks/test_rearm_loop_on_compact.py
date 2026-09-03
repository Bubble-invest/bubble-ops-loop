"""Tests for rearm-loop-on-compact.py SessionStart hook (#754).

Feeds the exact SessionStart stdin JSON + env and asserts the inject-file
re-arm decision. The hook re-arms ONLY when source is compact/resume AND
OPS_LOOP_BOOT_REARM=1 — so it can never fire in a human interactive session.

Run: python3 -m pytest deploy/hooks/test_rearm_loop_on_compact.py -q
"""
from __future__ import annotations
import importlib.util, json, os, subprocess, sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # bubble-ops-loop/
HOOK = REPO_ROOT / "deploy" / "hooks" / "rearm-loop-on-compact.py"


def _load_hook_module():
    """Load the hyphen-named hook as a module so tests share its REARM_TURN /
    REARM_SENTINEL constants instead of duplicating the wording."""
    spec = importlib.util.spec_from_file_location("rearm_loop_on_compact", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HOOK_MOD = _load_hook_module()
REARM_TURN = _HOOK_MOD.REARM_TURN
REARM_SENTINEL = _HOOK_MOD.REARM_SENTINEL


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


def test_unrelated_pending_turn_still_rearms(tmp_path):
    # #754 durable fix: an UNRELATED un-drained turn (e.g. a cross-agent inject)
    # must NOT suppress the re-arm. The old "getsize>0 -> skip" guard silently
    # dropped the re-arm here, leaving the loop dormant after a compact. Now the
    # re-arm is appended on its own line, preserving the pending turn.
    inject = _mk_inject(tmp_path, seed="pending prior turn\n")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    body = inject.read_text()
    assert body.startswith("pending prior turn\n")   # unrelated turn preserved
    assert "re-arm your /loop" in body.lower()        # re-arm still delivered
    assert body.count("re-arm your /loop, SELF-PACED") == 1


def test_pending_turn_no_trailing_newline_gets_separator(tmp_path):
    # A pending turn without a trailing newline must not fuse into the re-arm line.
    inject = _mk_inject(tmp_path, seed="half a turn")  # no newline
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    lines = inject.read_text().splitlines()
    assert lines[0] == "half a turn"                  # its own line
    assert lines[1].startswith("[compact] Context was compacted")


def test_already_queued_rearm_no_double(tmp_path):
    # If a re-arm is ALREADY queued (this hook's own, boot_rearm's, or a manual
    # inject sharing the wording), we skip — never stack two re-arms.
    inject = _mk_inject(tmp_path, seed=REARM_TURN + "\n")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    assert inject.read_text().count("re-arm your /loop, SELF-PACED") == 1


def test_compact_boundary_rearms_across_repeated_compacts(tmp_path):
    # Model the compact boundary end-to-end and assert re-arm survives it every
    # time: compact -> re-arm queued -> plugin DRAINS it (file emptied) -> the
    # NEXT compact re-arms again. A loop that re-arms once but not on the second
    # compact would still go dormant; this guards that.
    inject = _mk_inject(tmp_path, seed="")
    for _ in range(3):
        code = _run("compact", boot_rearm="1", home=tmp_path)
        assert code == 0
        body = inject.read_text()
        assert body.count("re-arm your /loop, SELF-PACED") == 1, "exactly one re-arm queued"
        # Simulate the telegram plugin file-watcher draining the turn.
        inject.write_text("")


def test_fire_writes_audit_log(tmp_path):
    # A REAL /compact must leave greppable proof the hook fired — the #754
    # acceptance blocker ("no clear auto-fire evidence").
    inject = _mk_inject(tmp_path, seed="")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    log = inject.parent / "rearm-on-compact.log"
    assert log.exists()
    entry = log.read_text()
    assert "decision=fired" in entry
    assert "source=compact" in entry


def test_skip_already_queued_is_audited(tmp_path):
    inject = _mk_inject(tmp_path, seed=REARM_TURN + "\n")
    code = _run("compact", boot_rearm="1", home=tmp_path)
    assert code == 0
    log = inject.parent / "rearm-on-compact.log"
    assert log.exists()
    assert "decision=skip:already-queued" in log.read_text()


def test_no_env_writes_no_audit(tmp_path):
    # A human interactive /compact (no OPS_LOOP_BOOT_REARM) must be a pure no-op:
    # inject untouched AND no audit line (no side effects in a human session).
    inject = _mk_inject(tmp_path, seed="")
    code = _run("compact", boot_rearm=None, home=tmp_path)
    assert code == 0
    assert inject.read_text() == ""
    assert not (inject.parent / "rearm-on-compact.log").exists()


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
