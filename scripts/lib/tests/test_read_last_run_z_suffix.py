"""Regression tests for card #309 — read_last_run crashes on 'Z'-suffix timestamp.

ROOT CAUSE: content-dept L1 hand-writes .last-run with a trailing 'Z'
(e.g. ``2026-06-26T08:03:39Z``).  On Python 3.9 / 3.10,
``datetime.fromisoformat('...Z')`` raises ``ValueError``.  Only Python 3.11+
accepts the 'Z' suffix natively.

FIX: ``_parse_iso`` normalises 'Z' → '+00:00' before calling ``fromisoformat``,
so the parse succeeds on all supported Python versions.

Test design note
----------------
If this test suite is run on Python 3.11+ the bare ``datetime.fromisoformat``
would ALSO accept 'Z' — so simply calling ``read_last_run`` would not expose
the regression on newer interpreters.  To keep the test meaningful on all
Python versions we:

  1. Assert the *helper* ``_parse_iso`` handles 'Z' correctly (always exercises
     the normalisation branch).
  2. Assert that ``_parse_iso('...Z')`` raises ``ValueError`` when the
     normalisation is bypassed (i.e. when called as bare ``fromisoformat`` on
     3.9 — we simulate this by asserting the raw value raises on 3.9 and
     document that the helper is what protects production).
  3. Assert ``read_last_run`` succeeds end-to-end with a 'Z'-suffix file.
  4. Assert a normal '+00:00' offset timestamp parses identically.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib.dispatch_helpers import _parse_iso, read_last_run


# ── helpers ────────────────────────────────────────────────────────────────

_Z_TS = "2026-06-26T08:03:39Z"
_OFFSET_TS = "2026-06-26T08:03:39+00:00"

# The expected tz-aware datetime both strings should decode to.
_EXPECTED = datetime(2026, 6, 26, 8, 3, 39, tzinfo=timezone.utc)


# ── _parse_iso unit tests ───────────────────────────────────────────────────

def test_parse_iso_z_suffix_returns_correct_utc_datetime():
    """_parse_iso must accept a trailing 'Z' and return the right tz-aware dt."""
    result = _parse_iso(_Z_TS)
    assert result == _EXPECTED
    assert result.tzinfo is not None, "result must be timezone-aware"


def test_parse_iso_offset_form_unchanged():
    """A '+00:00' timestamp must parse identically — no behaviour change."""
    result = _parse_iso(_OFFSET_TS)
    assert result == _EXPECTED
    assert result.tzinfo is not None


def test_parse_iso_z_and_offset_equal():
    """Both representations of the same instant must compare equal."""
    assert _parse_iso(_Z_TS) == _parse_iso(_OFFSET_TS)


def test_parse_iso_preserves_non_utc_offset():
    """Non-UTC offsets must be preserved, not overwritten."""
    paris_ts = "2026-06-26T10:03:39+02:00"
    result = _parse_iso(paris_ts)
    # Same instant as _EXPECTED when converted to UTC.
    assert result.utctimetuple() == _EXPECTED.utctimetuple()


def test_parse_iso_invalid_raises_value_error():
    """Genuinely invalid strings must still raise ValueError."""
    with pytest.raises(ValueError):
        _parse_iso("not-a-timestamp")


# ── Python 3.9 regression proof ────────────────────────────────────────────

@pytest.mark.skipif(
    sys.version_info >= (3, 11),
    reason="Python 3.11+ accepts 'Z' natively — this assertion is only "
           "meaningful on 3.9/3.10 where the bug reproduced",
)
def test_bare_fromisoformat_rejects_z_on_pre_311():
    """Confirm the root cause: bare fromisoformat('...Z') raises on py<3.11."""
    with pytest.raises(ValueError):
        datetime.fromisoformat(_Z_TS)


# ── read_last_run end-to-end ────────────────────────────────────────────────

def test_read_last_run_z_suffix(tmp_path: Path):
    """read_last_run must NOT raise when the .last-run file contains a 'Z' suffix.

    This is the exact scenario from card #309: content L1 writes
    'YYYY-MM-DDTHH:MM:SSZ' and the dispatcher's read_last_run crashed on
    Python 3.9 — blocking ALL dispatch for the rest of the day.
    """
    layer_dir = tmp_path / "outputs" / "2026-06-26" / "1"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text(_Z_TS, encoding="utf-8")

    result = read_last_run(layer_dir)

    assert result is not None, "read_last_run must return a datetime, not None"
    assert result == _EXPECTED
    assert result.tzinfo is not None, "returned datetime must be tz-aware"


def test_read_last_run_offset_form_still_works(tmp_path: Path):
    """read_last_run must continue to work for the canonical '+00:00' form."""
    layer_dir = tmp_path / "outputs" / "2026-06-26" / "1"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text(_OFFSET_TS, encoding="utf-8")

    result = read_last_run(layer_dir)

    assert result == _EXPECTED
    assert result.tzinfo is not None


def test_read_last_run_missing_file_returns_none(tmp_path: Path):
    """Existing behaviour: absent .last-run returns None (not raised)."""
    layer_dir = tmp_path / "outputs" / "2026-06-26" / "1"
    layer_dir.mkdir(parents=True)

    assert read_last_run(layer_dir) is None


def test_read_last_run_empty_file_returns_none(tmp_path: Path):
    """Existing behaviour: empty .last-run returns None."""
    layer_dir = tmp_path / "outputs" / "2026-06-26" / "1"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text("", encoding="utf-8")

    assert read_last_run(layer_dir) is None
