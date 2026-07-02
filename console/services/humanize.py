"""
humanize.py — translate technical enum slugs to operator French prose.

Used by the home page to render grouped décision-cards in human voice,
per Item E1 (Bureau-de-Cadre polish, {{OPERATOR}} flag msg 2709).

Mapping is intentionally tight (no fuzzy guessing). Unknown kinds fall
back to the snake_case → "snake case" stripped form (still pure prose,
no enum slug).
"""
from __future__ import annotations

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
