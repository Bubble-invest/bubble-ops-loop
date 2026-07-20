"""Regression tests for card #713 — naive (no tz) ISO timestamp in `.last-run`
crashes `_to_paris` and blocks dispatch.

ROOT CAUSE: on 2026-07-19 ~22:33 Paris, the L4 risk_control subagent wrote
``2026-07-19T19:06:19.897511`` (NAIVE — no ``+00:00``/``Z`` suffix) into two
`.last-run` files. ``read_last_run`` → ``_parse_iso`` happily parses a naive
string via ``datetime.fromisoformat`` and returns a NAIVE datetime (no crash
there) — but that naive datetime is later fed into ``_to_paris``, which
raised ``ValueError: _to_paris requires a tz-aware datetime``. This blocked
`build_dispatch_ctx` for every evening tick (incl. the backup floor + the
market_wrapup window) until the UTC day rolled over and a fresh (empty)
`.last-run` implicitly cleared the poison.

FIX (defense in depth, both boundaries):
  1. ``_parse_iso`` — if the parsed result is naive, assume UTC
     (``dt.replace(tzinfo=timezone.utc)``) before returning. Normalises at
     the read boundary so a naive marker on disk never produces a naive
     datetime in memory.
  2. ``_to_paris`` — if given a naive datetime anyway (any other call site,
     defense in depth), assume UTC instead of raising. A single bad marker
     must never crash dispatch.

Both changes are a pure NO-OP for already-tz-aware inputs — only naive
inputs change behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.lib.dispatch_helpers import _parse_iso, _to_paris, read_last_run


_PARIS = ZoneInfo("Europe/Paris")

# The exact naive string the L4 risk_control subagent wrote on 2026-07-19.
_NAIVE_TS = "2026-07-19T19:06:19.897511"
_AWARE_TS = "2026-07-19T19:06:19.897511+00:00"

# Naive input is interpreted as UTC → 19:06:19.897511 UTC.
_EXPECTED_UTC = datetime(2026, 7, 19, 19, 6, 19, 897511, tzinfo=timezone.utc)
# 19:06 UTC in July is 21:06 Europe/Paris (CEST, UTC+2).
_EXPECTED_PARIS = _EXPECTED_UTC.astimezone(_PARIS)


# ── read_last_run + _parse_iso: naive marker on disk ────────────────────────

def test_read_last_run_naive_marker_returns_tz_aware(tmp_path: Path):
    """The exact #713 scenario: a naive `.last-run` body must not poison the
    in-memory datetime — read_last_run must return a tz-AWARE result."""
    layer_dir = tmp_path / "outputs" / "2026-07-19" / "4"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text(_NAIVE_TS, encoding="utf-8")

    result = read_last_run(layer_dir)

    assert result is not None
    assert result.tzinfo is not None, "result must be timezone-aware"
    assert result == _EXPECTED_UTC


def test_parse_iso_naive_input_assumes_utc():
    """_parse_iso must normalise a naive string to tz-aware UTC, not leak a
    naive datetime to callers."""
    result = _parse_iso(_NAIVE_TS)
    assert result.tzinfo is not None
    assert result == _EXPECTED_UTC


def test_read_last_run_naive_marker_survives_to_paris(tmp_path: Path):
    """Feeding the #713 marker through the full read_last_run -> _to_paris
    path must NOT raise, and must yield the correct Paris-local instant
    (naive interpreted as UTC -> 19:06 UTC == 21:06 Europe/Paris in July)."""
    layer_dir = tmp_path / "outputs" / "2026-07-19" / "4"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text(_NAIVE_TS, encoding="utf-8")

    dt_utc = read_last_run(layer_dir)
    paris = _to_paris(dt_utc)  # must not raise

    assert paris == _EXPECTED_PARIS
    assert paris.hour == 21
    assert paris.minute == 6


# ── _to_paris: bare naive datetime no longer raises ─────────────────────────

def test_to_paris_bare_naive_datetime_no_longer_raises():
    """A bare naive datetime (any call site, not just read_last_run) must be
    tolerated by _to_paris — assume UTC instead of raising ValueError."""
    naive_dt = datetime(2026, 7, 19, 19, 6, 19, 897511)
    assert naive_dt.tzinfo is None  # sanity check on the fixture itself

    result = _to_paris(naive_dt)  # must not raise

    assert result == _EXPECTED_PARIS


# ── NO-OP guard: tz-aware input is unaffected ───────────────────────────────

def test_parse_iso_aware_input_is_noop():
    """A tz-aware ISO string must round-trip unchanged — no behaviour change
    for already-correct markers."""
    result = _parse_iso(_AWARE_TS)
    assert result == _EXPECTED_UTC
    assert result.tzinfo is not None


def test_read_last_run_aware_marker_is_noop(tmp_path: Path):
    """A tz-aware `.last-run` body must round-trip unchanged through
    read_last_run."""
    layer_dir = tmp_path / "outputs" / "2026-07-19" / "4"
    layer_dir.mkdir(parents=True)
    (layer_dir / ".last-run").write_text(_AWARE_TS, encoding="utf-8")

    result = read_last_run(layer_dir)

    assert result == _EXPECTED_UTC
    assert result.tzinfo is not None


def test_to_paris_aware_input_is_noop():
    """A tz-aware datetime passed to _to_paris must convert exactly as
    before — no behaviour change for already-correct input."""
    result = _to_paris(_EXPECTED_UTC)
    assert result == _EXPECTED_PARIS
