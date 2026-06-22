"""Activation PR body must be humanized + French (Bureau de Cadre).

{{OPERATOR}} directive (msg 2702 + 2708, 2026-05-21): the activation preview that
operators see is "Cérémonie d'arrivée de [Nom]", but the PR body markdown
inside it stayed in English with technical headings ("## Mandate", "## Recurring
missions", "## Layer outputs", "## Gate policy summary", "## Dry-run result",
"## Activation checklist", "## Risk notes"). Each English heading breaks the
Bureau-de-Cadre vocabulary substitution table (see the redesign agent's
briefing).

These tests pin the humanized French headings AND the body sentences so they
don't drift back. Per Maya identity: "expert finance, novice IA/tech, analogies
métier systématiques, zéro jargon nu" — every PR body line must read like a
concierge briefing the team, not like a DevOps changelog.

References:
- Notion v5 lines 977-995 (activation PR body spec — the heading names are
  illustrative, the vocabulary is ours to shape)
- /Users/{{OPERATOR_USER}}/claude-workspaces/Maya_Sales/webapp/templates/base.html for tone
"""
from __future__ import annotations

import pytest

from skill_lib.activation_pr import build_activation_pr_body


@pytest.fixture
def tony_state():
    return {
        "schema_version": 1,
        "slug": "tony",
        "display_name": "Tony",
        "owner": "operator",
        "created_at": "2026-05-18T08:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers",
            "skills_tools", "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-20T19:00:00Z",
        "commits": [
            {
                "sha": "deadbee",
                "step": "dry_run",
                "ts": "2026-05-20T18:00:00Z",
                "validated_at": "2026-05-20T18:00:00Z",
                "message": "tony: dry-run passed",
            },
        ],
    }


@pytest.fixture
def tony_dept():
    return {
        "department": {
            "slug": "tony",
            "display_name": "Tony",
            "level": "management",
            "mandate": (
                "Coordonne les départements ops, arbitre les conflits "
                "cross-équipe, écrit les directives prioritaires."
            ),
        },
        "layers": {"subscribed": [1, 4]},
        "recurring_missions": [],
        "skills": {
            "layer_1": ["cross-dept-aggregator"],
            "layer_4": ["directive-quality-auditor"],
        },
        "tools": ["github-api"],
        "gate_policies": {
            "priority_directive": {
                "current_mode": "manual_required",
                "eligible_future_modes": ["auto_if_policy_passed"],
            },
        },
    }


# ----- Headings: humanized French, no English -----

@pytest.mark.parametrize("english_heading", [
    "## Mandate",
    "## Recurring missions",
    "## Layer outputs",
    "## Gate policy summary",
    "## Dry-run result",
    "## Activation checklist",
    "## Risk notes",
])
def test_no_english_headings(english_heading, tony_state, tony_dept):
    """The Bureau-de-Cadre vocabulary forbids every English h2."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert english_heading not in body, (
        f"PR body still contains English heading {english_heading!r}. "
        f"Use the humanized French equivalent (see substitution table in "
        f"Phase D briefing)."
    )


@pytest.mark.parametrize("french_heading", [
    "## Sa mission",
    "## Ce qu'elle fera chaque jour",
    "## Ses 4 moments de la journée",
    "## Les décisions qu'elle prend",
    "## Sa répétition à blanc",
    "## Ce qu'il faut vérifier avant la cérémonie",
])
def test_humanized_french_headings_present(french_heading, tony_state, tony_dept):
    """Each humanized heading lands in the body verbatim."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert french_heading in body, (
        f"Expected humanized heading {french_heading!r} missing from PR body."
    )


# ----- Title + opening: humanized -----

def test_title_uses_lettre_d_arrivee(tony_state, tony_dept):
    """The h1 should read like a 'Lettre d'arrivée', not 'Activate X dept'."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert "Activate Tony department" not in body
    # Accept any of these humanized phrasings
    accepted = [
        "Lettre d'arrivée de Tony",
        "Cérémonie d'arrivée de Tony",
        "Bienvenue à Tony",
    ]
    assert any(t in body for t in accepted), (
        f"Expected one of {accepted} as the h1; got body starting with:\n"
        f"{body[:200]}"
    )


def test_branch_explanation_is_human(tony_state, tony_dept):
    """The 'Branch: onboarding/tony -> main' line should be human prose.
    Operator should understand 'fait passer Tony d'éclosion à en poste'."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    # No technical .yaml::field paths in the explanation
    assert "dept.yaml::department.status" not in body
    # Must contain a humanized version
    accepted_phrases = [
        "rejoindre officiellement l'équipe",
        "rejoint l'équipe",
        "rejoint officiellement l'équipe",
        "passe d'éclosion à en poste",
        "passer d'éclosion à en poste",
        "devient officiellement membre de l'équipe",
    ]
    assert any(p in body for p in accepted_phrases), (
        f"Expected one of {accepted_phrases} in branch explanation; got:\n"
        f"{body[:400]}"
    )


# ----- Layer outputs: human names -----

def test_layer_outputs_use_human_names(tony_state, tony_dept):
    """Layers 1-4 must use their human names from the vocabulary table."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    # Forbidden: literal "Layer 1" etc.
    for technical in ["**Layer 1**", "**Layer 2**", "**Layer 3**", "**Layer 4**"]:
        assert technical not in body, (
            f"PR body still uses technical {technical}. "
            f"Use the human moment names: Le matin / La recherche / "
            f"L'exécution / Le débrief du soir."
        )
    # Required: at least the 2 subscribed moments are named humanely.
    # Tony subscribes [1, 4].
    assert "Le matin" in body, "Layer 1 should render as 'Le matin'."
    assert "Le débrief du soir" in body, (
        "Layer 4 should render as 'Le débrief du soir'."
    )


# ----- Gate policy summary: human modes -----

def test_gate_modes_are_human(tony_state, tony_dept):
    """The 5 autonomy modes should be presented humanly, not as enum slugs."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    # tony has current_mode: manual_required + future: auto_if_policy_passed
    # The enum slug may still appear in a small mono parenthetical (operator
    # cross-reference) but the dominant phrasing must be French human.
    assert "Tu valides chaque fois" in body, (
        "manual_required should be presented as 'Tu valides chaque fois' "
        "(per vocabulary substitution table)."
    )


# ----- Checklist: human items -----

def test_checklist_is_in_french(tony_state, tony_dept):
    """The activation checklist items must be in French, action-oriented."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    # No English checklist items
    for english_item in [
        "Branch protection enabled on",
        "Dept secrets stored in",
        "deploy.target` reachable",
        "GitHub App installed on this repo",
        "Operator received Telegram pairing instructions",
    ]:
        assert english_item not in body, (
            f"Checklist still has English item: {english_item!r}"
        )
    # Must have French equivalents (concierge tone)
    assert "Avant la cérémonie" in body or "Avant d'envoyer" in body, (
        "Checklist must introduce itself in French."
    )


# ----- Footer: discreet, French -----

def test_footer_is_french_and_discreet(tony_state, tony_dept):
    """The 'Generated by...' line should be French + italic-mute tone."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert "Generated by `skills/" not in body, (
        "Footer is still English 'Generated by...'"
    )
    # Accept either a French generation line OR no footer at all
    # (silence is fine — the PR is signed by its content, not its tool)
    if "skill_lib/activation_pr.py" in body:
        # If we mention the source, it must be in italic French
        assert "Rédigée par" in body or "Préparée par" in body or \
               "Composée par" in body, (
            "Source mention should use French verb (Rédigée/Préparée/Composée)."
        )


# ----- No regression on existing structural tests -----

def test_body_still_mentions_dept_name_in_h1(tony_state, tony_dept):
    """Regression: the h1 must still name the dept."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert "Tony" in body.split("\n", 1)[0]


def test_body_still_includes_mandate_text(tony_state, tony_dept):
    """Regression: the mandate paragraph from dept.yaml lands in the body."""
    body = build_activation_pr_body("tony", tony_state, tony_dept)
    assert tony_dept["department"]["mandate"] in body
