"""Exercise the canonical writing helper with isolated CLI stubs (#1626).

PATH contains only test CLIs and required shell utilities; HOME/config/TMPDIR
are synthetic. No installed agent, operator credentials, or network is used.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/lib/codex_write.sh"

CODEX_STUB = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
brief = sys.stdin.read()
SECRETS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_STATE_DIR", "GH_TOKEN", "GITHUB_TOKEN")
Path(os.environ["CODEX_LOG"]).write_text(json.dumps({"args": args, "brief": brief,
    "leaked": [k for k in SECRETS if k in os.environ]}))
out = Path(args[args.index("-o") + 1])
assert not out.exists(), "stale output reached Codex"
if os.environ.get("CODEX_DRAFT", ""):
    out.write_text(os.environ["CODEX_DRAFT"])
print(os.environ.get("CODEX_CONSOLE", ""), file=sys.stderr)
sys.exit(int(os.environ.get("CODEX_RC", "0")))
'''

CLAUDE_STUB = r'''
import json, os, sys
from pathlib import Path
cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
assert cfg != Path(os.environ["SOURCE_CONFIG"])
assert cfg.is_dir()
assert not (cfg / "plugins").exists()
assert not Path(os.environ["OUTPUT_FILE"]).exists(), "partial Codex output reached Claude"
credentials = cfg / ".credentials.json"
Path(os.environ["CLAUDE_LOG"]).write_text(json.dumps({
    "args": sys.argv[1:], "brief": sys.stdin.read(), "cwd": os.getcwd(),
    "config": str(cfg), "onboarding": json.loads((cfg / ".claude.json").read_text()),
    "credentials": credentials.read_text() if credentials.exists() else None,
    "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
    "leaked": [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_STATE_DIR", "GH_TOKEN", "GITHUB_TOKEN") if k in os.environ],
}))
print(os.environ.get("CLAUDE_DRAFT", "Sonnet draft"), end="")
sys.exit(int(os.environ.get("CLAUDE_RC", "0")))
'''


def _stub(path, body):
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def runner(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    # Do not append the real PATH: even absent-CLI cases must stay isolated.
    for name in ("dirname", "rm", "grep", "tail", "mktemp", "cp", "env"):
        executable = shutil.which(name)
        assert executable, f"required shell utility {name} missing"
        (mock_bin / name).symlink_to(executable)
    _stub(mock_bin / "codex", CODEX_STUB)
    _stub(mock_bin / "claude", CLAUDE_STUB)
    home = tmp_path / "home"
    home.mkdir()
    source_config = home / ".claude"
    source_config.mkdir()
    # Simulate a caller with a Telegram plugin. This must never be copied.
    (source_config / "plugins").mkdir()
    brief = tmp_path / "writing brief.txt"
    brief.write_text("Use the supplied voice.\nTopic: synthetic test.\n", encoding="utf-8")
    out = tmp_path / "finished draft.txt"
    env = {
        "PATH": str(mock_bin), "HOME": str(home), "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1", "CODEX_DRAFT": "Codex draft",
        "CODEX_LOG": str(tmp_path / "codex.json"),
        "CLAUDE_LOG": str(tmp_path / "claude.json"),
        "SOURCE_CONFIG": str(source_config), "OUTPUT_FILE": str(out),
    }

    class Runner:
        def run(self, *args, relative=False):
            return subprocess.run(
                ["/bin/bash", str(SCRIPT), "--brief", brief.name if relative else str(brief),
                 "--out", out.name if relative else str(out), *args],
                cwd=tmp_path,
                env=env, capture_output=True, text=True, check=False, timeout=10,
            )

        def log(self, cli):
            return json.loads(Path(env[f"{cli.upper()}_LOG"]).read_text())

        def remove_cli(self, cli):
            (mock_bin / cli).unlink()

    instance = Runner()
    instance.env, instance.out, instance.brief = env, out, brief
    instance.source_config = source_config
    return instance


def test_codex_write_success_and_default_model(runner):
    runner.out.write_text("stale draft")
    result = runner.run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert runner.out.read_text() == "Codex draft"
    args = runner.log("codex")["args"]
    assert args == [
        "exec", "-m", "gpt-5.6-sol", "-c", "model_reasoning_effort=high",
        "-s", "read-only", "--skip-git-repo-check", "-C", str(REPO_ROOT),
        "-o", str(runner.out), "-",
    ]
    assert runner.log("codex")["brief"] == runner.brief.read_text()
    assert not Path(runner.env["CLAUDE_LOG"]).exists()


@pytest.mark.parametrize("console", [
    '{"type":"error"} recovered', "not supported; retry succeeded",
    "invalid_request_error recovered", "401 unauthorized; retry succeeded",
])
def test_codex_write_1301_valid_draft_beats_error_looking_console(runner, console):
    runner.env["CODEX_CONSOLE"] = console
    result = runner.run()
    assert result.returncode == 0, result.stderr
    assert runner.out.read_text() == "Codex draft"
    assert "CLASSIFICATION=" not in result.stderr
    assert not Path(runner.env["CLAUDE_LOG"]).exists()


@pytest.mark.parametrize("rc,draft,console,classification", [
    (1, "partial draft", "401 unauthorized", "auth"),
    (1, "partial draft", "403 forbidden", "auth"),
    (1, "partial draft", "model_not_found", "model"),
    (1, "partial draft", "invalid_request_error", "model"),
    (1, "partial draft", "network timeout", "runtime"),
    (0, "", "401 unauthorized", "runtime"),
])
def test_codex_write_failure_detection_and_classification(runner, rc, draft, console, classification):
    runner.remove_cli("claude")
    runner.out.write_text("stale draft")
    runner.env.update(CODEX_RC=str(rc), CODEX_DRAFT=draft, CODEX_CONSOLE=console)
    result = runner.run()
    assert result.returncode == 3, result.stderr
    assert f"CLASSIFICATION={classification}" in result.stderr
    assert not runner.out.exists()


@pytest.mark.parametrize("missing_codex,classification", [(False, "model"), (True, "runtime")])
def test_codex_write_1435_sonnet_fallback_isolated_token_auth(runner, missing_codex, classification):
    runner.out.write_text("stale draft")
    if missing_codex:
        runner.remove_cli("codex")
    runner.env.update(
        CODEX_RC="1", CODEX_CONSOLE="unsupported model", CODEX_DRAFT="partial draft",
        CLAUDE_CODE_OAUTH_TOKEN="synthetic-token",
    )
    (runner.source_config / ".credentials.json").write_text("unused credentials")
    result = runner.run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert runner.out.read_text() == "Sonnet draft"
    assert "SONNET FALLBACK" in result.stderr
    assert f"CLASSIFICATION={classification}" in result.stderr
    assert "synthetic-token" not in result.stderr
    log = runner.log("claude")
    assert log["args"][0] == "-p"
    assert log["args"][-4:] == ["--model", "sonnet", "--strict-mcp-config", "--dangerously-skip-permissions"]
    assert log["brief"] == runner.brief.read_text()
    assert log["cwd"] == str(REPO_ROOT)
    assert log["token"] == "synthetic-token"
    assert log["credentials"] is None  # env auth takes precedence
    assert log["onboarding"]["hasCompletedOnboarding"] is True
    assert not Path(log["config"]).exists()
    assert (runner.source_config / "plugins").exists()


@pytest.mark.parametrize("custom_config", [False, True])
def test_codex_write_fallback_copies_credentials_from_callers_config(runner, tmp_path, custom_config):
    if custom_config:
        runner.source_config = tmp_path / "custom-config"
        runner.source_config.mkdir()
        runner.env.update(CLAUDE_CONFIG_DIR=str(runner.source_config), SOURCE_CONFIG=str(runner.source_config))
    credentials = '{"synthetic":"test-only"}'
    (runner.source_config / ".credentials.json").write_text(credentials)
    runner.env.update(CODEX_RC="1", CODEX_CONSOLE="token expired")
    result = runner.run()
    assert result.returncode == 0, result.stderr
    assert "CLASSIFICATION=auth" in result.stderr
    log = runner.log("claude")
    assert log["credentials"] == credentials
    assert not Path(log["config"]).exists()
    assert (runner.source_config / ".credentials.json").read_text() == credentials


@pytest.mark.parametrize("rc,draft", [
    (1, "partial Sonnet draft"), (0, ""), (0, "Not logged in. Please run /login"),
    (0, "Unauthorized"), (0, "Invalid API key"),
])
def test_codex_write_rejects_failed_empty_or_login_stub_fallback(runner, rc, draft):
    runner.env.update(CODEX_RC="1", CLAUDE_RC=str(rc), CLAUDE_DRAFT=draft)
    result = runner.run()
    assert result.returncode == 3, result.stderr
    assert "CLASSIFICATION=runtime" in result.stderr
    assert not runner.out.exists()
    assert not Path(runner.log("claude")["config"]).exists()


def test_codex_write_missing_both_clis_clears_stale_output(runner):
    runner.remove_cli("codex")
    runner.remove_cli("claude")
    runner.out.write_text("stale draft")
    result = runner.run()
    assert result.returncode == 3
    assert "CLASSIFICATION=runtime" in result.stderr
    assert "codex CLI not found" in result.stderr
    assert not runner.out.exists()


@pytest.mark.parametrize("fallback", [False, True])
def test_codex_write_relative_paths_from_outside_repo(runner, fallback):
    if fallback:
        runner.env["CODEX_RC"] = "1"
    result = runner.run(relative=True)
    assert result.returncode == 0, result.stderr
    assert runner.out.read_text() == ("Sonnet draft" if fallback else "Codex draft")
    assert runner.log("claude" if fallback else "codex")["brief"] == runner.brief.read_text()


@pytest.mark.parametrize("flags,env_model,env_effort,model,effort", [
    ([], "custom-model", "medium", "custom-model", "medium"),
    (["--model", "flag-model", "--effort", "low"], "custom-model", "medium", "flag-model", "low"),
    ([], "", "", "gpt-5.6-sol", "high"),
])
def test_codex_write_model_effort_env_and_flag_compatibility(runner, flags, env_model, env_effort, model, effort):
    runner.env.update(CODEX_MODEL=env_model, CODEX_REASONING_EFFORT=env_effort)
    result = runner.run(*flags)
    assert result.returncode == 0, result.stderr
    args = runner.log("codex")["args"]
    assert args[args.index("-m") + 1] == model
    assert args[args.index("-c") + 1] == f"model_reasoning_effort={effort}"


@pytest.mark.parametrize("args", [
    ["--unknown"], ["--model"], ["--effort", ""], ["--out"],
    ["--brief", "/nonexistent-synthetic-brief"], ["--model", "--effort", "high"],
])
def test_codex_write_usage_errors_keep_exit_two(runner, args):
    result = runner.run(*args)
    assert result.returncode == 2, result.stderr
    assert not Path(runner.env["CODEX_LOG"]).exists()
    assert not Path(runner.env["CLAUDE_LOG"]).exists()


def test_codex_write_bash_syntax():
    result = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_worker_env_scrubs_fleet_secrets(runner):
    """#1598: inherited bot/GitHub tokens must never reach codex or the Sonnet fallback."""
    runner.env.update(
        TELEGRAM_BOT_TOKEN="synthetic-bot", TELEGRAM_STATE_DIR="/nonexistent/state",
        GH_TOKEN="synthetic-gh", GITHUB_TOKEN="synthetic-gh2",
        CODEX_RC="1", CODEX_CONSOLE="unsupported model", CODEX_DRAFT="partial draft",
        CLAUDE_CODE_OAUTH_TOKEN="synthetic-token",
    )
    result = runner.run()
    assert result.returncode == 0, result.stderr
    assert runner.log("codex")["leaked"] == []
    assert runner.log("claude")["leaked"] == []
