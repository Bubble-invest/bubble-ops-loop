#!/usr/bin/env python3
"""output-guard.py — PostToolUse hook that scrubs secret-VALUE-shaped strings
out of tool_output before they reach the model's context (and, per the
Claude Code hooks doc, the on-disk session .jsonl transcript — a PostToolUse
`updatedToolOutput` replacement is saved into the transcript, so this hook is
the ONE place that fixes both the in-context leak and the at-rest leak for
NEW turns going forward. It does NOT retroactively scrub secrets already
written to existing .jsonl files before this hook was deployed — that needs a
separate one-time disk-scrub backstop for #726's 211 already-leaked
transcripts).

Card #732. Root-fixes two related leak classes:
  - #695: a dept agent leaked a personal IMAP app-password because a
    self-redact `sed` matched key-NAMES (e.g. "*_PASSWORD") and missed the
    `_PERSONAL` suffix variant. Name-based matching is fragile by
    construction — there's always another env-var naming convention it
    misses.
  - #726: 211 session transcripts had secret VALUES (sk-ant-*, sk-or-v1-*,
    tskey-auth-*) sitting in cleartext because nothing scrubbed tool_output
    before it was written.

This hook fixes the ROOT CAUSE class by matching on secret VALUE SHAPE
instead of key name — a token doesn't stop being a secret because the
variable holding it was named unexpectedly.

Per the Claude Code hooks doc (PostToolUse):
  - stdin = JSON with tool_name + tool_input + tool_output (+ cwd, session_id,
    etc).
  - We return `updatedToolOutput` under `hookSpecificOutput` when a
    value-shaped secret was found and scrubbed.
  - For clean output we exit 0 with NO json -> tool_output passes through
    unchanged (mirrors mission-file-guard.py's allow convention).

FAIL-OPEN by design: if we cannot parse the input, or the scrub itself raises,
we exit 0 and let the ORIGINAL tool_output through unchanged rather than
break the tool call. A guard that breaks tools is worse than the leak it's
trying to catch — Claude Code will surface a broken/empty tool_output as a
hard failure on every single tool call fleet-wide, which is a much bigger
incident than a secret shape slipping through once.
"""

from __future__ import annotations

import json
import re
import sys

# ── Value-SHAPE patterns (NOT key-name patterns — see #695 lesson above) ───
#
# Each pattern matches the STRUCTURE of a real token format. Kept deliberately
# narrow / anchored on each provider's documented prefix + a minimum-length
# body so we don't over-match short or ambiguous strings (e.g. plain hex
# hashes, git SHAs, UUIDs) that happen to share a character class.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic API / OAuth tokens: sk-ant-..., sk-ant-oat01-..., sk-ant-api03-...
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # OpenRouter API keys: sk-or-v1-<64 hex>
    ("openrouter", re.compile(r"\bsk-or-v1-[a-f0-9]{40,}\b")),
    # Tailscale auth keys: tskey-auth-<stable-id>-<secret>
    ("tailscale", re.compile(r"\btskey-auth-[A-Za-z0-9-]{20,}\b")),
    # Generic OpenAI-style keys (sk-<48 alnum>) — conservative: requires
    # exactly the classic 48-char body so it doesn't swallow sk-ant-/sk-or-v1-
    # (those are matched above; ordering doesn't matter since each pattern is
    # independently anchored, but the length floor keeps this one narrow).
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9]{48}\b")),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_, github_pat_
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    # Slack tokens: xox[bpoas]-...
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # AWS access key IDs (the ID alone isn't secret, but it's a strong
    # co-locator for AWS secret material in the same blob — conservative
    # inclusion; does not match on AWS secret key shape alone, which is just
    # base64 and too ambiguous to safely match without over-masking).
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
]

REDACTION_MARKER = "[REDACTED-SECRET-SHAPE]"


def _scrub(text: str) -> tuple[str, bool]:
    """Replace every value-shaped secret match with the redaction marker.
    Returns (scrubbed_text, changed)."""
    changed = False
    out = text
    for _name, pattern in _SECRET_PATTERNS:
        out, n = pattern.subn(REDACTION_MARKER, out)
        if n:
            changed = True
    return out, changed


def _emit_scrubbed(text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": text,
        }
    }))


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail-open: can't parse input -> don't touch tool_output

    try:
        tool_output = data.get("tool_output", "")
        # tool_output is documented as the tool's result; it's normally a
        # string, but some tools may hand back structured JSON. Scrub the
        # string form either way — for non-string output, scrub its JSON
        # serialization so we still catch a leaked value nested in a field,
        # then hand back the modified string (updatedToolOutput is
        # string-only per the hooks doc).
        if isinstance(tool_output, str):
            text = tool_output
        else:
            text = json.dumps(tool_output)

        scrubbed, changed = _scrub(text)
        if changed:
            _emit_scrubbed(scrubbed)
        # unchanged -> exit 0 with no JSON, tool_output passes through as-is
        return 0
    except Exception:
        return 0  # fail-open: scrub itself errored -> don't block/mutate the tool call


if __name__ == "__main__":
    raise SystemExit(main())
