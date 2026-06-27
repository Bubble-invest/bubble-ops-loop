"""Read a dept's runtime /loop arming prompt — the boot-inject message the
systemd unit injects at (re)start that tells the agent HOW to pace its loop.

This is distinct from the per-layer MISSION prompts (what the agent DOES at each
OODA moment, surfaced from layers/<n>/PROMPT.md): this is the RUNTIME prompt that
governs WHEN / how-often the session wakes (self-paced vs fixed cron).

Source of truth on the VPS = the systemd drop-in
``/etc/systemd/system/ops-loop-<slug>.service.d/boot-inject.conf`` (world-readable;
the console runs as ``claude`` and can read it). For host:local depts (e.g. Miranda
on Jade's Mac) the drop-in is on another machine → we return None and the panel
shows a graceful "not available on this host" note.

Read-only. Phase 2 (separate card) may make this editable via a settings-PR flow.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Dict

_DROPIN = "/etc/systemd/system/ops-loop-{slug}.service.d/boot-inject.conf"

# slug must be a simple identifier (defense against path traversal).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _dropin_path(slug: str) -> Optional[Path]:
    if not _SLUG_RE.match(slug or ""):
        return None
    return Path(_DROPIN.format(slug=slug))


def _extract_prompt(conf_text: str) -> Optional[str]:
    """Pull the printf-ed prompt string out of the ExecStartPost line.

    Handles both forms we ship:
      printf 'PROMPT\\n' >> ...inject          (template form)
      printf '%s\\n' 'PROMPT' >> ...inject      (apply-script form)
    Returns the prompt with the trailing newline stripped, or None.
    """
    for line in conf_text.splitlines():
        if "ExecStartPost" not in line or "printf" not in line:
            continue
        # Every single-quoted chunk on the line, in order.
        chunks = re.findall(r"'([^']*)'", line)
        # Drop format-string-only chunks like '%s\n' / '\n'.
        cand = [c for c in chunks if c.replace("%s", "").replace("\\n", "").strip()]
        if not cand:
            continue
        # The prompt is the longest meaningful chunk (the format arg is short).
        # Assumes the redirect target (>> .../inject) is NOT single-quoted, which
        # holds for both forms we ship; a single-quoted long path would mis-win here.
        txt = max(cand, key=len)
        txt = txt.replace("\\n", "").strip()
        return txt or None
    return None


def _cadence_label(prompt: str) -> str:
    """One-word badge for the panel: how this dept paces its loop."""
    p = (prompt or "").lower()
    if "self-paced" in p or ("croncreate" in p and "never hardcode an hourly" in p):
        return "self-paced"
    if "every 1h" in p or "every hour" in p or "every 20 min" in p:
        return "fixed-interval"
    return "unknown"


def load_loop_runtime_prompt(slug: str) -> Optional[Dict[str, str]]:
    """Return {'prompt', 'source', 'cadence'} for the dept's boot-inject runtime
    prompt, or None if unavailable (bad slug, drop-in missing, or host:local).
    Never raises — the dept page must always render.
    """
    path = _dropin_path(slug)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prompt = _extract_prompt(text)
    if not prompt:
        return None
    return {
        "prompt": prompt,
        "source": str(path),
        "cadence": _cadence_label(prompt),
    }
