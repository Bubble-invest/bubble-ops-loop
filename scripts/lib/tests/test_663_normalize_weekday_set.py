"""Tests for #663 — extract shared `_normalize_weekday_set()`.

Context: the day-normalisation logic (`day:` may be a single weekday string
or a list of weekday strings, normalised to a set of valid lowercase weekday
names) was duplicated verbatim in `is_mission_due`'s weekly branch and in
`next_pending_mission_time_today`'s weekly-slot check (~line 632). Reviewer of
PR #253 flagged this as a preventive dedup — the two sites had already nearly
drifted once. This is a behavior-preserving extraction into one shared
`_normalize_weekday_set(raw_day)` helper; both call sites now use it.

These tests cover the same input shapes the two call sites accept: a single
day string, a list of day strings, case variance (both sites only ever
`.lower()` — neither ever strips whitespace, so that is not asserted here),
unknown/typo names, and empty/absent values.
"""
from __future__ import annotations

from scripts.lib.dispatch_helpers import _normalize_weekday_set


def test_single_day_string():
    assert _normalize_weekday_set("monday") == {"monday"}


def test_single_day_string_mixed_case():
    assert _normalize_weekday_set("Monday") == {"monday"}
    assert _normalize_weekday_set("FRIDAY") == {"friday"}


def test_list_of_days():
    assert _normalize_weekday_set(["monday", "thursday"]) == {"monday", "thursday"}


def test_list_of_days_mixed_case():
    assert _normalize_weekday_set(["Monday", "THURSDAY"]) == {"monday", "thursday"}


def test_list_with_duplicate_days_dedupes():
    assert _normalize_weekday_set(["monday", "Monday", "MONDAY"]) == {"monday"}


def test_unknown_day_name_filtered_out():
    """Typos / non-weekday strings are dropped, not raised."""
    assert _normalize_weekday_set("someday") == set()
    assert _normalize_weekday_set(["monday", "someday"]) == {"monday"}


def test_list_with_non_string_entries_ignored():
    assert _normalize_weekday_set(["monday", 5, None, "friday"]) == {"monday", "friday"}


def test_empty_string_returns_empty_set():
    assert _normalize_weekday_set("") == set()


def test_none_returns_empty_set():
    assert _normalize_weekday_set(None) == set()


def test_empty_list_returns_empty_set():
    assert _normalize_weekday_set([]) == set()


def test_list_of_all_unknown_returns_empty_set():
    assert _normalize_weekday_set(["someday", "otherday"]) == set()


def test_all_seven_weekday_names_recognized():
    for name in (
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ):
        assert _normalize_weekday_set(name) == {name}
