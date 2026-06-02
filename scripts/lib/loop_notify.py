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
  immediate :  ``🔁 <dept> · L<N> fired — <first line of summary.md>``
  batched   :  ``🔁 <dept> · L2 ×3, L3 ×1``

Both go to Telegram via the promoted ``TelegramBackend``. The recipient
``chat_id`` is resolved dept-agnostically from ``config`` (the same config
the rest of the loop already loads) via ``resolve_recipients`` — no hardcoded
chat_id. ``TELEGRAM_BOT_TOKEN`` is read from the env by the backend; a missing
token raises a clear ``MissingCredentialError`` (mirrors Maya's pattern) which
the backend converts into a failed-but-non-fatal receipt.

Testability
-----------
``send_*`` accept an injectable ``opener`` (urllib-like callable). Tests pass a
fake opener and assert the URL, chat_id, parse_mode, and message text — no live
HTTP. The same ``opener`` is threaded into ``TelegramBackend(config, _opener=…)``.

Stdlib only.
"""

from __future__ import annotations

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
# call so couple-mode depts (e.g. Maya → {{OPERATOR}}+{{OPERATOR_2}}) can fan out.
DEFAULT_ACCOUNT = "{{OPERATOR}}"

LAYER_FIRE_GLYPH = "🔁"


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
):
    """Send the IMMEDIATE per-layer ping for L1 / L4 (one line each).

    Message: ``🔁 <dept> · L<N> fired — <first line of summary.md>``
    (the ``— …`` tail is omitted when the summary is missing/empty).

    Args:
        dept: dept slug / label (e.g. "maya").
        layer: layer number (int or str, e.g. 1 / "1" / 4).
        summary_path: path to the layer's summary.md (first line → ping tail).
        config: full dept config dict (provides accounts → telegram_chat_id).
        account: account name(s) to ping (default "{{OPERATOR}}"); str or list.
        opener: injectable urllib-like callable for tests; None → live urllib.

    Returns:
        The ``DeliveryReceipt`` from the Telegram backend (success flag +
        error). A missing ``TELEGRAM_BOT_TOKEN`` yields a non-fatal failed
        receipt (backend converts the MissingCredentialError); a missing
        chat_id in config raises ValueError (clear, fail-fast config error).
    """
    layer_str = str(layer).lstrip("L").lstrip("l")
    first = _first_line_of_summary(summary_path)
    text = f"{LAYER_FIRE_GLYPH} {dept} · L{layer_str} fired"
    if first:
        text += f" — {first}"

    recipient = _resolve_chat_recipient(config or {}, account)
    payload = NotificationPayload(
        subject=text,
        markdown_body="",
        metadata={"dept": dept, "layer": layer_str, "kind": "layer_fired"},
    )
    backend = _telegram_backend(config or {}, opener)
    return backend.send(payload, recipient)


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
):
    """Send ONE batched ping coalescing L2/L3 fires for a tick.

    Message: ``🔁 <dept> · L2 ×3, L3 ×1`` (layers with count 0 omitted).

    Args:
        dept: dept slug / label.
        counts: mapping of layer → fire count this tick, e.g. {"2": 3, "3": 1}.
        config / account / opener: as in ``notify_layer_fired``.

    Returns:
        The ``DeliveryReceipt``, or ``None`` when no layer fired (nothing sent).
    """
    text = format_batched_line(dept, counts)
    if not text:
        return None
    recipient = _resolve_chat_recipient(config or {}, account)
    payload = NotificationPayload(
        subject=text,
        markdown_body="",
        metadata={"dept": dept, "kind": "layers_batched", "counts": dict(counts or {})},
    )
    backend = _telegram_backend(config or {}, opener)
    return backend.send(payload, recipient)


__all__ = [
    "notify_layer_fired",
    "notify_layers_batched",
    "format_batched_line",
    "DEFAULT_ACCOUNT",
    "LAYER_FIRE_GLYPH",
    "MissingCredentialError",
]
