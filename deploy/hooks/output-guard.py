#!/usr/bin/env python3
"""output-guard.py — PostToolUse hook that scrubs secret-VALUE-shaped strings
out of tool_output before they reach the model's context.

UNVERIFIED: whether the scrubbed `updatedToolOutput` also gets written to the
on-disk session .jsonl transcript is NOT confirmed by the Claude Code hooks
doc. The doc's "replays the saved text on --resume" language documents
SessionStart's `additionalContext`, not PostToolUse's `updatedToolOutput` —
the two are not shown to behave the same way. This hook DEFINITELY fixes the
in-context leak (the model never sees the raw secret again). The at-rest
(.jsonl) guarantee needs an empirical spot-check on a live box before we rely
on it — until that's done, treat the disk-scrub backstop below as required
regardless of outcome, not just a nice-to-have for the already-leaked
transcripts: if PostToolUse does NOT persist to disk, at-rest leaks continue
for every NEW session too, not only the 211 old ones covered by #726.

It does NOT retroactively scrub secrets already written to existing .jsonl
files before this hook was deployed — that needs a separate one-time
disk-scrub backstop for #726's 211 already-leaked transcripts, and per the
above, that backstop is not just for the old transcripts — see the
"must-happen-regardless" note above.

Card #732. Root-fixes the recurring PROVIDER-PREFIXED-TOKEN leak class (#726,
211 transcripts with sk-ant-*, sk-or-v1-*, tskey-auth-* etc sitting in
cleartext because nothing scrubbed tool_output before it was written) and the
general "key-name matching is fragile" pattern that #695 exposed (a
self-redact `sed` matched key-NAMES like "*_PASSWORD" and missed the
`_PERSONAL` suffix variant — there's always another naming convention a
name-based matcher misses).

IMPORTANT — this does NOT catch #695's own leak in practice: #695's actual
secret was a bare 16-char alphanumeric IMAP app-password with no distinctive
prefix, and value-shape matching deliberately does not match bare
unlabeled-shape strings (doing so would false-positive on every hex hash /
UUID / git SHA in normal tool output). So the next #695-SHAPED incident (a
bare, unlabeled secret with no recognizable prefix) will slip through this
hook exactly as it did before. That gap needs a different mitigation — e.g. a
scoped egress allowlist, or requiring newly-issued credentials to carry a
distinguishable prefix — tracked as a follow-up, not solved here.

This hook fixes the ROOT CAUSE class by matching on secret VALUE SHAPE
instead of key name — a token doesn't stop being a secret because the
variable holding it was named unexpectedly. It is necessarily bounded to the
provider prefixes below; see the non-exhaustive note near _SECRET_PATTERNS.

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
# NON-EXHAUSTIVE provider list — this is a living list, not full coverage.
# Known uncovered shapes (follow-up, not solved here): Stripe (sk_live_/
# sk_test_), Google API keys (AIzaSy...), generic JWTs, DB connection-string
# passwords (postgres://user:pass@host), AWS SECRET access keys (only the
# AKIA/ASIA access-key-ID is matched above, not the paired secret), base64-
# wrapped secrets, and secrets split across separate JSON fields (e.g.
# {"user": "...", "pass": "..."} where neither field alone is shaped like a
# known token). Add new provider prefixes here as they're identified; do not
# assume this list is complete.
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
