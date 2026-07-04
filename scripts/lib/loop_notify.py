"""loop_notify — per-layer-fire Telegram pings for the /loop protocol (WS3).

Built ON TOP of the promoted, dept-agnostic ``scripts/lib/notify.py``
(SMTPEmailBackend + TelegramBackend + per-channel failure isolation). The
layer-fire pings themselves ride the **Telegram** transport (the plan keeps
the layer pings Telegram-only even though the underlying notify module is the
full email+Telegram thing).

Verbosity ({{OPERATOR}} decision, see LOOP-FIX-BUILD.md / plan WS3):
  - **L1 & L4 notify IMMEDIATELY** — one line each, the moment the layer fires
    (in /loop STEP 3c, after ``validate_layer_output`` returns ok, before the
    STEP 5 commit). Call ``notify_layer_fired(dept, layer, summary_path)``.
  - **L2 & L3 are BATCHED** — the loop accumulates L2/L3 fires within a tick
    and emits ONE summary line at STEP 6. Call
    ``notify_layers_batched(dept, {"2": n2, "3": n3})``.

Message shapes
--------------
  immediate (no brief configured/found) :
      ``🔁 <dept> · L<N> fired — <first line of summary.md>``
  immediate (brief artifact configured+found, board #521) :
      ``🔁 <dept> · L<N> fired`` header, then the FULL BODY of the dept's
      real brief artifact (e.g. ``morning_brief.md`` / ``telegram_message.md``),
      truncated to ``BRIEF_MAX_CHARS`` with a "…(tronqué)" marker when cut.
  batched   :  ``🔁 <dept> · L2 ×3, L3 ×1``

Board #521 (fleet-wide L1/L4 brief delivery)
---------------------------------------------
Joris was getting a content-free stub for L1/L4 ("🔁 tony · L1 fired — L1
Morning Brief — 2026-07-04") instead of the actual brief. Root causes fixed
here (generalized to the whole fleet, not just Tony):

  1. ``notify_layer_fired`` now sends the REAL BRIEF BODY for L1/L4, not just
     the first line of summary.md. ``_first_line_of_summary`` remains the
     fallback shape for L2/L3 batching and for depts that haven't configured
     a brief artifact (no regression).
  2. **Config-driven, not hardcoded to Tony.** A dept declares its L1/L4 brief
     artifact filename via ``config['brief_artifacts']`` (preferred) or
     ``config['department']['brief_artifacts']`` (dept.yaml convention), e.g.::

         brief_artifacts:
           "1": morning_brief.md
           "4": telegram_message.md

     ``_resolve_brief_path(dept_outputs_dir, layer, config)`` resolves the
     configured filename under today's ``outputs/<date>/<layer>/`` dir. When
     no artifact is configured OR the configured file isn't found, callers
     fall back to the pre-#521 first-line-of-summary behavior — no dept
     regresses just because it hasn't opted in yet.
  3. The artifact gate (``tools/notify_layer.py``) no longer silently drops a
     fire when the *brief* body exists but the separate summary.md doesn't —
     see that module's docstring.
  4. Recipients: L1/L4 briefs fan out to every account configured for the
     dept (e.g. Operator + Jade / "cc-Jade-on-briefs") via the existing
     multi-account ``resolve_recipients`` fan-out — pass
     ``account=["Operator", "Jade"]`` (or your config's account names).

Both go to Telegram via the promoted ``TelegramBackend``. The recipient
``chat_id`` is resolved dept-agnostically from ``config`` (the same config
the rest of the loop already loads) via ``resolve_recipients`` — no hardcoded
chat_id. ``TELEGRAM_BOT_TOKEN`` is read from the env by the backend; a missing
token raises a clear ``MissingCredentialError`` (mirrors Maya's pattern) which
the backend converts into a failed-but-non-fatal receipt.

Observability (board #521 cause 4)
-----------------------------------
Every send attempt — success or failure — is appended as one JSON line to a
notify log (default ``<dept_outputs_dir>/notify.log``, override via
``notify_log_path=`` or env ``BUBBLE_NOTIFY_LOG_PATH``) via ``log_notify_event``.
This closes the "swallowed non-fatal print" gap: a 429/transient failure now
leaves a durable trace a human or the cockpit can read, instead of vanishing
into a local ``print()`` only visible in the cron's stdout. Logging itself
never raises and never blocks the send.

Testability
-----------
``send_*`` accept an injectable ``opener`` (urllib-like callable). Tests pass a
fake opener and assert the URL, chat_id, parse_mode, and message text — no live
HTTP. The same ``opener`` is threaded into ``TelegramBackend(config, _opener=…)``.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

# Import the promoted transport. Tests/callers run from the framework repo
# root, where ``scripts.lib.notify`` resolves (same convention as
# ``scripts.lib.loop_backup`` / ``scripts.lib.dispatch_helpers``).
try:  # pragma: no cover - import-path dependent
    from scripts.lib.notify import (
        NotificationPayload,
        TelegramBackend,
        resolve_recipients,
        CHANNEL_TELEGRAM_ALERT,
        MissingCredentialError,
    )
except Exception:  # noqa: BLE001 - allow sibling import when run inside scripts/lib
    from notify import (  # type: ignore
        NotificationPayload,
        TelegramBackend,
        resolve_recipients,
        CHANNEL_TELEGRAM_ALERT,
        MissingCredentialError,
    )

# The default account the loop pings on a layer fire. Dept configs map this
# account name → a telegram_chat_id under config['accounts']. Overridable per
# call so couple-mode depts (e.g. Maya → both operators) can fan out.
DEFAULT_ACCOUNT = os.environ.get("BUBBLE_OPERATOR_NAME", "Operator")

LAYER_FIRE_GLYPH = "🔁"

# Layers that get the IMMEDIATE, full-brief-body treatment (board #521). L2/L3
# stay batched one-liners — they're internal cadence, not operator briefs.
BRIEF_LAYERS = ("1", "4")

# Telegram hard cap is 4096 chars for the WHOLE message (subject + body, see
# notify.TELEGRAM_MESSAGE_MAX); the TelegramBackend already truncates at that
# level, but we truncate the brief body ourselves first so the cut lands at a
# sane boundary with a clear French "tronqué" marker (board #521 requirement)
# rather than an arbitrary mid-word chop deep inside notify.py's own budget
# math. Leaves headroom for the subject line + cockpit link + the marker.
BRIEF_MAX_CHARS = int(os.environ.get("BUBBLE_BRIEF_MAX_CHARS", "3500"))
BRIEF_TRUNCATION_MARKER = "\n\n…(tronqué)"

# Default notify-log filename, written under the dept's outputs dir (or
# BUBBLE_NOTIFY_LOG_PATH when the caller doesn't have an outputs dir handy).
DEFAULT_NOTIFY_LOG_NAME = "notify.log"

# Cockpit (console) base URL — every layer-fired ping carries the dept's cockpit
# link so {{OPERATOR}} can open the work directly from Telegram ({{OPERATOR}} msg 3985,
# 2026-06-06). Env-overridable for non-prod/test. Console is Tailscale-only.
COCKPIT_BASE_URL = os.environ.get(
    "BUBBLE_COCKPIT_BASE_URL",
    f"https://{os.environ.get('BUBBLE_VPS_HOST', 'localhost')}:8443",
).rstrip("/")


def _cockpit_link(dept: str) -> str:
    """The dept's cockpit page — appended to every layer-fired ping."""
    return COCKPIT_BASE_URL + "/dept/" + dept


def _first_line_of_summary(summary_path) -> str:
    """Return the first non-empty line of a layer's summary.md, stripped of a
    leading Markdown heading marker. Defensive — never raises; returns "" when
    the file is missing/unreadable/empty (the ping still goes out, just bare).
    """
    if not summary_path:
        return ""
    try:
        p = Path(summary_path)
        if not p.exists():
            return ""
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            # Strip a leading "# "/"## "… heading marker for a clean one-liner.
            while line.startswith("#"):
                line = line[1:]
            line = line.strip()
            if line:
                return line
        return ""
    except Exception:  # noqa: BLE001 - summary read must never break the ping
        return ""


def _configured_brief_filename(config: dict, layer) -> Optional[str]:
    """Resolve the configured brief-artifact FILENAME for ``layer`` from
    config (board #521, cause 2 — config-driven, not hardcoded to Tony).

    Looks in two places, preferred-first (either satisfies the contract):
      1. ``config['brief_artifacts']`` — flat, e.g. what a dept's
         ``config.yaml`` carries (notify_layer.py already loads this file).
      2. ``config['department']['brief_artifacts']`` — the ``dept.yaml``
         convention (mirrors how ``department.model`` / ``department.owner``
         etc. are nested), for callers that pass the parsed dept.yaml through.

    Keys may be the int layer, the str layer ("1"), or "L1" — all three are
    tried so a dept.yaml author doesn't have to guess YAML's int-vs-str
    quoting. Returns None (safe fallback, no regression) when nothing is
    configured for this layer — callers then fall back to the pre-#521
    first-line-of-summary behavior.
    """
    if not config:
        return None
    layer_str = str(layer).lstrip("L").lstrip("l")
    candidates = [layer, layer_str, f"L{layer_str}", int(layer_str) if layer_str.isdigit() else None]

    def _lookup(mapping) -> Optional[str]:
        if not isinstance(mapping, dict):
            return None
        for key in candidates:
            if key is None:
                continue
            val = mapping.get(key)
            if val:
                return str(val)
        return None

    val = _lookup(config.get("brief_artifacts"))
    if val:
        return val
    department = config.get("department")
    if isinstance(department, dict):
        val = _lookup(department.get("brief_artifacts"))
        if val:
            return val
    return None


def _resolve_brief_path(summary_path, layer, config: dict):
    """Resolve the real brief artifact path for an L1/L4 fire (board #521).

    ``summary_path`` is the layer's summary.md path as passed by the caller
    (e.g. ``outputs/2026-07-04/1/summary.md``); the brief artifact lives
    ALONGSIDE it in the same layer directory, under the filename configured
    via ``_configured_brief_filename``. Returns a ``Path`` when a filename is
    configured AND the file exists+is non-empty, else ``None`` (safe fallback
    — caller then uses the first-line-of-summary shape, so a dept with no
    brief_artifacts config or a not-yet-written brief never regresses).
    """
    filename = _configured_brief_filename(config, layer)
    if not filename or not summary_path:
        return None
    try:
        layer_dir = Path(summary_path).parent
        candidate = layer_dir / filename
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    except Exception:  # noqa: BLE001 - resolution must never break the ping
        return None
    return None


def _read_brief_body(brief_path) -> str:
    """Read + truncate a brief artifact's full text (board #521, cause 1).

    Truncates to ``BRIEF_MAX_CHARS`` with ``BRIEF_TRUNCATION_MARKER`` appended
    when cut, so an oversized brief (or a pathological huge file) never blows
    past Telegram's message cap. Defensive — never raises; "" on any error
    (caller then falls back to the first-line shape).
    """
    try:
        text = Path(brief_path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""
    if len(text) > BRIEF_MAX_CHARS:
        budget = BRIEF_MAX_CHARS - len(BRIEF_TRUNCATION_MARKER)
        if budget < 0:
            budget = 0
        text = text[:budget] + BRIEF_TRUNCATION_MARKER
    return text


def log_notify_event(
    event: dict,
    *,
    notify_log_path=None,
) -> None:
    """Append one JSON line recording a notify SEND OUTCOME (board #521,
    cause 4 — observability). Never raises: a logging failure must never
    break the (already best-effort) notify path, and must never crash the
    loop. Resolves the log path from, in order:
      1. ``notify_log_path`` (explicit, e.g. dept outputs dir / notify.log)
      2. ``BUBBLE_NOTIFY_LOG_PATH`` env var
      3. ``./notify.log`` relative to cwd (last-resort — still durable,
         just not dept-scoped; better than nothing).
    """
    try:
        path = notify_log_path or os.environ.get("BUBBLE_NOTIFY_LOG_PATH") or DEFAULT_NOTIFY_LOG_NAME
        record = {"ts": time.time()}
        record.update(event)
        p = Path(path)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass


def _telegram_backend(config: dict, opener):
    """Construct a TelegramBackend with the injected opener (tests) or the
    real urllib opener (production, when opener is None)."""
    return TelegramBackend(config or {}, _opener=opener)


def _resolve_chat_recipient(config: dict, account):
    """Resolve the Telegram chat_id recipient for ``account`` from config.

    Returns the recipient string (a chat_id, or comma-joined chat_ids for a
    multi-account fan-out). Raises ValueError (from resolve_recipients) when
    no chat_id is configured for the requested account.
    """
    mapping = resolve_recipients(account, [CHANNEL_TELEGRAM_ALERT], config or {})
    return mapping[CHANNEL_TELEGRAM_ALERT]


def notify_layer_fired(
    dept: str,
    layer,
    summary_path=None,
    *,
    config: Optional[dict] = None,
    account=DEFAULT_ACCOUNT,
    opener=None,
    test: bool = False,
    notify_log_path=None,
):
    """Send the IMMEDIATE per-layer ping for L1 / L4 (one line each).

    Board #521 shape (fleet-wide, config-driven — see module docstring):
      - For L1/L4 (``layer in BRIEF_LAYERS``), when the dept has configured a
        ``brief_artifacts`` filename for this layer AND that file exists
        alongside ``summary_path``, the ping is a short header
        (``🔁 <dept> · L<N> fired``) followed by the FULL BRIEF BODY
        (truncated to ``BRIEF_MAX_CHARS`` with a "…(tronqué)" marker).
      - Otherwise (no brief configured, brief not found, or L2/L3 callers
        that pass this function directly) — falls back to the pre-#521
        shape: ``🔁 <dept> · L<N> fired — <first line of summary.md>``. No
        dept regresses just because it hasn't opted into brief_artifacts.

    Args:
        dept: dept slug / label (e.g. "maya").
        layer: layer number (int or str, e.g. 1 / "1" / 4).
        summary_path: path to the layer's summary.md (first line → ping tail,
            and the directory the configured brief filename is resolved in).
        config: full dept config dict (provides accounts → telegram_chat_id,
            and optionally brief_artifacts).
        account: account name(s) to ping (default "{{OPERATOR}}"); str or
            list — pass e.g. ``["Operator", "Jade"]`` to cc a second account
            on the brief (board #521 cause 4, cc-Jade-on-briefs).
        opener: injectable urllib-like callable for tests; None → live urllib.
        notify_log_path: where to append the send-outcome observability
            record (board #521 cause 4). Defaults to
            ``BUBBLE_NOTIFY_LOG_PATH`` env or ``./notify.log``.

    Returns:
        The ``DeliveryReceipt`` from the Telegram backend (success flag +
        error). A missing ``TELEGRAM_BOT_TOKEN`` yields a non-fatal failed
        receipt (backend converts the MissingCredentialError); a missing
        chat_id in config raises ValueError (clear, fail-fast config error).
    """
    layer_str = str(layer).lstrip("L").lstrip("l")
    _prefix = "\U0001F9EA TEST " if test else ""  # 🧪 TEST prefix for verification pings
    header = f"{_prefix}{LAYER_FIRE_GLYPH} {dept} · L{layer_str} fired"

    brief_path = None
    if layer_str in BRIEF_LAYERS and not test:
        brief_path = _resolve_brief_path(summary_path, layer_str, config or {})

    if brief_path is not None:
        body = _read_brief_body(brief_path)
    else:
        body = ""

    if body:
        # Real brief body wins: header as the message subject (bold, via
        # TelegramBackend), full brief as the markdown body.
        text_subject = header
        markdown_body = body + "\n\n" + _cockpit_link(dept)
        used_brief = True
    else:
        # Fallback: pre-#521 first-line-of-summary shape, all in one line.
        first = _first_line_of_summary(summary_path)
        text_subject = header
        if first:
            text_subject += f" — {first}"
        markdown_body = _cockpit_link(dept)
        used_brief = False

    recipient = _resolve_chat_recipient(config or {}, account)
    payload = NotificationPayload(
        subject=text_subject,
        markdown_body=markdown_body,
        metadata={
            "dept": dept,
            "layer": layer_str,
            "kind": "layer_fired",
            "brief_sent": used_brief,
        },
    )
    backend = _telegram_backend(config or {}, opener)
    receipt = backend.send(payload, recipient)
    log_notify_event(
        {
            "dept": dept,
            "layer": layer_str,
            "kind": "layer_fired",
            "brief_sent": used_brief,
            "brief_path": str(brief_path) if brief_path else None,
            "recipient": recipient,
            "success": bool(getattr(receipt, "success", False)),
            "error": getattr(receipt, "error", None),
        },
        notify_log_path=notify_log_path,
    )
    return receipt


def format_batched_line(dept: str, counts: dict) -> str:
    """Build the BATCHED summary line for L2/L3 fires in a tick.

    ``🔁 <dept> · L2 ×3, L3 ×1`` — only layers with count > 0 are shown,
    in ascending layer order. Returns "" when no layer fired (count all 0 /
    empty) so the caller can skip the send.
    """
    parts = []
    for layer in sorted(counts or {}, key=lambda k: str(k)):
        try:
            n = int(counts[layer])
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            parts.append(f"L{str(layer).lstrip('L').lstrip('l')} ×{n}")
    if not parts:
        return ""
    return f"{LAYER_FIRE_GLYPH} {dept} · " + ", ".join(parts)


def notify_layers_batched(
    dept: str,
    counts: dict,
    *,
    config: Optional[dict] = None,
    account=DEFAULT_ACCOUNT,
    opener=None,
    notify_log_path=None,
):
    """Send ONE batched ping coalescing L2/L3 fires for a tick.

    Message: ``🔁 <dept> · L2 ×3, L3 ×1`` (layers with count 0 omitted).
    L2/L3 are UNCHANGED by board #521 — no brief-body behavior applies here,
    only L1/L4 via ``notify_layer_fired``.

    Args:
        dept: dept slug / label.
        counts: mapping of layer → fire count this tick, e.g. {"2": 3, "3": 1}.
        config / account / opener: as in ``notify_layer_fired``.
        notify_log_path: as in ``notify_layer_fired`` (board #521 cause 4).

    Returns:
        The ``DeliveryReceipt``, or ``None`` when no layer fired (nothing sent).
    """
    text = format_batched_line(dept, counts)
    if not text:
        return None
    text += "\n" + _cockpit_link(dept)
    recipient = _resolve_chat_recipient(config or {}, account)
    payload = NotificationPayload(
        subject=text,
        markdown_body="",
        metadata={"dept": dept, "kind": "layers_batched", "counts": dict(counts or {})},
    )
    backend = _telegram_backend(config or {}, opener)
    receipt = backend.send(payload, recipient)
    log_notify_event(
        {
            "dept": dept,
            "kind": "layers_batched",
            "counts": dict(counts or {}),
            "recipient": recipient,
            "success": bool(getattr(receipt, "success", False)),
            "error": getattr(receipt, "error", None),
        },
        notify_log_path=notify_log_path,
    )
    return receipt


__all__ = [
    "notify_layer_fired",
    "notify_layers_batched",
    "format_batched_line",
    "log_notify_event",
    "DEFAULT_ACCOUNT",
    "LAYER_FIRE_GLYPH",
    "BRIEF_LAYERS",
    "BRIEF_MAX_CHARS",
    "BRIEF_TRUNCATION_MARKER",
    "MissingCredentialError",
]
