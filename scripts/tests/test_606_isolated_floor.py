from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy" / "templates"
RUNNER = (ROOT / "scripts" / "loop-backup.sh").read_text()
TICK_LOCK = (ROOT / "scripts" / "tick-lock.sh").read_text()


def test_each_layer_has_per_department_service_and_timer_contract():
    schedules = {1: "09:00:00", 2: "14:00:00", 3: "18:00:00", 4: "21:00:00"}
    for layer, clock in schedules.items():
        service = (TEMPLATES / f"loop-layer{layer}@.service").read_text()
        timer = (TEMPLATES / f"loop-layer{layer}@.timer").read_text()
        assert "User=agent-%i" in service
        assert "Group=agent-%i" in service
        assert "WorkingDirectory=/srv/agents/%i" in service
        assert "Environment=HOME=/home/agent-%i" in service
        assert "EnvironmentFile=-/run/bubble-agent-%i/env" in service
        assert "BUBBLE_BACKUP_LOCK_DIR=/run/bubble-agent-%i" in service
        assert "BUBBLE_OPS_LOOP_ROOT=/opt/bubble-ops-loop" in service
        assert "BUBBLE_BACKUP_LOG=/srv/agents/%i/state/loop-backup.jsonl" in service
        assert "BUBBLE_DISPATCH_DIRECTIVES=remote" in service
        assert "BUBBLE_AUTORESTART=0" in service
        assert "BUBBLE_BACKUP_INJECT_ONLY_DEPTS=maya" in service
        assert f"--layer {layer} --dept %i" in service
        assert "User=root" not in service
        assert "sudo" not in service
        assert f"Unit=loop-layer{layer}@%i.service" in timer
        assert f"OnCalendar=*-*-* {clock} Europe/Paris" in timer
        assert "RandomizedDelaySec=120" in timer
        assert "Persistent=true" in timer


def test_runner_never_shell_loads_dotenv_or_hardcodes_shared_identity():
    assert '&& . "$envfile"' not in RUNNER
    assert 'source "$envfile"' not in RUNNER
    assert 'eval "$envfile"' not in RUNNER
    assert "--dept" in RUNNER
    assert "must run as ${expected_user}" in RUNNER
    assert "per-dept auth environment missing" in RUNNER
    assert "dispatch: delegated to Tony" in RUNNER
    assert "--remote-delivery" in RUNNER
    assert "wake_hermes_gateway.py" in RUNNER
    assert "one-shot /loop control" in RUNNER
    assert "competing headless CLI forbidden" in RUNNER
    assert 'state_dir="${HOME}/.claude/channels/telegram-${slug}"' in RUNNER
    assert 'sys.path.insert(0, "/home/claude/bubble-ops-loop")' not in RUNNER


def test_live_tick_derives_same_private_runtime_lock_without_restart():
    assert "BUBBLE_AGENT_SLUG" in TICK_LOCK
    assert 'LOCK_DIR="/run/bubble-agent-${BUBBLE_AGENT_SLUG}"' in TICK_LOCK
    assert "[[ -w \"/run/bubble-agent-${BUBBLE_AGENT_SLUG}\" ]]" in TICK_LOCK
