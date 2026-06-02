"""
notify — pluggable notification delivery layer, shared across all depts.

PROMOTED to the framework (WS3, 2026-06-02) from Maya's `lib/notify.py` so
every department (tony, cgp, and future depts) inherits BOTH email and
Telegram delivery — not a stripped Telegram-only helper (Joris decision:
"all agents get the email capabilities"). The module is DEPT-AGNOSTIC: no
Maya-specific recipients, sender identity, or hard imports. Recipients, the
sender identity, and the dept label all come from each dept's `config`.
When a dept has no SMTP credentials yet, email degrades gracefully (the
receipt is success=False but non-fatal) while Telegram still delivers —
per-channel failure isolation guarantees one channel never aborts another.

Originally: Maya is perceived by clients as an **email-delivering employee**
(formal, archivable, professional). Email is the primary channel for her
cron outputs; Telegram is reserved for interactive chat and urgent alerts.
That posture is now available to any dept via config.

Architecture
------------
The module mirrors `pool_backend.Pool` / `NotionPool`: a `NotificationBackend`
Protocol + concrete implementations. `SMTPEmailBackend` is the first concrete
email backend; Resend / Postmark / SendGrid swap-in lands at S7+ packaging
without consumer-side changes.

Public surface
--------------
- ``NotificationPayload`` — frozen dataclass: subject, markdown_body,
  attachments, priority, metadata.
- ``DeliveryReceipt`` — frozen dataclass: channel, recipient, success,
  delivered_at (ISO Paris-local), error, backend_response.
- ``NotificationBackend`` — typing.Protocol; concrete impls implement
  ``send(payload, recipient) -> DeliveryReceipt``.
- ``SMTPEmailBackend`` — stdlib smtplib + email.mime, MIME multipart/alternative
  (plain text + HTML), attachments, STARTTLS or SMTPS.
- ``TelegramBackend`` — direct HTTPS to api.telegram.org/bot{token}/sendMessage,
  MarkdownV2 with escape, 4096-char truncation.
- ``render_markdown_to_html(md_body)`` — stdlib-only Markdown→HTML converter
  (headings, bold/italic/code, links, lists, paragraphs, code fences).
- ``resolve_recipients(account_used, channels, config)`` — config-driven
  channel → recipient mapping.
- ``deliver(payload, channels, account_used, config)`` — fan-out across
  channels; per-channel failures do not abort other channels.
- ``notify_cron_completion(cron_name, payload, account_used, config)`` —
  reads ``config.notifications[cron_name].channels``.
- ``notify_alert(reason, severity, metadata, config)`` — high-priority,
  defaults to ["telegram-alert", "email"] to ``accounts._system``.

Stdlib only — no `markdown`, no `mistune`, no `requests`. `yaml` allowed
(already a project dep for ``config.yaml``).

Safety
------
- Credentials read from ``os.environ`` (``SMTP_USER``, ``SMTP_PASSWORD``,
  ``TELEGRAM_BOT_TOKEN``). Never printed. Missing → fast clear error.
- A failure on one channel never aborts the others — each ``DeliveryReceipt``
  carries its own success/error.
- Audit log line written per delivery (see ``audit_log.append_daily_line``).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders as email_encoders
from pathlib import Path
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

# Step 6 (2026-05-25): unified dry-run plumbing. Concrete senders check
# ``dry_run.is_dry_run()`` at the top of their public ``send()`` method
# and divert to a JSONL log when ``MAYA_DRY_RUN=1`` is set in the env.
# See ``lib/dry_run.py`` for the contract.
#
# PROMOTION NOTE (WS3): the framework copy must NOT hard-depend on Maya's
# ``lib.dry_run`` module (it does not exist in every dept repo / in the
# framework). We import it best-effort; absent it, we fall back to a
# self-contained no-op shim so the module is importable everywhere and
# dry-run simply stays OFF unless a dept provides its own ``lib.dry_run``.
try:  # pragma: no cover - import-environment dependent
    from lib import dry_run  # type: ignore
except Exception:  # noqa: BLE001 - any import failure → safe no-op shim
    class _NoOpDryRun:
        """Fallback dry-run shim: always off, log_write is a no-op."""

        @staticmethod
        def is_dry_run() -> bool:
            return False

        @staticmethod
        def log_write(category: str, payload: dict) -> None:
            return None

    dry_run = _NoOpDryRun()  # type: ignore

PARIS_TZ = ZoneInfo("Europe/Paris")

# ─── Constants ──────────────────────────────────────────────────────────────

CHANNEL_EMAIL = "email"
CHANNEL_TELEGRAM_ALERT = "telegram-alert"
SUPPORTED_CHANNELS = (CHANNEL_EMAIL, CHANNEL_TELEGRAM_ALERT)

PRIORITY_NORMAL = "normal"
PRIORITY_URGENT = "urgent"

# Telegram limits
TELEGRAM_MESSAGE_MAX = 4096
TELEGRAM_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
TELEGRAM_TRUNCATION_SUFFIX = "...(truncated, see email)"

# SMTP defaults
SMTP_CONNECTION_TIMEOUT_S = 10

# ─── Dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NotificationPayload:
    """Subject + body for a notification, channel-agnostic.

    Args:
        subject: e.g. "[Maya] Morning Sync — 2026-05-13 — 5 leads".
        markdown_body: Body in Markdown. Backends render to their native format.
        attachments: Optional file paths. Email = MIME attach; Telegram =
            sendDocument if ≤20MB, else ignore with warning.
        priority: "normal" or "urgent". Urgent = phone-buzz worthy.
        metadata: Caller-provided context (slug, tier, cron_name, etc.). Used
            for receipts/logging only.
    """

    subject: str
    markdown_body: str
    attachments: tuple = ()
    priority: str = PRIORITY_NORMAL
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    """Per-channel delivery result."""

    channel: str
    recipient: str
    success: bool
    delivered_at: str  # ISO 8601 Paris-local
    error: Optional[str] = None
    backend_response: Optional[dict] = None


# ─── Backend protocol ──────────────────────────────────────────────────────


class NotificationBackend(Protocol):
    """Pluggable notification backend.

    Concrete implementations: ``SMTPEmailBackend``, ``TelegramBackend``.
    Future: ``ResendEmailBackend``, ``PostmarkEmailBackend``, ``SendGridEmailBackend``.
    """

    name: str

    def send(
        self, payload: NotificationPayload, recipient: str
    ) -> DeliveryReceipt:  # pragma: no cover - Protocol
        """Single-recipient send. Backends translate Markdown → native format."""
        ...


# ─── Markdown → HTML ───────────────────────────────────────────────────────

_HTML_ESCAPE = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}


def _html_escape(s: str) -> str:
    out = []
    for ch in s:
        out.append(_HTML_ESCAPE.get(ch, ch))
    return "".join(out)


_INLINE_PATTERNS = [
    # Order matters: code first (so its contents aren't further interpreted),
    # then links, then bold, then italic.
    # Code spans: `code`
    (re.compile(r"`([^`\n]+)`"), lambda m: f"<code>{_html_escape(m.group(1))}</code>"),
    # Links: [text](url)
    (
        re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"),
        lambda m: f'<a href="{_html_escape(m.group(2))}">{_html_escape(m.group(1))}</a>',
    ),
    # Bold: **text**
    (
        re.compile(r"\*\*([^*\n]+)\*\*"),
        lambda m: f"<strong>{_html_escape(m.group(1))}</strong>",
    ),
    # Italic: *text* (word-boundary; avoid matching inside identifiers).
    # Strategy: require the * to be at start/end of token (not adjacent to alnum).
    (
        re.compile(r"(?<![\w*])\*([^\s*][^*\n]*?[^\s*]|\S)\*(?![\w*])"),
        lambda m: f"<em>{_html_escape(m.group(1))}</em>",
    ),
]


# A sentinel char unlikely to appear in real Markdown bodies, used to
# protect already-rendered HTML tags from later inline pattern runs.
_PROTECT_OPEN = "\x01"
_PROTECT_CLOSE = "\x02"


def _render_inline(text: str) -> str:
    """Apply inline patterns. We protect generated tags so later passes
    don't reinterpret them. We HTML-escape text *outside* generated tags
    in a second pass.
    """
    # Step 1: walk patterns; replace matches with sentinel-wrapped tags.
    # To avoid re-matching inside generated HTML, we operate on a list of
    # spans where each span is either (kind="text", value=str) or
    # (kind="html", value=str).
    spans: list[tuple[str, str]] = [("text", text)]
    for pat, repl in _INLINE_PATTERNS:
        new_spans: list[tuple[str, str]] = []
        for kind, value in spans:
            if kind == "html":
                new_spans.append((kind, value))
                continue
            pos = 0
            for m in pat.finditer(value):
                if m.start() > pos:
                    new_spans.append(("text", value[pos : m.start()]))
                new_spans.append(("html", repl(m)))
                pos = m.end()
            if pos < len(value):
                new_spans.append(("text", value[pos:]))
        spans = new_spans

    # Step 2: html-escape text spans; concatenate.
    out = []
    for kind, value in spans:
        if kind == "text":
            out.append(_html_escape(value))
        else:
            out.append(value)
    return "".join(out)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^(\s*)\d+\.\s+(.+)$")
_HR_RE = re.compile(r"^\s*---+\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```(.*)$")


def render_markdown_to_html(md_body: str) -> str:
    """Render a Markdown body to a safe HTML fragment (no surrounding ``<html>``).

    Supported subset (sufficient for Maya's Morning Sync / Draft Batch / etc.):

    - Headings: ``# H1`` / ``## H2`` / ``### H3`` (up to ``######``)
    - Bold: ``**text**``
    - Italic: ``*text*`` (word-boundary aware)
    - Code: `` `code` ``
    - Links: ``[text](url)``
    - Unordered lists: lines starting with ``- `` or ``* ``
    - Ordered lists: lines starting with ``1. ``
    - Horizontal rule: ``---``
    - Code fences: ```` ``` ```` ... ```` ``` ````
    - Blank-line-separated paragraphs

    No external dependency. Output is safe HTML (escapes ``<`` / ``>`` / ``&`` /
    ``"`` in text content). Generated tag attributes are themselves
    HTML-escaped.

    Args:
        md_body: Markdown source.

    Returns:
        HTML fragment (no doctype, no ``<html>`` wrapper).
    """
    if not md_body:
        return ""

    lines = md_body.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    paragraph: list[str] = []
    in_ul = False
    in_ol = False

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            text = " ".join(paragraph).strip()
            if text:
                out.append(f"<p>{_render_inline(text)}</p>")
            paragraph = []

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        # Code fence
        fence_match = _CODE_FENCE_RE.match(stripped)
        if fence_match:
            flush_paragraph()
            close_lists()
            i += 1
            code_lines: list[str] = []
            while i < n and not _CODE_FENCE_RE.match(lines[i].rstrip()):
                code_lines.append(lines[i])
                i += 1
            # Skip closing fence if present
            if i < n:
                i += 1
            out.append(
                "<pre><code>" + _html_escape("\n".join(code_lines)) + "</code></pre>"
            )
            continue

        # Blank line: paragraph break
        if not stripped:
            flush_paragraph()
            close_lists()
            i += 1
            continue

        # Horizontal rule
        if _HR_RE.match(stripped):
            flush_paragraph()
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        # Heading
        h_match = _HEADING_RE.match(stripped)
        if h_match:
            flush_paragraph()
            close_lists()
            level = min(len(h_match.group(1)), 6)
            content = _render_inline(h_match.group(2).strip())
            out.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # Numbered list
        num_match = _NUMBERED_RE.match(stripped)
        if num_match:
            flush_paragraph()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            # Greedy: gather continuation lines (indented or non-bullet,
            # non-blank) into the same list item.
            li_buf = [num_match.group(2).strip()]
            j = i + 1
            while j < n:
                nxt = lines[j].rstrip()
                if not nxt:
                    break
                if _BULLET_RE.match(nxt) or _NUMBERED_RE.match(nxt):
                    break
                if _HR_RE.match(nxt) or _HEADING_RE.match(nxt) or _CODE_FENCE_RE.match(nxt):
                    break
                # Continuation — strip leading whitespace for clean join
                li_buf.append(nxt.lstrip())
                j += 1
            out.append(f"<li>{_render_inline(' '.join(li_buf))}</li>")
            i = j
            continue

        # Bullet list
        bul_match = _BULLET_RE.match(stripped)
        if bul_match:
            flush_paragraph()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            # Greedy continuation (same logic as numbered)
            li_buf = [bul_match.group(2).strip()]
            j = i + 1
            while j < n:
                nxt = lines[j].rstrip()
                if not nxt:
                    break
                if _BULLET_RE.match(nxt) or _NUMBERED_RE.match(nxt):
                    break
                if _HR_RE.match(nxt) or _HEADING_RE.match(nxt) or _CODE_FENCE_RE.match(nxt):
                    break
                li_buf.append(nxt.lstrip())
                j += 1
            out.append(f"<li>{_render_inline(' '.join(li_buf))}</li>")
            i = j
            continue

        # Regular paragraph line
        if in_ul or in_ol:
            close_lists()
        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    close_lists()
    return "\n".join(out)


# ─── HTML email wrapper ────────────────────────────────────────────────────

_EMAIL_HTML_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif; "
    "max-width: 720px; "
    "margin: 0 auto; "
    "padding: 24px 16px; "
    "color: #1a1a1a; "
    "line-height: 1.5; "
    "font-size: 15px;"
)


def _wrap_html_email(html_fragment: str) -> str:
    """Wrap an HTML fragment in a minimal email-safe document."""
    return (
        '<!DOCTYPE html>\n'
        '<html><head><meta charset="utf-8"></head>\n'
        f'<body style="{_EMAIL_HTML_STYLE}">\n'
        f"{html_fragment}\n"
        "</body></html>"
    )


# ─── SMTP backend ──────────────────────────────────────────────────────────


class MissingCredentialError(RuntimeError):
    """Raised when an env-var credential is missing. Never carries the value."""


class SMTPEmailBackend:
    """SMTP/STARTTLS email backend. First concrete email impl.

    Credentials come from environment (set by ``tools/sync-secrets.sh``):
      - ``SMTP_USER`` — SMTP username (typically the sender address or alias)
      - ``SMTP_PASSWORD`` — SMTP App Password (Gmail/Postmark/etc.)

    Config (from ``config.yaml`` ``email.smtp`` + ``email.sender_*``):
      - ``host`` (str, e.g. "smtp.gmail.com")
      - ``port`` (int, e.g. 587 for STARTTLS or 465 for SMTPS)
      - ``use_tls`` (bool, default True)
      - ``sender_address`` (str, the From: address)
      - ``sender_name`` (str, the From: display name)
    """

    name = "email"

    def __init__(self, config: dict):
        """Initialize from ``config['email']`` dict (``smtp`` + ``sender_*`` keys).

        Does NOT read env vars at construction (so import-time use stays safe).
        Env vars are read at ``send()`` time and validated there.
        """
        if not isinstance(config, dict):
            raise TypeError(f"SMTPEmailBackend config must be dict, got {type(config)}")
        smtp_cfg = config.get("smtp") or {}
        self.host = smtp_cfg.get("host", "smtp.gmail.com")
        self.port = int(smtp_cfg.get("port", 587))
        self.use_tls = bool(smtp_cfg.get("use_tls", True))
        # Dept-agnostic defaults (WS3): no Maya-specific sender hardcoded.
        # Each dept supplies its own sender via config['email'].sender_address
        # / sender_name (or under the nested smtp block).
        self.sender_address = (
            config.get("sender_address")
            or smtp_cfg.get("sender_address")
            or "noreply@example.com"
        )
        self.sender_name = (
            config.get("sender_name") or smtp_cfg.get("sender_name") or "Bubble Ops"
        )

    def _read_creds(self) -> tuple[str, str]:
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        if not user or not password:
            missing = []
            if not user:
                missing.append("SMTP_USER")
            if not password:
                missing.append("SMTP_PASSWORD")
            raise MissingCredentialError(
                f"SMTP credentials missing from env: {', '.join(missing)} [REDACTED]. "
                "Run tools/sync-secrets.sh after operator-set-secret.sh."
            )
        return user, password

    def _build_message(
        self, payload: NotificationPayload, recipient: str
    ) -> MIMEMultipart:
        """Build a multipart/alternative MIME message with optional attachments."""
        # Outer = multipart/mixed if attachments, else multipart/alternative
        if payload.attachments:
            outer = MIMEMultipart("mixed")
        else:
            outer = MIMEMultipart("alternative")

        # Subject — RFC 2047 encode if non-ASCII (Header handles UTF-8)
        try:
            payload.subject.encode("ascii")
            outer["Subject"] = payload.subject
        except UnicodeEncodeError:
            outer["Subject"] = Header(payload.subject, "utf-8").encode()

        from_header = f"{self.sender_name} <{self.sender_address}>"
        outer["From"] = from_header
        outer["To"] = recipient
        outer["Reply-To"] = self.sender_address

        # If we have attachments, alternative goes inside the mixed root
        if payload.attachments:
            alt = MIMEMultipart("alternative")
            outer.attach(alt)
        else:
            alt = outer

        text_part = MIMEText(payload.markdown_body, "plain", "utf-8")
        html_body = _wrap_html_email(render_markdown_to_html(payload.markdown_body))
        html_part = MIMEText(html_body, "html", "utf-8")
        alt.attach(text_part)
        alt.attach(html_part)

        if payload.attachments:
            for att_path in payload.attachments:
                att = Path(att_path)
                if not att.exists():
                    # Skip silently — caller's responsibility; will surface in
                    # logs via the receipt's metadata. We don't abort.
                    continue
                ctype, _ = mimetypes.guess_type(str(att))
                if ctype is None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                part = MIMEBase(maintype, subtype)
                with open(att, "rb") as f:
                    part.set_payload(f.read())
                email_encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=att.name,
                )
                outer.attach(part)

        return outer

    def send(
        self, payload: NotificationPayload, recipient: str
    ) -> DeliveryReceipt:
        """Send the payload via SMTP. Defensive — never raises.

        Returns a ``DeliveryReceipt`` with ``success=False`` and ``error``
        populated on any failure (connection, auth, MIME build, etc.).
        """
        delivered_at = _now_iso_paris()
        # Step 6 dry-run gate: log the would-have-been-send + return success.
        # Returning success=False would cascade into "notify failed → cron
        # marks itself errored", which is not what we want during a soak.
        if dry_run.is_dry_run():
            dry_run.log_write("notify_email", {
                "recipient": recipient,
                "subject": payload.subject,
                "priority": payload.priority,
                "body_len": len(payload.markdown_body or ""),
                "attachments": list(payload.attachments or []),
                "sender_address": self.sender_address,
            })
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=True,
                delivered_at=delivered_at,
                error=None,
                backend_response={"dry_run": True},
            )
        try:
            user, password = self._read_creds()
        except MissingCredentialError as exc:
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=False,
                delivered_at=delivered_at,
                error=str(exc),
            )

        try:
            msg = self._build_message(payload, recipient)
            raw = msg.as_string()
        except Exception as exc:  # noqa: BLE001 - want all build failures captured
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=False,
                delivered_at=delivered_at,
                error=f"MIME build failed: {exc}",
            )

        # Recipient may be a comma-joined list (multi-account To:);
        # split for the SMTP envelope so each address gets RCPT TO.
        envelope_to = [
            addr.strip() for addr in str(recipient).split(",") if addr.strip()
        ] or [recipient]
        try:
            if self.use_tls and self.port != 465:
                smtp = smtplib.SMTP(self.host, self.port, timeout=SMTP_CONNECTION_TIMEOUT_S)
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            else:
                # SMTPS (port 465 typical) or plain (rare)
                if self.port == 465:
                    smtp = smtplib.SMTP_SSL(
                        self.host,
                        self.port,
                        timeout=SMTP_CONNECTION_TIMEOUT_S,
                        context=ssl.create_default_context(),
                    )
                else:
                    smtp = smtplib.SMTP(
                        self.host, self.port, timeout=SMTP_CONNECTION_TIMEOUT_S
                    )
                    smtp.ehlo()
            try:
                smtp.login(user, password)
                smtp.sendmail(self.sender_address, envelope_to, raw)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            # Never let an SMTP error swallow the failure signal.
            # Avoid printing credentials.
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=False,
                delivered_at=delivered_at,
                error=f"SMTP send failed: {type(exc).__name__}: {exc}",
            )

        return DeliveryReceipt(
            channel=self.name,
            recipient=recipient,
            success=True,
            delivered_at=delivered_at,
            error=None,
            backend_response={"host": self.host, "port": self.port},
        )


# ─── Telegram backend ──────────────────────────────────────────────────────

# MarkdownV2 special characters per https://core.telegram.org/bots/api#markdownv2-style
_TELEGRAM_MDV2_ESCAPE = "_*[]()~`>#+-=|{}.!"


def _escape_telegram_text(text: str) -> str:
    """Escape arbitrary text for Telegram MarkdownV2.

    Any of ``_*[]()~`>#+-=|{}.!`` must be escaped with a backslash when
    they appear as plain text (not part of intentional markdown syntax).

    This helper assumes the input is *plain text* (not partially-marked-up).
    For full-rendering we pre-render Markdown bullets/headings into plain
    Telegram-friendly equivalents in ``_markdown_to_telegram``.
    """
    out = []
    for ch in text:
        if ch in _TELEGRAM_MDV2_ESCAPE:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _markdown_to_telegram(md_body: str) -> str:
    """Convert Maya's Markdown to Telegram MarkdownV2.

    Strategy: render line-by-line; preserve **bold**, *italic*, `code`,
    [link](url) as MarkdownV2-equivalent; convert lists/headings to
    plain-text styled prefixes. Escape every other special char.

    Telegram MarkdownV2:
      *bold*      → bold (note: single asterisk in Telegram MD2)
      _italic_    → italic
      `code`      → code
      [text](url) → link

    To avoid ambiguity with our Markdown's ``*italic*``, we translate:
      ``**bold**`` → ``*bold*`` (TG bold uses single asterisk)
      ``*italic*`` → ``_italic_``
    """
    lines = md_body.split("\n")
    out: list[str] = []
    in_code_fence = False
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        # Code fence toggling
        fence_match = _CODE_FENCE_RE.match(line)
        if fence_match:
            in_code_fence = not in_code_fence
            # Telegram uses ``` for code fences, escape language tag chars
            out.append("```")
            continue
        if in_code_fence:
            # Inside code fences, no escaping required, but newlines preserved
            out.append(line)
            continue

        # Heading → bold prefix
        h_match = _HEADING_RE.match(line)
        if h_match:
            content = _render_inline_telegram(h_match.group(2).strip())
            out.append(f"*{content}*")
            continue

        # HR
        if _HR_RE.match(line):
            out.append(_escape_telegram_text("---"))
            continue

        # Bullet list
        bul_match = _BULLET_RE.match(line)
        if bul_match:
            indent_spaces = len(bul_match.group(1))
            indent = " " * indent_spaces
            content = _render_inline_telegram(bul_match.group(2).strip())
            # Use a bullet glyph; escape it
            out.append(f"{indent}{_escape_telegram_text('•')} {content}")
            continue

        # Numbered list
        num_match = _NUMBERED_RE.match(line)
        if num_match:
            indent_spaces = len(num_match.group(1))
            indent = " " * indent_spaces
            content = _render_inline_telegram(num_match.group(2).strip())
            # Telegram MDV2 requires escape of the dot.
            out.append(f"{indent}1{_escape_telegram_text('.')} {content}")
            continue

        # Regular line
        if line.strip() == "":
            out.append("")
        else:
            out.append(_render_inline_telegram(line))
    return "\n".join(out)


def _render_inline_telegram(text: str) -> str:
    """Render inline Markdown to Telegram MarkdownV2 with escaping.

    Walks the same patterns as ``_render_inline`` but emits Telegram-flavored
    output, then escapes literal special characters in the un-marked-up
    segments.
    """
    # Spans: ("text", str) or ("tg", str_already_telegram_mdv2)
    spans: list[tuple[str, str]] = [("text", text)]

    # Apply patterns in order
    # Code spans first
    new_spans = []
    for kind, value in spans:
        if kind != "text":
            new_spans.append((kind, value))
            continue
        pos = 0
        for m in re.finditer(r"`([^`\n]+)`", value):
            if m.start() > pos:
                new_spans.append(("text", value[pos : m.start()]))
            # Inside code, escape \ and ` per MarkdownV2 inside `pre`/`code` rules
            code_content = m.group(1).replace("\\", "\\\\").replace("`", "\\`")
            new_spans.append(("tg", f"`{code_content}`"))
            pos = m.end()
        if pos < len(value):
            new_spans.append(("text", value[pos:]))
    spans = new_spans

    # Links
    new_spans = []
    for kind, value in spans:
        if kind != "text":
            new_spans.append((kind, value))
            continue
        pos = 0
        for m in re.finditer(r"\[([^\]]+)\]\(([^)\s]+)\)", value):
            if m.start() > pos:
                new_spans.append(("text", value[pos : m.start()]))
            # Link text is escaped; URL has its own escaping rules (backslash
            # escape ) and \ ).
            link_text = _escape_telegram_text(m.group(1))
            url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
            new_spans.append(("tg", f"[{link_text}]({url})"))
            pos = m.end()
        if pos < len(value):
            new_spans.append(("text", value[pos:]))
    spans = new_spans

    # Bold **text** → *text*
    new_spans = []
    for kind, value in spans:
        if kind != "text":
            new_spans.append((kind, value))
            continue
        pos = 0
        for m in re.finditer(r"\*\*([^*\n]+)\*\*", value):
            if m.start() > pos:
                new_spans.append(("text", value[pos : m.start()]))
            content = _escape_telegram_text(m.group(1))
            new_spans.append(("tg", f"*{content}*"))
            pos = m.end()
        if pos < len(value):
            new_spans.append(("text", value[pos:]))
    spans = new_spans

    # Italic *text* → _text_
    new_spans = []
    for kind, value in spans:
        if kind != "text":
            new_spans.append((kind, value))
            continue
        pos = 0
        for m in re.finditer(
            r"(?<![\w*])\*([^\s*][^*\n]*?[^\s*]|\S)\*(?![\w*])", value
        ):
            if m.start() > pos:
                new_spans.append(("text", value[pos : m.start()]))
            content = _escape_telegram_text(m.group(1))
            new_spans.append(("tg", f"_{content}_"))
            pos = m.end()
        if pos < len(value):
            new_spans.append(("text", value[pos:]))
    spans = new_spans

    # Concatenate; escape leftover text spans
    out = []
    for kind, value in spans:
        if kind == "text":
            out.append(_escape_telegram_text(value))
        else:
            out.append(value)
    return "".join(out)


class TelegramBackend:
    """Telegram Bot API backend for urgent alerts (and as a fallback channel).

    Reads ``TELEGRAM_BOT_TOKEN`` from the environment (set by
    ``tools/sync-secrets.sh``). Sends to a ``chat_id`` recipient via
    ``api.telegram.org/bot{token}/sendMessage`` (or ``sendDocument`` for
    attachments ≤20MB).
    """

    name = "telegram-alert"

    def __init__(self, config: dict, _opener=None):
        """Initialize. ``config`` is the parent config dict (currently unused
        but reserved for future Telegram-specific options like parse_mode
        override or rate limits).

        Args:
            config: full config dict.
            _opener: injectable for tests (must be a callable mimicking
                ``urllib.request.urlopen(request, timeout=…)``). Default uses
                ``urllib.request.urlopen``.
        """
        self.config = config or {}
        self._opener = _opener or urllib.request.urlopen

    def _read_token(self) -> str:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise MissingCredentialError(
                "TELEGRAM_BOT_TOKEN missing from env [REDACTED]. "
                "Run tools/sync-secrets.sh."
            )
        return token

    def _build_telegram_body(self, payload: NotificationPayload) -> str:
        # Subject as bold first line, then body translated to MarkdownV2.
        subject_line = f"*{_escape_telegram_text(payload.subject)}*"
        body_md2 = _markdown_to_telegram(payload.markdown_body)
        full = subject_line + "\n\n" + body_md2

        if len(full) <= TELEGRAM_MESSAGE_MAX:
            return full

        # Truncate; preserve subject line and as much body as possible
        suffix = "\n\n" + _escape_telegram_text(TELEGRAM_TRUNCATION_SUFFIX)
        # Budget for body: cap - subject - separator - suffix length
        max_body = TELEGRAM_MESSAGE_MAX - len(subject_line) - 2 - len(suffix)
        if max_body < 0:
            # Pathological subject — just return subject + suffix
            return subject_line[:TELEGRAM_MESSAGE_MAX]
        truncated_body = body_md2[:max_body]
        return subject_line + "\n\n" + truncated_body + suffix

    def _send_one(
        self,
        payload: NotificationPayload,
        chat_id: str,
        token: str,
        text: str,
    ) -> DeliveryReceipt:
        """Send one Telegram message to a single chat_id. Helper for ``send``."""
        delivered_at = _now_iso_paris()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body_payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        data = json.dumps(body_payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=SMTP_CONNECTION_TIMEOUT_S) as resp:
                resp_bytes = resp.read()
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return DeliveryReceipt(
                channel=self.name,
                recipient=chat_id,
                success=False,
                delivered_at=delivered_at,
                error=f"Telegram HTTP {exc.code}: {body[:200]}",
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(
                channel=self.name,
                recipient=chat_id,
                success=False,
                delivered_at=delivered_at,
                error=f"Telegram send failed: {type(exc).__name__}: {exc}",
            )

        try:
            resp_json = json.loads(resp_bytes.decode("utf-8"))
        except Exception:
            resp_json = {}

        if status >= 400 or not resp_json.get("ok", True):
            return DeliveryReceipt(
                channel=self.name,
                recipient=chat_id,
                success=False,
                delivered_at=delivered_at,
                error=f"Telegram API not ok: status={status}, body={str(resp_json)[:200]}",
            )

        # Best-effort attachment notes (Telegram = buzzer, not file carrier).
        attachment_errors: list[str] = []
        if payload.attachments:
            for att_path in payload.attachments:
                att = Path(att_path)
                if not att.exists():
                    attachment_errors.append(f"{att.name}: missing")
                    continue
                size = att.stat().st_size
                if size > TELEGRAM_DOCUMENT_MAX_BYTES:
                    attachment_errors.append(f"{att.name}: too large ({size} B)")
                    continue
                attachment_errors.append(f"{att.name}: skipped (see email)")

        msg_id = None
        result = resp_json.get("result") if isinstance(resp_json, dict) else None
        if isinstance(result, dict):
            msg_id = result.get("message_id")

        backend_response = {"message_id": msg_id}
        if attachment_errors:
            backend_response["attachment_notes"] = attachment_errors

        return DeliveryReceipt(
            channel=self.name,
            recipient=chat_id,
            success=True,
            delivered_at=delivered_at,
            error=None,
            backend_response=backend_response,
        )

    def send(
        self, payload: NotificationPayload, recipient: str
    ) -> DeliveryReceipt:
        """Send via Telegram Bot API. Defensive — returns receipt with success
        flag; never raises.

        ``recipient`` may be a single chat_id or a comma-joined list of
        chat_ids (multi-account routing). For multi-recipient calls we fan
        out one API call per chat_id and aggregate the receipts: success
        only if ALL deliveries succeeded; the receipt's ``error`` field
        carries per-recipient detail when any failed.
        """
        delivered_at = _now_iso_paris()
        # Step 6 dry-run gate: log + return success (same posture as
        # SMTPEmailBackend). Multi-recipient lists are logged as the raw
        # recipient string so the operator can audit the would-have-been
        # fan-out without us having to recompute the split here.
        if dry_run.is_dry_run():
            dry_run.log_write("notify_telegram", {
                "recipient": recipient,
                "subject": payload.subject,
                "priority": payload.priority,
                "body_len": len(payload.markdown_body or ""),
                "attachments": list(payload.attachments or []),
            })
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=True,
                delivered_at=delivered_at,
                error=None,
                backend_response={"dry_run": True},
            )
        try:
            token = self._read_token()
        except MissingCredentialError as exc:
            return DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=False,
                delivered_at=delivered_at,
                error=str(exc),
            )

        text = self._build_telegram_body(payload)

        chat_ids = [c.strip() for c in str(recipient).split(",") if c.strip()]
        if not chat_ids:
            chat_ids = [recipient]

        if len(chat_ids) == 1:
            return self._send_one(payload, chat_ids[0], token, text)

        per_receipts = [
            self._send_one(payload, cid, token, text) for cid in chat_ids
        ]
        all_ok = all(r.success for r in per_receipts)
        errors = [
            f"{r.recipient}: {r.error}" for r in per_receipts if not r.success
        ]
        backend_response = {
            "per_recipient": [
                {
                    "chat_id": r.recipient,
                    "success": r.success,
                    "message_id": (r.backend_response or {}).get("message_id"),
                }
                for r in per_receipts
            ]
        }
        return DeliveryReceipt(
            channel=self.name,
            recipient=recipient,
            success=all_ok,
            delivered_at=delivered_at,
            error=("; ".join(errors) if errors else None),
            backend_response=backend_response,
        )


# ─── Recipient resolution ──────────────────────────────────────────────────


def resolve_recipients(
    account_used,
    channels: list[str],
    config: dict,
) -> dict[str, str]:
    """Map each channel to a concrete recipient for the given account(s).

    Reads ``config['accounts'][account_used]`` then falls back to
    ``config['accounts']['_system']`` for unknown accounts.

    Args:
        account_used: a single account name (str — "Joris" | "Jade" |
            "_system" | unknown → fall back to "_system") OR a list of
            account names (e.g. ``["Joris", "Jade"]``) to address all of
            them in the same notification. When multiple accounts are
            supplied, their recipients are de-duplicated (preserving order)
            and joined with ``", "`` per channel — SMTP natively accepts
            comma-separated To: addresses, and the Telegram backend splits
            the string on commas to fan out one API call per chat_id.
        channels: list of channel names from ``SUPPORTED_CHANNELS``.
        config: full config dict (typically loaded from ``config.yaml``).

    Returns:
        dict mapping channel → recipient string. For multi-account requests
        the string is a comma-joined list (e.g. ``"a@x.com, b@y.com"``).

    Raises:
        ValueError: if a channel is not in ``SUPPORTED_CHANNELS``, if no
            valid recipient is configured for any of the requested accounts,
            or if ``account_used`` is an empty list.
    """
    # Normalize to a list of account names (preserving caller order).
    if isinstance(account_used, str):
        accounts_requested = [account_used]
    else:
        accounts_requested = list(account_used or [])
    if not accounts_requested:
        raise ValueError("account_used must be a non-empty str or list of str")

    accounts_cfg = (config or {}).get("accounts") or {}

    def _acct(name: str) -> dict:
        acct = accounts_cfg.get(name)
        if acct is None:
            acct = accounts_cfg.get("_system") or {}
        return acct if isinstance(acct, dict) else {}

    result: dict[str, str] = {}
    for channel in channels:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(
                f"Unknown channel: {channel!r}. Supported: {SUPPORTED_CHANNELS}"
            )
        recipients: list[str] = []
        seen: set[str] = set()
        for name in accounts_requested:
            acct_cfg = _acct(name)
            if channel == CHANNEL_EMAIL:
                rec = acct_cfg.get("email") or ""
            elif channel == CHANNEL_TELEGRAM_ALERT:
                rec = str(acct_cfg.get("telegram_chat_id") or "")
            else:
                rec = ""
            rec = rec.strip()
            if not rec:
                # Skip silently when a sub-account has no recipient on this
                # channel (e.g. Jade has no telegram_chat_id yet). We only
                # fail if NONE of the accounts produce a recipient.
                continue
            if rec not in seen:
                seen.add(rec)
                recipients.append(rec)
        if not recipients:
            raise ValueError(
                f"No recipient configured for account={accounts_requested!r} "
                f"channel={channel!r}. Check config.yaml accounts block."
            )
        result[channel] = ", ".join(recipients)
    return result


# ─── Backend factory ───────────────────────────────────────────────────────


def _get_backend(channel: str, config: dict) -> NotificationBackend:
    """Return a concrete backend for the channel.

    The email backend choice is configurable via ``config.email.backend``
    (default ``"smtp"``). Today only ``"smtp"`` is implemented.
    """
    email_cfg = (config or {}).get("email") or {}
    if channel == CHANNEL_EMAIL:
        backend_name = email_cfg.get("backend", "smtp")
        if backend_name == "smtp":
            return SMTPEmailBackend(email_cfg)
        raise ValueError(
            f"Unknown email backend: {backend_name!r}. "
            "Implemented: smtp. Future: resend, postmark, sendgrid."
        )
    if channel == CHANNEL_TELEGRAM_ALERT:
        return TelegramBackend(config)
    raise ValueError(
        f"Unknown channel: {channel!r}. Supported: {SUPPORTED_CHANNELS}"
    )


# ─── Top-level helpers ─────────────────────────────────────────────────────


def deliver(
    payload: NotificationPayload,
    channels: list[str],
    account_used="Joris",
    config: Optional[dict] = None,
    *,
    _backends: Optional[dict[str, NotificationBackend]] = None,
    _audit_workspace_root: Optional[Path] = None,
) -> list[DeliveryReceipt]:
    """Fan-out delivery across N channels.

    Returns one ``DeliveryReceipt`` per channel. A failure in one channel
    does NOT abort the others — each receipt carries its own ``success``
    and ``error``.

    Args:
        payload: the notification payload.
        channels: list of channel names (subset of ``SUPPORTED_CHANNELS``).
            Empty list → returns ``[]``.
        account_used: single account name (str, default "Joris") OR a list
            of account names (e.g. ``["Joris", "Jade"]``) to address all
            of them in the same notification (single email with multi-To:,
            Telegram fan-out). Unknown values fall back to "_system".
        config: full config dict. If None, an empty dict is used (will
            fail on recipient resolution unless ``_backends`` is provided
            with pre-resolved recipients).
        _backends: optional dict ``{channel: backend_instance}`` for
            test injection. If absent, backends are constructed from config.
        _audit_workspace_root: optional path to write Daily/{date}.md
            audit lines. If None, no audit log line is written (suitable
            for tests / probes).
    """
    if not channels:
        return []
    config = config or {}
    recipients = resolve_recipients(account_used, channels, config)

    receipts: list[DeliveryReceipt] = []
    for channel in channels:
        recipient = recipients[channel]
        try:
            if _backends and channel in _backends:
                backend = _backends[channel]
            else:
                backend = _get_backend(channel, config)
            receipt = backend.send(payload, recipient)
        except Exception as exc:  # noqa: BLE001 - never let one channel kill others
            receipt = DeliveryReceipt(
                channel=channel,
                recipient=recipient,
                success=False,
                delivered_at=_now_iso_paris(),
                error=f"backend setup failed: {type(exc).__name__}: {exc}",
            )
        receipts.append(receipt)

        if _audit_workspace_root is not None:
            _audit_receipt(receipt, _audit_workspace_root, payload)

    return receipts


def notify_cron_completion(
    cron_name: str,
    payload: NotificationPayload,
    account_used="Joris",
    config: Optional[dict] = None,
    **kwargs,
) -> list[DeliveryReceipt]:
    """Deliver a cron-completion notification.

    Reads ``config['notifications'][cron_name]`` for the channel list.
    If absent → defaults to ``[CHANNEL_EMAIL]``.

    If ``payload.priority == 'urgent'``, escalates by adding the
    ``on_failure`` channels (typically ``telegram-alert`` + ``email``).

    Args:
        cron_name: e.g. "morning_sync", "draft_batch", "discovery_feed_scan".
        payload: the notification payload (subject + markdown body).
        account_used: single account name (str, default "Joris" — e.g.
            "Joris" | "Jade" | "_system") OR a list of account names
            (e.g. ``["Joris", "Jade"]``) to address all of them in the
            same notification.
        config: full config dict.
        **kwargs: forwarded to ``deliver`` (e.g. ``_backends``,
            ``_audit_workspace_root``).
    """
    config = config or {}
    notif_cfg = (config.get("notifications") or {}).get(cron_name) or {}
    channels = list(notif_cfg.get("channels") or [CHANNEL_EMAIL])
    if payload.priority == PRIORITY_URGENT:
        on_failure = list(notif_cfg.get("on_failure") or [])
        for ch in on_failure:
            if ch not in channels:
                channels.append(ch)
    return deliver(payload, channels, account_used=account_used, config=config, **kwargs)


def notify_alert(
    reason: str,
    severity: str = "high",
    metadata: Optional[dict] = None,
    config: Optional[dict] = None,
    **kwargs,
) -> list[DeliveryReceipt]:
    """Deliver an urgent alert (kill switch, captcha, quota breach, …).

    Always priority=urgent. Default channels = ``["telegram-alert", "email"]``
    to ``accounts._system``. Subject is built from the reason.

    Args:
        reason: short text describing the event (used in subject + body).
        severity: "high" | "critical" | "info" (default "high"); reflected
            in subject prefix.
        metadata: optional dict added to the payload's metadata.
        config: full config dict.
        **kwargs: forwarded to ``deliver``.
    """
    config = config or {}
    sev_prefix = {
        "critical": "[CRITICAL]",
        "high": "[ALERT]",
        "info": "[INFO]",
    }.get(severity, "[ALERT]")
    # Dept-agnostic label (WS3): derive from config rather than hardcoding
    # "Maya". Honors config['dept_label'] / config['dept'] / config['agent_name'],
    # falling back to a neutral "Bubble Ops".
    dept_label = (
        config.get("dept_label")
        or config.get("dept")
        or config.get("agent_name")
        or "Bubble Ops"
    )
    subject = f"{sev_prefix} {dept_label} — {reason}"
    body_lines = [
        f"# {sev_prefix} {dept_label} — {reason}",
        "",
        f"**Severity:** {severity}",
        f"**Timestamp:** {_now_iso_paris()}",
    ]
    md = (metadata or {})
    if md:
        body_lines.append("")
        body_lines.append("## Context")
        for k, v in md.items():
            body_lines.append(f"- **{k}:** {v}")
    payload = NotificationPayload(
        subject=subject,
        markdown_body="\n".join(body_lines),
        priority=PRIORITY_URGENT,
        metadata=dict(md, severity=severity, reason=reason),
    )
    # Read default alert channels from config if present, else fall back.
    alert_cfg = (config.get("notifications") or {}).get("_alert_default") or {}
    channels = list(
        alert_cfg.get("channels") or [CHANNEL_TELEGRAM_ALERT, CHANNEL_EMAIL]
    )
    return deliver(payload, channels, account_used="_system", config=config, **kwargs)


# ─── Internal helpers ──────────────────────────────────────────────────────


def _now_iso_paris() -> str:
    """ISO-8601 timestamp in Europe/Paris timezone, seconds precision."""
    return datetime.now(PARIS_TZ).isoformat(timespec="seconds")


def _audit_receipt(
    receipt: DeliveryReceipt,
    workspace_root: Path,
    payload: NotificationPayload,
) -> None:
    """Append a Daily audit line for the given receipt. Defensive — never
    raises (audit-log failure does not affect delivery success)."""
    try:
        # Inline import to avoid hard coupling at module-load.
        from lib.audit_log import append_daily_line  # type: ignore

        cron_name = (payload.metadata or {}).get("cron_name", "notify")
        status = "ok" if receipt.success else f"FAILED: {receipt.error}"
        line = (
            f"notify → {receipt.channel} → {receipt.recipient} "
            f"({status})"
        )
        append_daily_line(
            workspace_root=workspace_root,
            line=line,
            skill="notify",
            slug=cron_name,
            partial=not receipt.success,
        )
    except Exception as exc:  # noqa: BLE001
        # Audit log failure must never break the delivery flow.
        print(
            f"[notify] audit_log append failed (non-fatal): {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


# ─── JSON serialization helpers (for future CLI conversion at S7+) ─────────


def receipt_to_dict(receipt: DeliveryReceipt) -> dict:
    """Convert a DeliveryReceipt to a JSON-serializable dict."""
    return asdict(receipt)


def payload_to_dict(payload: NotificationPayload) -> dict:
    """Convert a NotificationPayload to a JSON-serializable dict.

    ``attachments`` (tuple of paths) is converted to a list of strings.
    """
    d = asdict(payload)
    d["attachments"] = [str(p) for p in (payload.attachments or ())]
    return d


__all__ = [
    "PARIS_TZ",
    "CHANNEL_EMAIL",
    "CHANNEL_TELEGRAM_ALERT",
    "SUPPORTED_CHANNELS",
    "PRIORITY_NORMAL",
    "PRIORITY_URGENT",
    "TELEGRAM_MESSAGE_MAX",
    "TELEGRAM_DOCUMENT_MAX_BYTES",
    "TELEGRAM_TRUNCATION_SUFFIX",
    "SMTP_CONNECTION_TIMEOUT_S",
    "MissingCredentialError",
    "NotificationPayload",
    "DeliveryReceipt",
    "NotificationBackend",
    "SMTPEmailBackend",
    "TelegramBackend",
    "render_markdown_to_html",
    "resolve_recipients",
    "deliver",
    "notify_cron_completion",
    "notify_alert",
    "receipt_to_dict",
    "payload_to_dict",
]
