"""test_gate_human_title.py — humanize.gate_human_title() rule chain
(Option A, Jade-validated mockup, card #666 follow-up, 2026-07-16).

No pending gate carries its own `title:` field. gate_human_title() derives
a real headline server-side in this priority order:

  1. A NAMED title quoted in `summary` — only when a title-bearing noun
     (essay/essai/article/note/post) appears earlier in the same clause.
     A quote that's a hook, an archetype label, or reported speech being
     refuted is NOT a title, even though it's syntactically identical
     (a bare quoted span).
  2. `image_hook` — LinkedIn gates already write a short, standalone,
     publish-ready phrase here.
  3. De-slugified `id` — strip `publish-<channel>-` + trailing date,
     hyphens -> spaces, sentence-case.
  4. Newsletter special case (checked FIRST in the real function, ahead of
     1-3, since a newsletter id/summary never carries a topical title) —
     "Newsletter — <created>".

Fixtures below mirror the real shapes found in the 12 live content-dept
gates (bubble-ops-content/queues/gates/, checked 2026-07-16) plus straight-
quote variants for the same rule, since real summaries mix « guillemets »
and "straight quotes" depending on which agent authored them.
"""
from __future__ import annotations

from console.services.humanize import gate_human_title


# ─── Tier 1 — quoted named title (guillemets and straight quotes) ─────────

def test_named_title_in_guillemets_after_essay_noun():
    """Real gate: Substack essay literally titled in « » after 'essay'."""
    gate = {
        "id": "publish-substack-essay-two-maps-ai-decade-2026-06-27",
        "channel": "substack_post",
        "summary": (
            "FREE Substack essay (thought-leadership, FR) "
            "« Deux cartes pour la même décennie ». Meta-frame, not a trade: "
            "Aschenbrenner lit la décennie IA comme une lentille de RISQUE..."
        ),
    }
    assert gate_human_title(gate) == "Deux cartes pour la même décennie"


def test_named_title_in_straight_quotes_after_essay_noun():
    """Same rule, straight ASCII quotes instead of « » — must match too."""
    gate = {
        "id": "publish-substack-essay-two-maps-ai-decade-2026-06-27",
        "channel": "substack_post",
        "summary": (
            'FREE Substack essay (thought-leadership, FR) '
            '"Deux cartes pour la même décennie". Meta-frame, not a trade...'
        ),
    }
    assert gate_human_title(gate) == "Deux cartes pour la même décennie"


def test_named_title_tolerates_one_parenthetical_between_noun_and_quote():
    """The title-intro noun can be separated from the quote by exactly one
    parenthetical aside (real shape: 'essay (thought-leadership, FR) "..."')
    — this is the case that a naive 15-char lookback window would miss."""
    gate = {
        "id": "publish-substack-post-x-2026-01-01",
        "channel": "substack",
        "summary": 'This is an article (some long parenthetical aside here) "The Real Title".',
    }
    assert gate_human_title(gate) == "The Real Title"


# ─── Tier 1 negative cases — quotes that are NOT titles ────────────────────

def test_quote_after_archetype_label_is_not_a_title():
    """Real gate (publish-linkedin-agent-qualification-59): the guillemet
    span « Impact / résultat » is an archétype LABEL, not a title — must
    fall through to image_hook."""
    gate = {
        "id": "publish-linkedin-agent-qualification-59-2026-07-02",
        "channel": "linkedin",
        "summary": (
            "LinkedIn post — compte JADE, archétype « Impact / résultat » "
            "(ops corpo, prospection, équipe augmentée). Registre POSITIF "
            "90/10 : un agent de qualification a traité 59 contacts..."
        ),
        "image_hook": "59 qualifiés pendant que je dormais.",
    }
    assert gate_human_title(gate) == "59 qualifiés pendant que je dormais."


def test_quote_of_a_refuted_claim_is_not_a_title():
    """Real gate (publish-linkedin-agent-muet-tuyau-pas-source): the quote
    "l'agent n'a rien fait" is a claim the post REFUTES (followed by
    '= FAUX'), not a title — must fall through to image_hook."""
    gate = {
        "id": "publish-linkedin-agent-muet-tuyau-pas-source-2026-07-12",
        "channel": "linkedin",
        "summary": (
            "LinkedIn post (pilier 2 build-in-public, bridge 3). Angle : "
            'notre pipeline concluait chaque semaine "l\'agent n\'a rien '
            'fait" = FAUX. Cause reelle = bug de MINING...'
        ),
        "image_hook": "La newsletter disait « rien à signaler ». Le carnet, lui, débordait.",
    }
    assert gate_human_title(gate) == (
        "La newsletter disait « rien à signaler ». Le carnet, lui, débordait."
    )


def test_quote_after_hook_marker_is_not_a_title():
    """Real gate shape: 'Hook (<=210 car.) : "..."' — the quote is the
    post's HOOK line, not a title. No image_hook on this gate shape ->
    falls all the way to the de-slugified id."""
    gate = {
        "id": "publish-linkedin-repondre-nest-pas-avoir-raison-2026-07-15",
        "channel": "linkedin",
        "summary": (
            "LinkedIn post — compte JADE, slot mercredi. "
            'Hook (<=210 car.) : "Nos agents sont revenus après une panne '
            'totale. On n\'a rien cru." Thèse : après la panne totale...'
        ),
    }
    assert gate_human_title(gate) == "Repondre nest pas avoir raison"


# ─── Tier 2 — image_hook fallback ──────────────────────────────────────────

def test_image_hook_used_when_no_named_title():
    gate = {
        "id": "publish-linkedin-org-100-agents-2026-07-02",
        "channel": "linkedin",
        "summary": (
            "LinkedIn post — compte JORIS, archétype « Ce qu'on a construit » "
            "(build-in-public, deep multi-agent architecture)..."
        ),
        "image_hook": "Une société d'investissement dans quelques agents.",
    }
    # 'Ce qu'on a construit' is preceded by 'archétype', a NOT_TITLE marker
    # -> rejected -> falls to image_hook.
    assert gate_human_title(gate) == "Une société d'investissement dans quelques agents."


def test_image_hook_wins_over_deslug_when_summary_has_no_quote():
    gate = {
        "id": "publish-linkedin-model-commoditization-2026-07-11",
        "channel": "linkedin",
        "summary": "LinkedIn post (pilier 2, POV mode 3). Angle : le prix d'un modele s'effondre...",
        "image_hook": "Quand le carburant devient gratuit, ce qui compte c'est le moteur.",
    }
    assert gate_human_title(gate) == (
        "Quand le carburant devient gratuit, ce qui compte c'est le moteur."
    )


# ─── Tier 3 — de-slugified id fallback ─────────────────────────────────────

def test_deslug_fallback_strips_publish_channel_prefix_and_date():
    gate = {
        "id": "publish-x-le-plus-gros-modele-2026-07-16",
        "channel": "x",
        "summary": "X thread (7 tweets, FR, @bubbleinvlabs) pilier 2 vers pont 3...",
    }
    assert gate_human_title(gate) == "Le plus gros modele"


def test_deslug_fallback_for_substack_note_with_no_hook():
    gate = {
        "id": "publish-substack-note-inference-devient-loyer-2026-07-11",
        "channel": "substack_note",
        "summary": (
            "Substack Note FR (pilier 3, lecture economique). 50 mots hors "
            "disclaimer. Thèse : quand l'inference s'effondre..."
        ),
    }
    assert gate_human_title(gate) == "Note inference devient loyer"


def test_deslug_fallback_when_summary_and_image_hook_both_absent():
    gate = {"id": "publish-linkedin-samedi-tout-recompte-2026-07-16", "channel": "linkedin"}
    assert gate_human_title(gate) == "Samedi tout recompte"


# ─── Tier 4 — newsletter special case ──────────────────────────────────────

def test_newsletter_uses_created_date_not_deslug():
    """Newsletter ids/summaries never carry a topical title (batch id +
    date only) — dedicated fallback fires ahead of the generic tiers even
    though this summary DOES contain a quoted span."""
    gate = {
        "id": "publish-newsletter-ai-cost-war-2026-07-10",
        "channel": "newsletter",
        "created": "2026-07-10",
        "summary": (
            '"Newsletter Friday 2026-07-10 — batch 20260710_193431. Thèse : '
            "la semaine du 7-9 juillet 2026 a mis en scène la "
            'commoditisation..."'
        ),
    }
    assert gate_human_title(gate) == "Newsletter — 2026-07-10"


def test_newsletter_without_created_falls_back_to_bare_label():
    gate = {"id": "publish-newsletter-2026-07-10", "channel": "newsletter"}
    assert gate_human_title(gate) == "Newsletter"


def test_newsletter_detected_via_id_when_channel_missing():
    """Some newsletter gates may omit `channel` — id prefix alone must
    still route to the newsletter fallback."""
    gate = {"id": "publish-newsletter-2026-06-30", "created": "2026-06-30"}
    assert gate_human_title(gate) == "Newsletter — 2026-06-30"


# ─── Edge cases ─────────────────────────────────────────────────────────────

def test_empty_gate_returns_generic_label():
    assert gate_human_title({}) == "Décision"
    assert gate_human_title(None) == "Décision"


def test_no_id_no_summary_no_hook_returns_generic_label():
    gate = {"channel": "linkedin"}
    assert gate_human_title(gate) == "Décision"


def test_dept_slug_never_appears_in_returned_title():
    """The dept slug ('content', always identical on this page) must never
    leak into the returned title — every tier derives from summary/
    image_hook/id, never from gate.slug."""
    gate = {
        "id": "publish-x-le-plus-gros-modele-2026-07-16",
        "slug": "content",
        "channel": "x",
        "summary": "X thread...",
    }
    assert "content" not in gate_human_title(gate).lower()


def test_all_12_real_pending_gates_produce_readable_non_empty_titles():
    """Regression guard mirroring the 2026-07-16 manual verification against
    the live bubble-ops-content queue (12 pending gates) — every one must
    produce a non-empty, non-generic title, and the dept slug must never
    leak into any of them."""
    real_shapes = [
        {
            "id": "publish-linkedin-agent-muet-tuyau-pas-source-2026-07-12",
            "channel": "linkedin",
            "summary": 'Angle : notre pipeline concluait chaque semaine "l\'agent '
                       'n\'a rien fait" = FAUX.',
            "image_hook": "La newsletter disait « rien à signaler ». Le carnet, lui, débordait.",
        },
        {
            "id": "publish-linkedin-agent-qualification-59-2026-07-02",
            "channel": "linkedin",
            "summary": "compte JADE, archétype « Impact / résultat » (ops corpo...).",
            "image_hook": "59 qualifiés pendant que je dormais.",
        },
        {
            "id": "publish-linkedin-model-commoditization-2026-07-11",
            "channel": "linkedin",
            "summary": "LinkedIn post (pilier 2, POV mode 3)...",
            "image_hook": "Quand le carburant devient gratuit, ce qui compte c'est le moteur.",
        },
        {
            "id": "publish-linkedin-org-100-agents-2026-07-02",
            "channel": "linkedin",
            "summary": "compte JORIS, archétype « Ce qu'on a construit » (build-in-public...).",
            "image_hook": "Une société d'investissement dans quelques agents.",
        },
        {
            "id": "publish-linkedin-repondre-nest-pas-avoir-raison-2026-07-15",
            "channel": "linkedin",
            "summary": 'Hook (<=210 car.) : "Nos agents sont revenus après une panne totale."',
        },
        {
            "id": "publish-linkedin-samedi-tout-recompte-2026-07-16",
            "channel": "linkedin",
            "summary": 'Hook (<=210 car.) : "Samedi, marchés fermés, aucun ordre à passer."',
        },
        {
            "id": "publish-newsletter-ai-cost-war-2026-07-10",
            "channel": "newsletter",
            "created": "2026-07-10",
            "summary": '"Newsletter Friday 2026-07-10 — batch 20260710_193431..."',
        },
        {
            "id": "publish-substack-essay-two-maps-ai-decade-2026-06-27",
            "channel": "substack_post",
            "summary": 'FREE Substack essay (thought-leadership, FR) "Deux cartes '
                       'pour la même décennie". Meta-frame...',
        },
        {
            "id": "publish-substack-note-frontier-premium-evaporated-2026-07-16",
            "channel": "substack",
            "summary": "Substack Note EN (pilier 3, lecture economique)...",
        },
        {
            "id": "publish-substack-note-inference-devient-loyer-2026-07-11",
            "channel": "substack_note",
            "summary": "Substack Note FR (pilier 3, lecture economique)...",
        },
        {
            "id": "publish-substack-note-verifier-vs-se-sentir-gueri-2026-07-12",
            "channel": "substack_note",
            "summary": "Substack Note FR (pilier 3 discipline epistemique)...",
        },
        {
            "id": "publish-x-le-plus-gros-modele-2026-07-16",
            "channel": "x",
            "summary": "X thread (7 tweets, FR, @bubbleinvlabs)...",
        },
    ]
    expected = {
        "publish-linkedin-agent-muet-tuyau-pas-source-2026-07-12":
            "La newsletter disait « rien à signaler ». Le carnet, lui, débordait.",
        "publish-linkedin-agent-qualification-59-2026-07-02":
            "59 qualifiés pendant que je dormais.",
        "publish-linkedin-model-commoditization-2026-07-11":
            "Quand le carburant devient gratuit, ce qui compte c'est le moteur.",
        "publish-linkedin-org-100-agents-2026-07-02":
            "Une société d'investissement dans quelques agents.",
        "publish-linkedin-repondre-nest-pas-avoir-raison-2026-07-15":
            "Repondre nest pas avoir raison",
        "publish-linkedin-samedi-tout-recompte-2026-07-16":
            "Samedi tout recompte",
        "publish-newsletter-ai-cost-war-2026-07-10":
            "Newsletter — 2026-07-10",
        "publish-substack-essay-two-maps-ai-decade-2026-06-27":
            "Deux cartes pour la même décennie",
        "publish-substack-note-frontier-premium-evaporated-2026-07-16":
            "Note frontier premium evaporated",
        "publish-substack-note-inference-devient-loyer-2026-07-11":
            "Note inference devient loyer",
        "publish-substack-note-verifier-vs-se-sentir-gueri-2026-07-12":
            "Note verifier vs se sentir gueri",
        "publish-x-le-plus-gros-modele-2026-07-16":
            "Le plus gros modele",
    }
    for gate in real_shapes:
        title = gate_human_title(gate)
        assert title, f"empty title for {gate['id']}"
        assert title != "Décision", f"generic fallback for {gate['id']}"
        assert "content" not in title.lower(), f"dept slug leaked for {gate['id']}"
        assert title == expected[gate["id"]], (
            f"{gate['id']}: got {title!r}, expected {expected[gate['id']]!r}"
        )
