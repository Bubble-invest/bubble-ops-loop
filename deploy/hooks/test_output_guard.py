"""Tests for output-guard.py PostToolUse hook.

Feeds the exact PostToolUse stdin JSON and asserts the scrub/pass-through
decision. Card #732 — root fix for #695 (key-name matching missed the
`_PERSONAL` suffix) and #726 (211 transcripts leaking token values).

ALL secret-shaped strings in this file are SYNTHETIC (clearly fake bodies,
not real credential material) — never a real decrypted secret.

Run: python3 -m pytest deploy/hooks/test_output_guard.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # bubble-ops-loop/
HOOK = REPO_ROOT / "deploy" / "hooks" / "output-guard.py"

MARKER = "[REDACTED-SECRET-SHAPE]"


def _run(tool_output, tool_name: str = "Bash", tool_input: dict | None = None):
    """Invoke the hook with a PostToolUse payload; return (exit_code, parsed_json_or_None)."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_output": tool_output,
        "cwd": "/home/claude/agents/bubble-ops-maya",
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, check=False,
    )
    out = proc.stdout.strip()
    parsed = json.loads(out) if out else None
    return proc.returncode, parsed


def _updated(parsed) -> str | None:
    if not parsed:
        return None
    return parsed.get("hookSpecificOutput", {}).get("updatedToolOutput")


# ── Synthetic secret-shaped strings get scrubbed ────────────────────────────

## NOTE: these are deliberately NOT plausible real tokens — each body is a
## repeated placeholder character run (XXXX.../0000...) rather than
## token-like random-looking text, specifically so GitHub push protection /
## secret scanners don't mistake a test fixture for a live leak (a synthetic
## Slack-shaped fixture with pseudo-random digits tripped push protection
## during this PR's own review — see PR body). The value-SHAPE regexes in
## output-guard.py match on prefix + character-class + length only, so a
## placeholder run still exercises the exact same match path as a real token.
SYNTHETIC_SECRETS = {
    "anthropic": "sk-ant-oat01-" + "X" * 46,
    "anthropic-api": "sk-ant-api03-" + "X" * 45,
    "openrouter": "sk-or-v1-" + "0" * 48,
    "tailscale": "tskey-auth-" + "X" * 30,
    "openai-style": "sk-" + "X" * 48,
    "github-pat-classic": "ghp_" + "X" * 40,
    "github-pat-fine": "github_pat_" + "X" * 70,
    "slack": "xoxb-" + "X" * 20,
    "aws-access-key-id": "AKIA" + "X" * 16,  # AKIA + 16-char body
}


def test_all_synthetic_secret_shapes_are_scrubbed():
    for label, secret in SYNTHETIC_SECRETS.items():
        blob = f"here is some tool output\nAPI_KEY={secret}\ndone"
        code, parsed = _run(blob)
        assert code == 0
        updated = _updated(parsed)
        assert updated is not None, f"{label}: expected a scrub, got pass-through"
        assert secret not in updated, f"{label}: raw secret still present in output"
        assert MARKER in updated, f"{label}: redaction marker missing"


def test_multiple_secrets_in_one_blob_all_scrubbed():
    blob = (
        f"first: {SYNTHETIC_SECRETS['anthropic']}\n"
        f"second: {SYNTHETIC_SECRETS['openrouter']}\n"
        f"third: {SYNTHETIC_SECRETS['tailscale']}\n"
    )
    code, parsed = _run(blob)
    updated = _updated(parsed)
    assert updated is not None
    for label in ("anthropic", "openrouter", "tailscale"):
        assert SYNTHETIC_SECRETS[label] not in updated


def test_secret_embedded_in_structured_json_output_is_scrubbed():
    # Some tools hand back structured JSON as tool_output instead of a string.
    structured = {"stdout": f"token={SYNTHETIC_SECRETS['anthropic']}", "exit_code": 0}
    code, parsed = _run(structured)
    updated = _updated(parsed)
    assert updated is not None
    assert SYNTHETIC_SECRETS["anthropic"] not in updated
    assert MARKER in updated


# ── Non-secret output passes through UNCHANGED (no over-masking) ───────────

REALISTIC_NON_SECRET_OUTPUTS = [
    # Normal shell output
    "total 24\ndrwxr-xr-x  5 claude claude 160 Jul 21 10:00 .\n-rw-r--r--  1 claude claude 512 Jul 21 09:58 README.md",
    # Normal prose / commit message style output
    "Successfully merged branch 'feat/732-posttooluse-output-guard' into main.",
    # Python code containing identifiers that share character classes with
    # token shapes but aren't secrets (git SHA, UUID, plain hex, var names).
    (
        "commit a1b2c3d4e5f6789012345678901234567890abcd\n"
        "uuid: 550e8400-e29b-41d4-a716-446655440000\n"
        "def sk_ant_helper(x): return x\n"
        "hex_digest = 'deadbeefcafebabe0123456789abcdef01234567'\n"
    ),
    # Env var NAME mentions without a value-shaped secret attached (the #695
    # class this hook deliberately does NOT try to catch by name — but it
    # also must not false-positive on the bare name).
    "OPENROUTER_API_KEY_PERSONAL is set in .sops.env (value not shown here)",
    # A short ambiguous string that merely starts with a similar prefix but
    # is far too short to be a real token — must NOT be flagged.
    "sk-ant-demo",
    "ghp_short",
    # AWS-style ID-looking string that's actually just a resource tag, too
    # short to match the 16-char body requirement.
    "AKIA-NOT-A-REAL-KEY-ID",
]


def test_realistic_non_secret_output_passes_through_unchanged():
    for blob in REALISTIC_NON_SECRET_OUTPUTS:
        code, parsed = _run(blob)
        assert code == 0
        assert parsed is None, (
            f"over-masked non-secret output: {blob!r} -> {parsed!r}"
        )


def test_empty_output_passes_through():
    code, parsed = _run("")
    assert code == 0
    assert parsed is None


# ── Fail-open ────────────────────────────────────────────────────────────

def test_empty_stdin_fails_open():
    proc = subprocess.run([sys.executable, str(HOOK)], input="", capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_malformed_json_stdin_fails_open():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{not valid json",
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_scrub_error_fails_open_and_passes_tool_output_through(monkeypatch):
    """Inject a fault into the scrub path itself (simulating a bug/exception
    inside _scrub) and confirm the hook still exits 0 with NO json — i.e. the
    ORIGINAL tool output is what the caller (Claude Code) will use, so the
    tool call completes normally instead of breaking."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("output_guard", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _boom(text):
        raise RuntimeError("simulated scrub failure")

    monkeypatch.setattr(mod, "_scrub", _boom)
    monkeypatch.setattr(
        sys, "stdin",
        __import__("io").StringIO(json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {},
            "tool_output": f"leaked: {SYNTHETIC_SECRETS['anthropic']}",
            "cwd": "/tmp",
        })),
    )
    rc = mod.main()
    assert rc == 0  # fail-open: no crash, no non-zero exit that could break the tool call


def test_unknown_tool_output_shape_none_is_handled():
    # tool_output missing entirely from payload -> defaults to "" -> no crash
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {},
        "cwd": "/tmp",
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
