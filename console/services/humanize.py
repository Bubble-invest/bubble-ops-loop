"""
humanize.py — translate technical enum slugs to operator French prose.

Used by the home page to render grouped décision-cards in human voice,
per Item E1 (Bureau-de-Cadre polish, {{OPERATOR}} flag msg 2709).

Mapping is intentionally tight (no fuzzy guessing). Unknown kinds fall
back to the snake_case → "snake case" stripped form (still pure prose,
no enum slug).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

_HUMAN_KIND: dict[str, str] = {
    "decision":           "décision",
    "exec_retry":         "reprise d'exécution",
    "mandate_breach":     "écart de mandat",
    "modify":             "modification",
    "prospect_dm":        "DM à approuver",
    "prospect_followup":  "relance prospect",
    "trade_order":        "ordre à valider",
    "content_publish":    "publication à valider",
    "research_decision":  "décision de recherche",
    "echo_action":        "action d'écho",
    "social_post":        "publication sociale",
    # These three are already used elsewhere in gate_card.html/gate_batch.html
    # as confirmed content-dept kinds (part of the `content_kinds` tuple) but
    # were missing from this map, so they fell back to the raw snake_case
    # form. Everything else content-related (investment_case, essay,
    # newsletter, note, ...) has NO confirmed kind: enum anywhere in the
    # codebase (#433 scout check) — left unmapped on purpose so they use the
    # existing graceful fallback (snake_case → space-separated prose, e.g.
    # "essay" → "essay", "investment_case" → "investment case") rather than
    # guessing labels for slugs that may not exist.
    "news_post":          "post",
    "news_post_task":     "post",
    "followup_draft":     "relance",
    # #730 — Miranda's dynamic question cards: mid-draft, she asks the
    # operator to pick a direction among 2-3 options (kind: question).
    "question":           "question à trancher",
}


_HUMAN_RISK: dict[str, str] = {
    "low":      "faible",
    "medium":   "modéré",
    "moderate": "modéré",
    "high":     "élevé",
    "critical": "critique",
}


def humanize_risk(risk: str | None) -> str:
    """Return the French operator-prose label for a gate `risk_level`."""
    if not risk:
        return "inconnu"
    return _HUMAN_RISK.get(str(risk).strip().lower(), str(risk))


def capitalize_fr(text: str | None) -> str:
    """Capitalize the first character without lowering the rest.

    Unlike Jinja's `|capitalize`, this preserves acronyms like 'DM'.
    Returns '' for None/empty input.
    """
    if not text:
        return ""
    return text[:1].upper() + text[1:]


# Map each of the 5 autonomy modes to a French operator-prose sentence.
# Used both as the "Niveau actuel" line and as ingredients for the
# "Pourrait apprendre" composition. Keep these short — they render
# inside a 3-line .gate-autonomie block on mobile.
_HUMAN_MODE: dict[str, str] = {
    "manual_required":             "Tu valides chaque fois.",
    "manual_unless_policy_passed": "Tu valides sauf si la règle est OK.",
    "auto_if_policy_passed":       "Elle gère seule si la règle est OK.",
    "auto_with_veto_window":       "Elle agit ; tu peux revenir dessus.",
    "disabled":                    "Désactivé pour l'instant.",
}


def humanize_mode(mode: str | None) -> str:
    """Return the French operator-prose label for a single autonomy mode.

    Examples:
        >>> humanize_mode("manual_required")
        'Tu valides chaque fois.'
        >>> humanize_mode("auto_if_policy_passed")
        'Elle gère seule si la règle est OK.'
        >>> humanize_mode(None)
        ''
        >>> humanize_mode("unknown_mode")
        'unknown_mode'
    """
    if not mode:
        return ""
    return _HUMAN_MODE.get(str(mode).strip(), str(mode))


def humanize_future_modes(modes: list[str] | None) -> str:
    """Return a French sentence describing the future-eligible-modes set.

    Notion v5 lines 920-924 require a "Future autonomy" line on every gate
    surface. We render this in operator-prose, never raw enum slugs.

    Empty / None → reassuring "no upgrade path planned" copy (matches the
    existing PR-body convention reused across the console).

    A non-empty list → join the humanized mode sentences with " ou " so
    {{OPERATOR}} sees the full trajectory. We strip the trailing period from
    each sentence to keep the joined string readable.

    Examples:
        >>> humanize_future_modes([])
        "Pas de palier d'autonomie supérieur prévu pour l'instant."
        >>> humanize_future_modes(None)
        "Pas de palier d'autonomie supérieur prévu pour l'instant."
        >>> humanize_future_modes(["auto_if_policy_passed"])
        'Elle gère seule si la règle est OK.'
        >>> humanize_future_modes(["auto_if_policy_passed", "auto_with_veto_window"])
        'Elle gère seule si la règle est OK, ou elle agit ; tu peux revenir dessus.'
    """
    if not modes:
        return "Pas de palier d'autonomie supérieur prévu pour l'instant."

    parts = [humanize_mode(m).rstrip(".") for m in modes if m]
    parts = [p for p in parts if p]
    if not parts:
        return "Pas de palier d'autonomie supérieur prévu pour l'instant."
    if len(parts) == 1:
        return parts[0] + "."
    # Compose: first part capitalized, subsequent lowercased for natural flow.
    joined = parts[0] + ", ou " + ", ou ".join(p[:1].lower() + p[1:] for p in parts[1:])
    return joined + "."


def shadow_autonomy_label(gate: dict | None) -> str:
    """Return the French operator-prose label for the shadow-autonomy state.

    Gate-level shadow autonomy is NOT carried on the gate item in v3 — it
    is computed at the dept-level by Layer 4 under
    `management-export.autonomy_readiness.action_classes[*]`. For v1 of
    this UX we render a static placeholder so {{OPERATOR}} sees the 3rd line
    Notion mandates (lines 920-924). Future work wires the real state.

    QA-Letter Finding A doctrine note (Notion v5 lines 421-436): the
    'shadow autonomy' phase is an OBSERVATION PHASE, NOT a 5th autonomy
    mode. Do not map it to any of the 5 official modes — the safety
    semantics are different (agent observes + proposes, never acts).
    """
    # `gate` arg reserved for future Layer-4-aware rendering.
    _ = gate
    return "Pas encore activée pour cette équipe."


# ---------------------------------------------------------------------------
# Sprint Maya-blocker Fix 2 (2026-05-21): humanize_substep
#
# Surface `STATE.yaml::step_progress.<step>.current_substep` in the operator-
# facing onboarding view so {{OPERATOR}} sees WHAT the agent is currently asking
# about, not just "Step 5 in progress". Bureau-de-Cadre French prose, no enum
# slugs.
#
# Notion v5 references:
#   - lines 894-924 (Step 5 substeps: policy_card, kpi_naming, band_naming)
#   - lines 762-781 (3-pane onboarding layout)
# ---------------------------------------------------------------------------


def humanize_substep(substep: dict | None) -> str:
    """Render a STATE.yaml current_substep dict to French operator prose.

    Args:
        substep: dict like
            {"type": "kpi_naming", "draft_payload": {"class_id": "social_post"}}
            or None / {} when no substep is in flight.

    Returns:
        Empty string if no substep, otherwise a sentence like
        "En ce moment : elle te demande comment nommer le jeu de garde-fous
        KPI pour `social_post`."

    Examples:
        >>> humanize_substep(None)
        ''
        >>> humanize_substep({})
        ''
        >>> humanize_substep({"type": "kpi_naming",
        ...                    "draft_payload": {"class_id": "social_post"}})
        'En ce moment : elle te demande comment nommer le jeu de garde-fous KPI pour `social_post`.'
        >>> humanize_substep({"type": "band_naming",
        ...                    "draft_payload": {"class_id": "social_post"}})
        "En ce moment : elle te demande comment nommer la bande d'autorisation pour `social_post`."
        >>> humanize_substep({"type": "policy_card",
        ...                    "draft_payload": {"class_id": "social_post"}})
        'En ce moment : elle te propose la gate policy pour `social_post`.'
        >>> humanize_substep({"type": "proposing_mission",
        ...                    "draft_payload": {"mission_id": "signal_scan_task"}})
        'En ce moment : elle te propose la mission `signal_scan_task`.'
        >>> humanize_substep({"type": "weird_unknown_substep",
        ...                    "draft_payload": {}})
        'En ce moment : étape en cours.'
    """
    if not substep or not isinstance(substep, dict):
        return ""
    stype = substep.get("type")
    if not stype:
        return ""
    payload = substep.get("draft_payload") or {}
    class_id = payload.get("class_id")
    mission_id = payload.get("mission_id")

    # Step 5 substeps (gates_kpis runner).
    if stype == "kpi_naming" and class_id:
        return (
            f"En ce moment : elle te demande comment nommer le jeu de "
            f"garde-fous KPI pour `{class_id}`."
        )
    if stype == "band_naming" and class_id:
        return (
            f"En ce moment : elle te demande comment nommer la bande "
            f"d'autorisation pour `{class_id}`."
        )
    if stype == "policy_card" and class_id:
        return (
            f"En ce moment : elle te propose la gate policy pour `{class_id}`."
        )
    # Step 2 substeps (missions runner).
    if stype == "proposing_mission" and mission_id:
        return (
            f"En ce moment : elle te propose la mission `{mission_id}`."
        )
    # Step 7 substep (activation runner).
    if stype == "activation_pr":
        return (
            "En ce moment : elle te présente la lettre d'arrivée pour validation."
        )
    # Generic fallback — no enum leak.
    return "En ce moment : étape en cours."


def humanize_kind(kind: str | None) -> str:
    """Return the French operator-prose label for a gate `kind`.

    Examples:
        >>> humanize_kind("prospect_dm")
        'DM à approuver'
        >>> humanize_kind("research_decision")
        'décision de recherche'
        >>> humanize_kind("domain:trade_order")
        'ordre à valider'
        >>> humanize_kind("custom_thing_not_mapped")
        'custom thing not mapped'
        >>> humanize_kind(None)
        'décision'
    """
    if not kind:
        return "décision"

    raw = str(kind).strip()
    # Pattern `domain:<suffix>` → use the suffix after the colon.
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip() or raw

    if raw in _HUMAN_KIND:
        return _HUMAN_KIND[raw]

    # Fallback: snake_case → space-separated lower prose. No enum slugs.
    return raw.replace("_", " ").strip().lower()


# Kinds that default to the linkedin channel when a gate doesn't set
# `channel` explicitly — mirrors the inference every content-proposal
# template already applied ad hoc (gate_card.html, gate_batch.html,
# gate-identity-chips.html). Centralized here (card #666 wave 2) so the
# batch-view channel filter and the identity chip agree on one definition.
_LINKEDIN_DEFAULT_KINDS = ("news_post", "news_post_task", "prospect_dm", "followup_draft")

# Channels a gate can be filtered by in the batch triage view, in display
# order. "other" is the catch-all bucket for anything not in this list
# (or with no channel at all) — see gate_channel().
GATE_CHANNELS = ("linkedin", "substack", "x", "newsletter", "other")

# Display label per channel for the Option A chip row (card #666 follow-up,
# Jade-validated mockup). "X" is already the correct casing on both axes so
# it needs no entry — the fallback (.title()/upper handled by the caller)
# covers it and "other" identically.
CHANNEL_LABEL: dict[str, str] = {
    "linkedin": "LinkedIn",
    "substack": "Substack",
    "x": "X",
    "newsletter": "Newsletter",
    "other": "Autre",
}


def humanize_channel(channel: str | None) -> str:
    """Return the display label for a GATE_CHANNELS value.

    >>> humanize_channel("linkedin")
    'LinkedIn'
    >>> humanize_channel("x")
    'X'
    >>> humanize_channel(None)
    'Autre'
    """
    return CHANNEL_LABEL.get((channel or "").strip().lower(), "Autre")


def gate_channel(gate: dict | None) -> str:
    """Return the channel a gate belongs to, for display and filtering.

    Resolution order:
      1. `gate.channel`, if set (lowercased).
      2. "linkedin" for the content kinds that historically implied it
         without setting the field (Maya's LinkedIn post / prospect-DM /
         follow-up proposals).
      3. "other" — the gate has no channel and none can be inferred.

    Always returns a non-empty string from GATE_CHANNELS, so callers never
    need a None-guard (unlike the raw `gate.channel` field).
    """
    if not gate:
        return "other"
    raw = gate.get("channel")
    if raw:
        ch = str(raw).strip().lower()
        if ch in GATE_CHANNELS:
            return ch
        # Prefix-normalize dept variants (real data: substack_post,
        # substack_note, x_thread, ...) onto their canonical channel so the
        # filter buckets them correctly instead of losing them.
        for known in GATE_CHANNELS:
            if known != "other" and ch.startswith(known):
                return known
        return "other"
    if gate.get("kind") in _LINKEDIN_DEFAULT_KINDS:
        return "linkedin"
    return "other"


# ---------------------------------------------------------------------------
# Cadence humanizer ({{OPERATOR}} flag 2026-05-24 msg 3137)
# ---------------------------------------------------------------------------

# French day names (lowercase). Maps both English and French keys so
# missions/*.yaml authors can write either.
_FR_DAYS: dict[str, str] = {
    "monday":    "lundi",
    "tuesday":   "mardi",
    "wednesday": "mercredi",
    "thursday":  "jeudi",
    "friday":    "vendredi",
    "saturday":  "samedi",
    "sunday":    "dimanche",
    "lundi":     "lundi",
    "mardi":     "mardi",
    "mercredi":  "mercredi",
    "jeudi":     "jeudi",
    "vendredi":  "vendredi",
    "samedi":    "samedi",
    "dimanche":  "dimanche",
}


def _fmt_time(t: str | None) -> str | None:
    """Convert '07:00' -> '07h00'. Returns None if input is falsy/invalid."""
    if not t:
        return None
    s = str(t).strip()
    if ":" in s:
        return s.replace(":", "h")
    # Bare hour like '7' or '17' -> '7h00' / '17h00'
    if s.isdigit():
        return f"{int(s):02d}h00"
    return s


def _fmt_range(rng: str | None) -> tuple[str, str] | None:
    """Convert '08:00-21:00' -> ('08h00', '21h00')."""
    if not rng or "-" not in str(rng):
        return None
    parts = [p.strip() for p in str(rng).split("-", 1)]
    a = _fmt_time(parts[0])
    b = _fmt_time(parts[1])
    if not a or not b:
        return None
    return a, b


def humanize_cadence(mission: dict | None) -> str:
    """Render a mission's cadence in French operator prose.

    Reads the relevant fields out of the mission dict (cadence, time, day,
    active_hours). Returns a single-sentence-fragment suitable for the
    `.mission-cadence` line in the UI (no trailing period).

    Examples ({{OPERATOR}} flag 2026-05-24 msg 3137):
        >>> humanize_cadence({"cadence": "daily", "time": "07:00"})
        'Tous les jours à 07h00'
        >>> humanize_cadence({"cadence": "weekly", "day": "friday",
        ...                    "time": "17:00"})
        'Le vendredi à 17h00'
        >>> humanize_cadence({"cadence": "hourly",
        ...                    "active_hours": "08:00-21:00"})
        'Toutes les heures entre 08h00 et 21h00'
        >>> humanize_cadence({"cadence": "every_2h"})
        'Toutes les 2 heures'
        >>> humanize_cadence({"cadence": "every_4h",
        ...                    "active_hours": "09:00-18:00"})
        'Toutes les 4 heures entre 09h00 et 18h00'
        >>> humanize_cadence({})
        'Cadence non spécifiée'
        >>> humanize_cadence(None)
        'Cadence non spécifiée'

    Unknown cadences fall back to the snake_case → prose form, with the
    active_hours range appended when present. No raw enum is ever leaked.
    """
    if not mission or not isinstance(mission, dict):
        return "Cadence non spécifiée"
    cadence = str(mission.get("cadence", "")).strip().lower()
    if not cadence:
        return "Cadence non spécifiée"
    time = _fmt_time(mission.get("time"))
    rng = _fmt_range(mission.get("active_hours"))

    # daily — "Au plus tôt à partir de HHhMM, dès que son tour vient"
    #
    # {{OPERATOR}} flag msg 3142 (2026-05-24): the old phrasing "Tous les jours
    # à 07h00" implied a guaranteed cron, which lies about the actual
    # dispatch model. /loop ticks every 20 min; Layer 1 fires only when
    # other layers are idle ≥1 round; THEN it materializes due missions
    # into queues. So the displayed `time:` is the EARLIEST POSSIBLE
    # materialization, not a target.
    if cadence == "daily":
        if time:
            return f"Au plus tôt à partir de {time}, dès que son tour vient"
        return "Une fois par jour, dès que son tour vient"

    # weekly — "Au plus tôt le <jour> à partir de HHhMM..."
    if cadence == "weekly":
        raw_day = str(mission.get("day", "")).strip().lower()
        day_fr = _FR_DAYS.get(raw_day)
        if day_fr:
            if time:
                return (f"Au plus tôt le {day_fr} à partir de {time}, "
                        f"dès que son tour vient")
            return f"Une fois par semaine, le {day_fr}, dès que son tour vient"
        # No day specified
        if time:
            return (f"Une fois par semaine, au plus tôt à partir de {time}, "
                    f"dès que son tour vient")
        return "Une fois par semaine, dès que son tour vient"

    # hourly — "Une fois par heure, dès que son tour vient [entre HHhMM et HHhMM]"
    if cadence == "hourly":
        if rng:
            return (f"Une fois par heure entre {rng[0]} et {rng[1]}, "
                    f"dès que son tour vient")
        return "Une fois par heure, dès que son tour vient"

    # every_<N>h — "Toutes les <N> heures [entre HHhMM et HHhMM]"
    if cadence.startswith("every_") and cadence.endswith("h"):
        middle = cadence[len("every_"):-1]
        if middle.isdigit():
            n = int(middle)
            unit = "heure" if n == 1 else "heures"
            base = f"Toutes les {n} {unit}"
            if rng:
                return f"{base} entre {rng[0]} et {rng[1]}"
            return base

    # every_<N>m — minutes
    if cadence.startswith("every_") and cadence.endswith("m"):
        middle = cadence[len("every_"):-1]
        if middle.isdigit():
            n = int(middle)
            unit = "minute" if n == 1 else "minutes"
            base = f"Toutes les {n} {unit}"
            if rng:
                return f"{base} entre {rng[0]} et {rng[1]}"
            return base

    # Fallback: snake_case → space-separated lower, capitalise first letter
    prose = cadence.replace("_", " ").strip()
    if prose:
        prose = prose[0].upper() + prose[1:]
    if rng:
        return f"{prose} entre {rng[0]} et {rng[1]}"
    if time:
        return f"{prose} à {time}"
    return prose or "Cadence non spécifiée"


# ---------------------------------------------------------------------------
# Queue-item ("À traiter") summaries — board card #460, Joris spec 2026-07-02
#
# The left-column pending pile (console/services/github_reader.py's
# `_derive_queue_item_title` for L1/L3/gates, mgmt_note_state.py's
# `_note_title` for L2 management notes) used to render "<kind>: <kind>"
# whenever a queue item carried no free-text field (title/summary/body/…) —
# e.g. `morning_brief` notes, whose payload is entirely numeric KPIs
# (`dept_health_score`, `children_in_warning`), not prose. That is a mission
# id repeated twice, not a summary a human can read at a glance.
#
# `humanize_queue_item()` is the single place both callers reach for a
# payload-aware one-liner BEFORE falling through to their own generic
# text-field scan. It only special-cases kinds/mission_ids whose payload
# shape is actually known (tight, no fuzzy guessing — same philosophy as
# `humanize_kind` above); anything else returns None so the caller's
# existing fallback chain (free-text field → subject field → first scalar →
# bare label) still applies unchanged.
# ---------------------------------------------------------------------------


def _fmt_num(v: Any) -> str:
    """Render a score number without a noisy trailing .0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


def humanize_queue_item(doc: dict | None, key: str | None) -> str | None:
    """Return a human, few-words summary for a queue-item/mgmt-note payload,
    or None if this payload shape isn't one we know how to summarize.

    `key` is whatever the caller already uses as its display label: a queue
    item's `kind` (github_reader._derive_queue_item_title), or a mgmt note's
    resolved `mission_id`-else-`kind` label (mgmt_note_state._note_label) —
    so a `morning_brief` note is recognized the same way whether it arrives
    as a queue item (kind="morning_brief") or a management note
    (mission_id="morning_brief", generic kind="management_note").

    Examples:
        >>> humanize_queue_item(
        ...     {"dept_health_score": 86.7, "children_in_warning": ["content"]},
        ...     "morning_brief")
        'Brief du matin — santé dept 86.7/100, content en warning'
        >>> humanize_queue_item({"dept_health_score": 92}, "morning_brief")
        'Brief du matin — santé dept 92/100'
        >>> humanize_queue_item({"foo": "bar"}, "unknown_kind") is None
        True
    """
    if not isinstance(doc, dict):
        return None
    key = (key or "").strip()

    if key == "morning_brief":
        score = doc.get("dept_health_score")
        warning = doc.get("children_in_warning")
        # Normalize to a list of names, tolerating a bare string or a count.
        if isinstance(warning, str):
            warning_names = [warning] if warning.strip() else []
        elif isinstance(warning, (list, tuple)):
            warning_names = [str(w).strip() for w in warning if str(w).strip()]
        else:
            warning_names = []

        parts: list[str] = []
        if score is not None:
            parts.append(f"santé dept {_fmt_num(score)}/100")
        if warning_names:
            joined = ", ".join(warning_names)
            plural = "s" if len(warning_names) > 1 else ""
            parts.append(f"{joined} en warning{plural}")
        if parts:
            return "Brief du matin — " + ", ".join(parts)
        return "Brief du matin"

    if key == "dept_kpi_analysis":
        score = doc.get("dept_health_score") or doc.get("health_score")
        if score is not None:
            return f"Analyse KPI — santé dept {_fmt_num(score)}/100"
        return "Analyse KPI du département"

    if key == "directive":
        text = doc.get("directive_text") or doc.get("instruction")
        if isinstance(text, str) and text.strip():
            excerpt = text.strip().replace("\n", " ")
            return "Directive — " + (excerpt[:57] + "…" if len(excerpt) > 57 else excerpt)
        return "Nouvelle directive"


# ---------------------------------------------------------------------------
# Gate anteriority — compact "how old is this pending decision" display
# (board #666, Jade's triage need: see at a glance which gates have waited
# longest). Pairs with github_reader._attach_gate_date, which stamps every
# gate dict with `_gate_date` (a `date` or None) and `_gate_age_days`.
# ---------------------------------------------------------------------------

# Age (days) at which a pending gate is flagged as stale in the UI.
GATE_AGE_WARNING_DAYS = 7


def format_gate_age(gate: dict | None) -> str | None:
    """Return a compact "12/07 · il y a 4 j" string for a gate's `_gate_date`,
    or None when no date could be determined (graceful no-op — caller must
    not render an age chip in that case).

    >>> format_gate_age({"_gate_date": date(2026, 7, 12), "_gate_age_days": 4})
    '12/07 · il y a 4 j'
    """
    if not gate:
        return None
    d = gate.get("_gate_date")
    if not isinstance(d, date):
        return None
    age = gate.get("_gate_age_days")
    if not isinstance(age, int):
        age = 0
    if age <= 0:
        when = "aujourd'hui"
    elif age == 1:
        when = "il y a 1 j"
    else:
        when = f"il y a {age} j"
    return f"{d:%d/%m} · {when}"


def gate_age_is_stale(gate: dict | None) -> bool:
    """True when a pending gate's age crosses GATE_AGE_WARNING_DAYS — used to
    apply the amber "getting old" highlight in list/batch views."""
    if not gate:
        return False
    age = gate.get("_gate_age_days")
    return isinstance(age, int) and age > GATE_AGE_WARNING_DAYS

    return None


# ---------------------------------------------------------------------------
# Gate human title — Option A (Jade-validated mockup, card #666 follow-up,
# 2026-07-16). No gate carries its own `title:` field; the batch/decision
# cards used to lead with the dept slug ("content") or the raw gate id
# (publish-linkedin-org-100-agents-2026-07-02), neither of which is
# something {{OPERATOR}} can read at a glance. gate_human_title() derives a
# real headline server-side, in this priority order:
#
#   1. A NAMED title quoted in `summary`, but ONLY when a title-bearing noun
#      (essay/essai/article/note/post) appears earlier in the same clause —
#      an essay literally titled "Deux cartes pour la même décennie" is a
#      title; a hook or reported line quoted mid-thesis ("l'agent n'a rien
#      fait" = FAUX, or a « Impact / résultat » archetype label) is NOT.
#      Verified against all 12 real pending content gates (2026-07-16):
#      this tier fires exactly once (the Substack essay) — everything else
#      correctly falls through, because a naive "first quote in the first
#      two lines" rule (the mockup's original wording) mis-fired on 2/12
#      real gates that quote a claim being refuted or an archetype label,
#      not a title.
#   2. `image_hook` — LinkedIn content gates already write a short,
#      standalone, publish-ready phrase here for the card visual; it reads
#      as a headline even though it wasn't authored as one.
#   3. De-slugified `id` — strip the `publish-<channel>-` prefix and the
#      trailing `-YYYY-MM-DD`, hyphens -> spaces, sentence-case. Covers X
#      threads, Substack notes, and anything with neither a named title nor
#      an image_hook.
#   4. Newsletter special case — a newsletter gate's id/summary never carry
#      a topical title (batch id + date only), so it gets a dedicated
#      "Newsletter — <created>" fallback ahead of the generic de-slug tier.
#
# The dept slug never appears in the returned title — callers move it out
# of the heading entirely (Option A mockup: gate.slug/kind used only for
# the chip row, never the H-tag).
# ---------------------------------------------------------------------------

# A quoted span counts as a NAMED TITLE only when introduced by one of these
# nouns earlier in the same clause (one parenthetical aside tolerated in
# between, e.g. 'essay (thought-leadership, FR) "Title"').
_TITLE_INTRO_RE = re.compile(
    r"(essay|essai|article|note|post)\b(\s*\([^)]{0,40}\))?[^.\"\n]{0,15}$",
    re.IGNORECASE,
)
# A quote preceded by one of these within the lookback window is illustrative
# (an archetype label, reported speech, a claim being refuted) — never a
# title, even if a title-intro noun appears further back in the same window.
_NOT_TITLE_BEFORE_RE = re.compile(
    r"(arch[ée]type|hook\b|concluait|disait|r[ée]pondait)[^.\"\n]{0,30}$",
    re.IGNORECASE,
)
# French guillemets or straight double quotes, 4-90 chars inside.
_QUOTE_RE = re.compile(r"«\s*([^»]{4,90})\s*»|\"\s*([^\"]{4,90})\s*\"")
# Trailing `-YYYY-MM-DD` on a gate id.
_ID_TRAILING_DATE_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
# `publish-<channel>-<rest>` prefix on a gate id.
_ID_PUBLISH_PREFIX_RE = re.compile(r"^publish-[a-z0-9]+-(.*)$")


def _quoted_named_title(summary: str) -> str | None:
    """Return the first quoted span in `summary` that reads as a NAMED
    title (not a hook/label/reported-speech quote), or None."""
    window = summary[:260]
    for m in _QUOTE_RE.finditer(window):
        title = (m.group(1) or m.group(2) or "").strip()
        if not title:
            continue
        preceding = window[max(0, m.start() - 70):m.start()]
        if _NOT_TITLE_BEFORE_RE.search(preceding):
            continue
        if _TITLE_INTRO_RE.search(preceding):
            return title
    return None


def _deslug_gate_id(gate_id: str) -> str | None:
    """De-slugify a gate id into sentence-case prose.

    >>> _deslug_gate_id("publish-x-le-plus-gros-modele-2026-07-16")
    'Le plus gros modele'
    >>> _deslug_gate_id("publish-linkedin-org-100-agents-2026-07-02")
    'Org 100 agents'
    """
    if not gate_id:
        return None
    m = _ID_PUBLISH_PREFIX_RE.match(gate_id)
    rest = m.group(1) if m else gate_id
    rest = _ID_TRAILING_DATE_RE.sub("", rest)
    rest = rest.replace("-", " ").replace("_", " ").strip()
    if not rest:
        return None
    return rest[:1].upper() + rest[1:]


def gate_human_title(gate: dict | None) -> str:
    """Return a human, publish-ready headline for a gate card (Option A).

    See the module comment above this function for the full priority-order
    rationale. Always returns a non-empty string — the final fallback is
    the raw gate id (or a generic label if even that is missing), so a
    caller never needs a None-guard.

    Examples (real gate shapes, verified 2026-07-16):
        >>> gate_human_title({
        ...     "id": "publish-substack-essay-two-maps-ai-decade-2026-06-27",
        ...     "channel": "substack_post",
        ...     "summary": 'FREE Substack essay (thought-leadership, FR) '
        ...                '"Deux cartes pour la même décennie". Meta-frame...',
        ... })
        'Deux cartes pour la même décennie'
        >>> gate_human_title({
        ...     "id": "publish-linkedin-org-100-agents-2026-07-02",
        ...     "channel": "linkedin",
        ...     "summary": "LinkedIn post — compte JORIS, archétype "
        ...                '« Ce qu\\'on a construit » (build-in-public...).',
        ...     "image_hook": "Une société d'investissement dans quelques agents.",
        ... })
        "Une société d'investissement dans quelques agents."
        >>> gate_human_title({
        ...     "id": "publish-x-le-plus-gros-modele-2026-07-16",
        ...     "channel": "x",
        ...     "summary": "X thread (7 tweets, FR)...",
        ... })
        'Le plus gros modele'
        >>> gate_human_title({
        ...     "id": "publish-newsletter-ai-cost-war-2026-07-10",
        ...     "channel": "newsletter",
        ...     "created": "2026-07-10",
        ... })
        'Newsletter — 2026-07-10'
        >>> gate_human_title({})
        'Décision'
    """
    if not gate:
        return "Décision"
    gate_id = str(gate.get("id") or "").strip()
    channel = str(gate.get("channel") or "").strip().lower()

    # Newsletter special case — no topical title exists anywhere on this
    # kind (id is batch+date only); dedicated fallback ahead of de-slug.
    if channel.startswith("newsletter") or gate_id.startswith("publish-newsletter"):
        created = gate.get("created")
        if created:
            return f"Newsletter — {created}"
        return "Newsletter"

    summary = gate.get("summary")
    if isinstance(summary, str) and summary.strip():
        named = _quoted_named_title(summary)
        if named:
            return named

    image_hook = gate.get("image_hook")
    if isinstance(image_hook, str) and image_hook.strip():
        return image_hook.strip()

    fallback = _deslug_gate_id(gate_id)
    if fallback:
        return fallback

    return gate_id or "Décision"
