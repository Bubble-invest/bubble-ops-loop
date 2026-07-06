"""
test_queue_item_summaries.py — card #460: human-readable "few-words"
summaries for pending queue items / mgmt notes, instead of "<kind>: <kind>".

Root cause under test: `_derive_queue_item_title` (github_reader.py, L1/L3/
gates) and `_note_title` (mgmt_note_state.py, L2 management notes) only knew
how to read free-text fields (title/subject/summary/body/…). A payload made
entirely of numeric KPIs — e.g. a `morning_brief` note carrying
`dept_health_score`/`children_in_warning` — had no such field, so both
functions fell through to the bare kind/mission_id label, rendering
"morning_brief: morning_brief" (Joris screenshot 2026-07-02).

Covers:
  - humanize.humanize_queue_item(): the new payload-aware summarizer,
    directly, for each known kind (morning_brief, dept_kpi_analysis,
    directive) plus its "I don't know this shape" None return.
  - _derive_queue_item_title() wiring: known kinds render the curated
    summary; unknown kinds are unaffected (existing #391 behaviour); the
    new generic fallback (label + created_at date + first payload scalar)
    replaces the old bare-label fallback.
  - _note_title() wiring: same known-kind summary reaches L2 mgmt notes,
    where morning_brief notes actually live in production.
"""
from __future__ import annotations


# ─── humanize_queue_item() — direct unit tests ───────────────────────────────


def test_morning_brief_with_score_and_warnings():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item(
        {"dept_health_score": 86.7, "children_in_warning": ["content"]},
        "morning_brief",
    )
    assert out == "Brief du matin — santé dept 86.7/100, content en warning"


def test_morning_brief_multiple_warnings_pluralizes():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item(
        {"dept_health_score": 91, "children_in_warning": ["content", "maya"]},
        "morning_brief",
    )
    assert out == "Brief du matin — santé dept 91/100, content, maya en warnings"


def test_morning_brief_score_only_no_warnings():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item({"dept_health_score": 92}, "morning_brief")
    assert out == "Brief du matin — santé dept 92/100"


def test_morning_brief_empty_payload_still_labeled():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item({}, "morning_brief")
    assert out == "Brief du matin"


def test_dept_kpi_analysis_known_kind():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item({"dept_health_score": 77}, "dept_kpi_analysis")
    assert out == "Analyse KPI — santé dept 77/100"


def test_directive_known_kind_uses_directive_text():
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item(
        {"directive_text": "Pause LinkedIn outreach until Monday"}, "directive"
    )
    assert out == "Directive — Pause LinkedIn outreach until Monday"


def test_unknown_kind_returns_none():
    from console.services.humanize import humanize_queue_item

    assert humanize_queue_item({"foo": "bar"}, "ideas_scout") is None


def test_matches_on_resolved_mgmt_note_label():
    """mgmt_note_state._note_title resolves its own `label` (mission_id, else
    kind — see _note_label) BEFORE calling humanize_queue_item, so passing
    that resolved label as `key` must match the same way a queue item's
    `kind` does."""
    from console.services.humanize import humanize_queue_item

    out = humanize_queue_item(
        {"dept_health_score": 86.7, "children_in_warning": ["content"]},
        "morning_brief",  # the resolved label, not the generic "management_note" kind
    )
    assert out == "Brief du matin — santé dept 86.7/100, content en warning"


# ─── _derive_queue_item_title() — wiring into the L1/L3/gates queue path ─────


def test_derive_queue_item_title_uses_known_summary_for_morning_brief():
    from console.services.github_reader import _derive_queue_item_title

    doc = {
        "id": "mb-1", "kind": "morning_brief",
        "dept_health_score": 86.7, "children_in_warning": ["content"],
        "created_at": "2026-07-02T07:00:00Z",
    }
    title = _derive_queue_item_title(doc, "morning_brief")
    assert title == "Brief du matin — santé dept 86.7/100, content en warning"
    assert title != "morning_brief: morning_brief"


def test_derive_queue_item_title_unknown_kind_unaffected():
    """#391 behaviour (subject-field disambiguation) must not regress for
    kinds humanize_queue_item doesn't know about."""
    from console.services.github_reader import _derive_queue_item_title

    doc = {"id": "is-1", "kind": "ideas_scout", "ticker_or_theme": "LSEG"}
    assert _derive_queue_item_title(doc, "ideas_scout") == "ideas_scout: LSEG"


def test_derive_queue_item_title_generic_fallback_includes_date_and_scalar():
    """Card #460 spec: generic fallback = kind + created_at + first payload
    scalar — never the bare label alone (indistinguishable from the old
    "<kind>: <kind>" bug)."""
    from console.services.github_reader import _derive_queue_item_title

    doc = {
        "id": "x-1", "kind": "totally_unknown_kind",
        "created_at": "2026-07-01T12:00:00Z",
        "confidence": 0.42,
    }
    title = _derive_queue_item_title(doc, "totally_unknown_kind")
    assert title == "totally_unknown_kind · 2026-07-01 · confidence=0.42"


def test_derive_queue_item_title_generic_fallback_no_scalar_no_date():
    """With nothing usable at all, falls back to just the label (never
    crashes, never renders an empty/garbled string)."""
    from console.services.github_reader import _derive_queue_item_title

    title = _derive_queue_item_title({"id": "x-2", "kind": "mystery"}, "mystery")
    assert title == "mystery"


def test_derive_queue_item_title_generic_fallback_caps_large_payload():
    """Card #503: the step-5 generic fallback (added by #213/#460) built
    "<label> · <date> · <first scalar>" with NO length cap — unlike steps
    2-4, which all route through `_fmt(max_len=60)`. A large numeric/bool
    scalar (the only shapes step 5 reaches that step 4 doesn't, since step 4
    only matches strings) rendered a 200+ char title in the cockpit
    pending-item row, defeating #460's "few words" intent.

    Uses a huge int, not a huge string, because a huge *string* field would
    already be caught (and truncated) by step 4 — this test must exercise
    step 5 specifically."""
    from console.services.github_reader import _derive_queue_item_title

    doc = {
        "id": "x-3", "kind": "totally_unknown_kind",
        "created_at": "2026-07-01T12:00:00Z",
        "huge_number": int("9" * 250),
    }
    title = _derive_queue_item_title(doc, "totally_unknown_kind")
    assert len(title) <= 61  # max_len=60 + 1-char ellipsis, same cap as _fmt
    assert title.startswith("totally_unknown_kind · 2026-07-01 · huge_number=")
    assert title.endswith("…")


# ─── _cap() — the shared tail-truncation helper (#540, follow-up to #503) ────
# Factored out of `_fmt`'s truncation tail and step 5's duplicate copy of the
# same expression, so the one truncation contract (`s[:max_len] + ellipsis`)
# lives in a single place. Pure refactor — these are direct unit tests of
# the helper itself; the behavioural coverage above already pins the
# unchanged rendered output.


def test_cap_empty_string():
    from console.services.github_reader import _cap

    assert _cap("", 60) == ""


def test_cap_exactly_max_len_unchanged():
    from console.services.github_reader import _cap

    s = "x" * 60
    assert _cap(s, 60) == s


def test_cap_over_max_len_truncates_with_ellipsis():
    from console.services.github_reader import _cap

    s = "x" * 61
    out = _cap(s, 60)
    assert out == ("x" * 60) + "…"
    assert len(out) == 61


def test_cap_under_max_len_unchanged():
    from console.services.github_reader import _cap

    assert _cap("short", 60) == "short"


# ─── _note_title() — wiring into the L2 mgmt-note path (where morning_brief
#     notes actually live in production; #459's own fixtures use mission_id=
#     "morning_brief") ──────────────────────────────────────────────────────


def test_note_title_uses_known_summary_for_morning_brief():
    from console.services.mgmt_note_state import _note_title

    data = {
        "mission_id": "morning_brief", "kind": "management_note",
        "dept_health_score": 86.7, "children_in_warning": ["content"],
    }
    title = _note_title(data, "morning_brief")
    assert title == "Brief du matin — santé dept 86.7/100, content en warning"
    assert title != "morning_brief: morning_brief"
    assert title != "morning_brief"


def test_note_title_unknown_mission_falls_back_to_free_text_field():
    from console.services.mgmt_note_state import _note_title

    data = {"mission_id": "escalation", "title": "Ben missed a KPI band"}
    assert _note_title(data, "escalation") == "escalation: Ben missed a KPI band"


def test_note_title_unknown_mission_no_text_field_falls_back_to_label():
    from console.services.mgmt_note_state import _note_title

    assert _note_title({"mission_id": "escalation"}, "escalation") == "escalation"
