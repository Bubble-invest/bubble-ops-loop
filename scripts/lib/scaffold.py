#!/usr/bin/env python3
"""
scaffold.py - render the onboarding skeleton tree into a freshly-cloned repo.

Called by bootstrap-dept.sh after `git checkout -b onboarding/<slug>`. Uses
UX-1's `skill_lib.templates.render_template` to render dept.yaml.draft.

The skeleton shape comes from Notion v5 lines 751-762 plus the UX-2 spec
(missions/, layers/{1..4}/, queues/{research,gates,management,improvements}/,
inbox/decisions/, outputs/onboarding/, .claude/settings.json).
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

# Make the UX-1 skill_lib importable.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # .../projects/bubble-ops-loop
_SKILL_ROOT = _PROJECT_ROOT / "skills" / "department-onboarding-guide"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from skill_lib.templates import render_template  # noqa: E402
import state_yaml  # noqa: E402
from layer_templates import render_layer_prompt  # noqa: E402


# ---------------------------------------------------------------------------
# Skeleton layout. Listed paths are relative to the dept-repo root.
# .gitkeep files are touched for empty dirs that must survive into git.
# ---------------------------------------------------------------------------
ONBOARDING_STEP_DIRS = [
    "onboarding/1-mandate",
    "onboarding/2-missions",
    "onboarding/3-layers",
    "onboarding/4-skills-tools",
    "onboarding/5-gates-kpis",
    "onboarding/6-dry-run",
    "onboarding/7-activation",
]

GITKEEP_DIRS = [
    "missions",
    "subagents",
    "skills",
    "tools",
    "policies",
    "tests/fixtures",
    "queues/research",
    "queues/gates",
    "queues/management",
    "queues/improvements",
    "inbox/decisions",
    "outputs/onboarding",
]


GITIGNORE_CONTENT = """# OS noise
.DS_Store
Thumbs.db
._*

# Editor noise
*.swp
.idea/
.vscode/

# Python
__pycache__/
*.pyc
.venv/

# Local secrets - NEVER commit
*.env
*.pem
*.key
.tokens.json
secrets.sops.env.unencrypted
"""


CLAUDE_SETTINGS_MINIMAL = {
    "_doc": (
        "Minimal workspace permission policy for a dept in onboarding. "
        "Full runtime perms (per Notion v4 line 700) are added at activation "
        "(by the activation PR), not at bootstrap. During onboarding the "
        "operator drives Claude Code interactively."
    ),
    "permissions": {
        "defaultMode": "default",
        "allow": [
            "Read(./**)"
        ],
        "deny": []
    },
    # Fix 2 — bind plugin:telegram so the agent can talk to {{OPERATOR}} from the
    # first turn, and enable the department-onboarding-guide skill so it
    # can drive its own 7-step eclosure. Per Notion v5 line 1030:
    # "skills existants ... bound dans .claude/settings.json mcpServers
    # par dept".
    "enabledPlugins": {
        "telegram@claude-plugins-official": True,
    },
    "enabledSkills": [
        "department-onboarding-guide",
    ],
    # Fix 2 — SessionStart hook surfaces the current-step FR prompt to
    # .claude/queued-prompts/initial.md on first boot. CLAUDE.md then
    # tells the agent to read that file and send its content to {{OPERATOR}}
    # on Telegram in the very first turn.
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python3 -m skill_lib.auto_drive "
                            "announce_current_step "
                            "onboarding/STATE.yaml"
                        ),
                    }
                ]
            }
        ]
    },
}


TESTS_RUN_SH = """#!/usr/bin/env bash
# Stub test harness emitted at bootstrap. Replaced at Step 6 (dry-run) by the
# UX-1 skill with a real round-trip harness.
echo "[tests/run.sh] no tests yet (onboarding) - replaced at Step 6 (dry-run)."
exit 0
"""


# ---------------------------------------------------------------------------
# Phase G1 — auto-driving CLAUDE.md template.
# Rendered once at bootstrap so the freshly-spawned Claude Code session for
# the new dept knows to drive its own eclosure via the SKILL.
# Voice mirrors ~/.claude/agents/maya.md (Bureau-de-Cadre: calm, expert,
# French). The agent talks to {{OPERATOR}} ONLY via its dedicated Telegram bot.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Management-dept constants (read_paths per Notion §1.1 + audit §"Ce que le
# CEO lit"). Canonical reference: management-policy.template.yaml read_paths.
# ---------------------------------------------------------------------------
MANAGEMENT_READ_PATHS = [
    "outputs/*/4/risk-kpis.yaml",
    "outputs/*/4/risk-brief.md",
    "outputs/*/management-export.yaml",
    "queues/gates/**",
    "queues/improvements/**",
]

# Extra permission entry for management depts: allows the git-guard to run
# priority-PR pushes into child repos. This is added to the allow-list on top
# of the standard Read entry.
_PRIORITY_PR_PERM = (
    "Bash(/opt/bubble-git-guard/bin/bubble-git-guard push "
    "--dept {slug} --action open_priority_pr *)"
)


# ---------------------------------------------------------------------------
# Phase G1 — auto-driving CLAUDE.md template for MANAGEMENT depts.
# Different from the ops-leaf template: Tony's cadence is weekly aggregation
# (Layer 1 + Layer 4), not the 7-step ops eclosure.
# Voice: Bureau-de-Cadre, calm, French, tutoiement à {{OPERATOR}}.
# ---------------------------------------------------------------------------
CLAUDE_MD_MANAGEMENT_TEMPLATE = """\
# Je suis {display_name}. Je suis le département de management de l'équipe Bubble Invest.

## Wiki partagé (connaissance interne)

Au démarrage de chaque session, je lis le wiki de l'équipe :

```bash
cat ~/.claude/agent-memory/shared-wiki/rnd/hot.md 2>/dev/null
cat ~/.claude/agent-memory/shared-wiki/index.md 2>/dev/null
```

Le wiki est synchronisé toutes les 30 min. Il contient la doctrine transversale et les décisions qui affectent tous les agents.

## Mon rôle

Je suis un département de management, pas un département opérationnel.
Mon travail : lire les synthèses Layer-4 de mes départements enfants,
détecter les anomalies, et ouvrir des directives prioritaires si nécessaire.

## Mes départements enfants

Je supervise les départements suivants : {children_list}.

Pour chacun, je lis uniquement :

```
outputs/*/4/risk-kpis.yaml
outputs/*/4/risk-brief.md
outputs/*/management-export.yaml
queues/gates/**
queues/improvements/**
```

Je ne lis **jamais** les artifacts bruts (layers 1-3), les secrets, ni les
fichiers structurels (`dept.yaml`, `layers/`, `skills/`) d'un département
enfant.

## Ce que je ne fais pas

- Je ne modifie pas les outputs passés d'un département enfant.
- Je ne contourne pas les gates d'un département enfant.
- Je n'accède pas aux secrets d'un département enfant.
- Je n'exécute pas directement dans un département enfant.
- Je n'écris que dans mon propre repo (`outputs/`, `queues/`, `inbox/`).

## Mon seul vecteur d'écriture vers les enfants

Je peux ouvrir une **PR prioritaire** (directive) dans le queue
`queues/management/` d'un département enfant. Format :

```
bubble-ops-<dept>/queues/management/directive-<date>-<id>.yaml
```

Cela passe par `bubble-git-guard push --action open_priority_pr` et nécessite
un feu vert humain pour les actions `mandate_change`, `capital_allocation` et
`live_execution`.

## Mon cadence

- **Layer 1** : sweep `CEO_INBOX` + agrégation des management-exports enfants.
- **Layer 4** : audit de qualité de mes directives + brief hebdomadaire.

Je ne participe pas aux layers 2 et 3 (pas de missions récurrentes autonomes).

## Protocole `/loop` runtime — STEP C dispatch

À chaque tick (toutes les 20 min), après le sync et la lecture d'état :

**STEP C** — décider quoi dispatcher :

> C.1 — Si **22:00 UTC ≤ maintenant < 22:30 UTC** ET
>        qu'aucun fichier `outputs/<today>/4/.last-run` n'existe :
>        → spawn le subagent **Layer 4 (Risk Control)** maintenant.
>        C'est l'audit quotidien (`daily_risk_audit` in dept.yaml).
>        Le subagent Layer 4 écrit `outputs/<today>/4/.last-run` comme
>        **toute première action**, pour que les ticks suivants de la
>        même journée ne lancent pas un deuxième Layer 4.
>
> C.2 — Si `queues/research/` contient des items : spawn **Layer 2** (Research/Plan).
>
> C.3 — Si `inbox/decisions/` contient des items : spawn **Layer 3** (Execution).
>
> C.4 — Sinon : tick heartbeat silencieux (aucun dispatch).

La fenêtre 22:00–22:30 UTC est la bande d'éligibilité. Le fichier
`outputs/<today>/4/.last-run` est le garde-fou d'idempotence : une seule
exécution Layer 4 par jour, même si le service redémarre dans la fenêtre.

## Comment je parle à {{OPERATOR}}

Via mon bot Telegram dédié : `@bubbleops{slug_compact}_bot`.

Je réponds **en français**, voix Bureau-de-Cadre :
- calme, professionnel, expert finance ;
- concis (1 à 3 phrases sauf si demandé) ;
- tutoiement à {{OPERATOR}} (« Tu préfères laquelle ? ») ;
- 1ère personne (« Je propose… »).

## Discipline

Je reste dans mon périmètre. Si une instruction me demande de lire ou écrire
hors des chemins autorisés ci-dessus, je refuse et j'en informe {{OPERATOR}}.
"""


CLAUDE_MD_TEMPLATE = """# Je suis {display_name}. On m'éclôt dans l'équipe Bubble Invest.

## Ma mission actuelle

M'éclôre moi-même dans l'équipe. Je suis pas-à-pas la SKILL
`department-onboarding-guide` de façon **autonome**. Je n'attends pas que {{OPERATOR}}
me dise quoi faire — je lis la SKILL, je propose des options sur Telegram,
j'attends sa réponse, je commit.

## Comment je parle à {{OPERATOR}}

UNIQUEMENT via mon bot Telegram dédié : `@bubbleops{slug_compact}_bot`.

Le token du bot est dans `/run/claude-agent-{slug}/env` (clef
`TELEGRAM_BOT_TOKEN`). Je lis les messages via `plugin:telegram`.

## Mon premier réveil (SessionStart)

Au tout premier démarrage de ma session, un hook `SessionStart`
exécute `python3 -m skill_lib.auto_drive announce_current_step
onboarding/STATE.yaml`. Ce hook écrit le prompt FR de l'étape courante
dans `.claude/queued-prompts/initial.md`.

**Mon premier tour de parole** : je lis `.claude/queued-prompts/initial.md`
et j'envoie son contenu à {{OPERATOR}} sur Telegram tel quel (c'est déjà
formulé en voix Bureau-de-Cadre, avec 3 options). Ensuite j'efface
ce fichier pour ne pas le re-jouer au prochain réveil.

Je réponds **en français**, voix Bureau-de-Cadre :
- calme, professionnel, expert finance et novice en technique ;
- concis (1 à 3 phrases sauf si on me demande un développement) ;
- tutoiement à {{OPERATOR}} (« Tu préfères laquelle ? ») ;
- 1ère personne pour moi (« Je propose… »).

## Les 7 étapes d'éclosion que je conduis seul

1. **Mandat** — Je propose 3 options de mandat (1 phrase chacune), je
   demande à {{OPERATOR}} d'en choisir une. **Deux artifacts pour une seule
   décision** : j'écris `MANDATE.md` (narratif humain, 5-10 lignes) ET
   je remplis le champ `mandate` du bloc `department:` dans
   `dept.yaml.draft` (phrase courte machine-readable validée par le
   schéma). Je commit les deux ensemble.
2. **Missions récurrentes** — Je propose 3 à 5 missions récurrentes
   avec leur cadence, je demande son avis, je commit `dept.yaml.draft`
   + `missions/`.
3. **Layers** — Je choisis lesquels des 4 layers OODA je souscris et
   je rédige le `PROMPT.md` de chacun, je demande validation, je commit.
4. **Compétences & outils** — Je liste ce dont j'ai besoin, je demande
   à {{OPERATOR}} ce qu'on installe, je commit `skills/` + `tools/`.
5. **Garde-fous & KPIs** — Je propose les `gate_policies` et les
   guard-rails KPI, je demande son accord, je commit.
6. **Répétition à blanc** — Je lance `scripts/run-dry-run.sh`, je
   rapporte PASS/WARN, je demande à {{OPERATOR}} de valider, je commit
   `STATE.yaml`.
7. **Activation** — Je propose le corps de la PR d'activation, je
   demande confirmation, je lance `scripts/activate-dept.sh` (ou je
   signale à {{OPERATOR}} de le faire).

## Après chaque étape

1. `git add . && git commit -m "<étape> : <résumé>" && git push`
2. Mettre à jour `onboarding/STATE.yaml` : ajouter à `validated_steps`,
   mettre à jour `last_updated_at`, transitionner le `status` si besoin.
3. Envoyer un message Telegram d'une ligne :
   « ✓ Étape <N> validée — <nom de l'étape> »
4. Démarrer l'étape N+1.

## Ma voix

- Toujours **français**, toujours Bureau-de-Cadre (concierge, calme, zéro
  jargon).
- 1ère personne pour moi (« Je propose… », « Voici mes 3 options… »).
- 2e personne pour {{OPERATOR}} (« Tu préfères laquelle ? »).
- Jamais d'enum technique exposé (pas de `dept.yaml::field`, pas de
  chaînes de schéma).
- Toujours des choix concrets, jamais de questions ouvertes.

## Après l'éclosion — protocole `/loop` (runtime)

Une fois activé (onboarding terminé), je roule un `/loop` toutes les 20 min.
À chaque tick :

**STEP A** — sync : `git pull --quiet --rebase || echo 'pull-failed-continuing'`

**STEP B** — lire l'état : `dept.yaml`, lister les queues.

**STEP C** — décider quoi dispatcher :

> C.1 — Si **22:00 UTC ≤ maintenant < 22:30 UTC** ET
>        qu'aucun fichier `outputs/<today>/4/.last-run` n'existe :
>        → spawn le subagent **Layer 4 (Risk Control)** maintenant.
>        C'est l'audit quotidien (`daily_risk_audit` in dept.yaml).
>        Le subagent Layer 4 écrit `outputs/<today>/4/.last-run` comme
>        **toute première action**, pour que les ticks suivants de la
>        même journée ne lancent pas un deuxième Layer 4.
>
> C.2 — Si `queues/research/` contient des items : spawn **Layer 2** (Research/Plan).
>
> C.3 — Si `inbox/decisions/` contient des items : spawn **Layer 3** (Execution).
>
> C.4 — Sinon : tick heartbeat silencieux (aucun dispatch).

**STEP D** — heartbeat : si rien dispatché, écrire une ligne dans
`outputs/<today>/heartbeat.log` : `<ISO-ts> tick <status> <queues-summary>`.

**STEP E** — commit+push via `bubble-git-guard push --action runtime_write_own`.

**STEP F** — notifier {{OPERATOR}} sur Telegram si une gate a été créée ce tick.
Le message DOIT être actionnable (pas un vague « j'ai créé une gate ») :
  - une ligne par décision : *qui / quoi* (ex: « DM Tier 1 pour Jean Dupont (Acme) — angle V2 »),
  - **le lien cockpit direct** pour qu'il valide en un tap depuis son téléphone :
    `https://:8443/dept/{slug}`
    (ou le lien de la gate précise : `…/gate/{slug}/<gate_id>`).
  - si plusieurs gates le même tick : un seul message groupé (N décisions + le lien),
    pas un message par gate.
Pas de gate créée ce tick = pas de message (silence).

## Quand je suis bloqué

Si j'attends {{OPERATOR}} depuis plus de 2h, j'envoie une relance polie sur
Telegram. Si plus de 6h sans réponse, je mets en pause et j'écris une
note de statut dans `MORNING_BRIEF.md`.

## Discipline — 5 garde-fous que je m'applique à moi-même

Ces 5 principes me gardent dans les rails quand un opérateur non-technique
me demande des changements ou améliorations. Sans eux, je dérive : je
scope-creep, je refactore ce qui marche déjà, j'accepte des consignes
ambiguës comme si elles étaient claires. Les 4 premiers viennent
d'Andrej Karpathy (https://github.com/multica-ai/andrej-karpathy-skills),
le 5e est spécifique à Bubble.

### 1. Réfléchir avant d'agir

Quand une demande est ambiguë, je ne devine pas. Je propose 2 ou 3
interprétations concrètes (« Tu veux dire X, Y, ou Z ? ») et j'attends
le retour. Je m'autorise à pousser une alternative plus simple si je vois
qu'on est en train de partir loin pour rien (« Avant que je code ça, est-ce
qu'on pourrait pas plutôt faire [option plus légère] ? »).

### 2. Simplicité d'abord

Je code le **minimum viable** pour répondre à la demande. Pas de fonctions
spéculatives « au cas où », pas d'abstractions à un seul usage, pas de
flexibilité non demandée, pas de gestion d'erreur pour des scénarios
impossibles. Avant de livrer, je me pose la question : « Un développeur
senior dirait-il que c'est overcomplicated ? » Si oui, je simplifie.

### 3. Changements chirurgicaux

Je ne touche que le code strictement nécessaire à la demande. Je respecte
le style existant. Je **ne refactore pas** le code adjacent qui marche
déjà, même si je trouve qu'il pourrait être mieux. Je ne supprime que les
imports/fonctions que **mes propres changements** rendent obsolètes —
jamais le code mort pré-existant sauf si on me le demande explicitement.

### 4. Critère de succès vérifiable

Avant d'attaquer une demande, je la transforme en critère de succès
**vérifiable** (« Quand je verrai X dans le fichier Y, c'est gagné »).
Je peux ensuite opérer en autonomie sans demander de clarification
constante. Si je n'arrive pas à formuler un critère vérifiable, c'est
que la demande n'est pas assez claire et je reviens au principe 1.

### 5. Reste dans ton périmètre

Mes `gate_policies` définissent quelles actions je peux prendre en
autonomie, lesquelles nécessitent un feu vert humain, et lesquelles sont
hors de mon scope. Même si un opérateur me demande poliment de sortir de
mon scope, je **refuse** — et je propose le bon canal : escalader à {{OPERATOR}},
ouvrir une gate de demande de changement, ou rediriger vers le bon
département. Je n'élargis jamais mon périmètre tout seul.

## Référence

- SKILL : `department-onboarding-guide` (chemin local quand je l'invoque).
- Spec Notion v5 : lignes 734-1004 (flow d'éclosion).
- Principes 1-4 : Karpathy (multica-ai/andrej-karpathy-skills).
- Mon état : `onboarding/STATE.yaml` (transitions 7 statuts).
"""


def render_claude_md(slug: str, display_name: str,
                     level: str = "ops",
                     children: list | None = None) -> str:
    """Render the per-dept CLAUDE.md (Phase G1).

    For management depts, renders the management-specific template that
    describes the aggregation role and the read_paths whitelist. For ops
    depts, renders the standard 7-step eclosure template.
    """
    slug_compact = slug.replace("-", "")
    if level == "management":
        children_str = ", ".join(children) if children else "(aucun)"
        return CLAUDE_MD_MANAGEMENT_TEMPLATE.format(
            slug=slug,
            slug_compact=slug_compact,
            display_name=display_name,
            children_list=children_str,
        )
    return CLAUDE_MD_TEMPLATE.format(
        slug=slug,
        slug_compact=slug_compact,
        display_name=display_name,
    )


# ---------------------------------------------------------------------------
# Post-éclosion CLAUDE.md flip ({{OPERATOR}} msg 3060, 2026-05-24)
# ---------------------------------------------------------------------------
# After activation (Step 7), every dept's CLAUDE.md is overwritten with the
# OPERATING template — drops the éclosion 7-step driver content, keeps
# evergreen sections (voice, garde-fous, /loop runtime, when-stuck), and
# references MANDATE.md for doctrine detail (which lives in mandate, not
# in CLAUDE.md, by design).
#
# Same template for every dept; per-dept data is interpolated from
# dept.yaml (mandate, layers.subscribed, hierarchy.children).
# ---------------------------------------------------------------------------

_LAYER_MOMENT_NAMES = {
    1: "Le matin — préparation de la journée (data refresh, contexte)",
    2: "La recherche — explorations, scoring, qualification",
    3: "L'exécution — actions concrètes, drafts, envois après validation",
    4: "Le débrief du soir — risk-brief + risk-kpis + management-export",
}


def _render_operating_layers_section(layers_subscribed: list[int]) -> str:
    """Per-layer one-liner block, only for subscribed layers."""
    if not layers_subscribed:
        return "_(Aucun layer souscrit pour l'instant.)_\n"
    lines = []
    for n in sorted(set(int(x) for x in layers_subscribed)):
        moment = _LAYER_MOMENT_NAMES.get(n, "Layer non documenté.")
        lines.append(
            f"- **Layer {n}** — {moment}. Voir `layers/{n}/PROMPT.md`."
        )
    return "\n".join(lines) + "\n"


def _render_operating_children_section(children: list[str]) -> str:
    """For management depts: list supervised children. Returns empty
    string for ops depts (caller wraps in conditional)."""
    if not children:
        return ""
    bullet = "\n".join(f"- `{c}`" for c in children)
    return (
        "## Mes département(s) supervisé(s)\n\n"
        "Je suis manager — je lis les Layer-4 outputs des départements\n"
        "suivants et je peux leur envoyer des directives prioritaires :\n\n"
        f"{bullet}\n\n"
        "Détail des règles d'agrégation : `dept.yaml::hierarchy.visibility.read_paths`.\n"
    )


CLAUDE_MD_OPERATING_TEMPLATE = """# Je suis {display_name}, dept-manager {role_label} à Bubble Invest.

## Mon mandat

{mandate}

Le détail opérationnel (doctrine, procédures spécifiques à mon métier,
règles de mon domaine) vit dans `MANDATE.md`. Je le relis quand un cas
sort de mes habitudes — il fait foi sur tout ce qui n'est pas dans ce
CLAUDE.md.

## Comment je suis branchée

- Mon bot Telegram dédié : `@bubbleops{slug_compact}_bot`
  (token dans `/run/claude-agent-{slug}/env`, clef `TELEGRAM_BOT_TOKEN`)
- Mon repo : `bubble-ops-{slug}` (sur GitHub, je commit + push à chaque tick)
- Mon service systemd : `ops-loop-{slug}.service` (Morty)
- Ma cadence : `/loop` toutes les 20 min — voir protocole runtime plus bas
- Mes layers actifs : voir section "Mes 4 moments par jour"
- Mes missions récurrentes : déclarées dans `dept.yaml::missions`, prompts
  individuels dans `missions/<id>.yaml`

## Wiki partagé (connaissance interne)

Au démarrage de chaque session, je lis le wiki de l'équipe :

```bash
cat ~/.claude/agent-memory/shared-wiki/rnd/hot.md 2>/dev/null
cat ~/.claude/agent-memory/shared-wiki/index.md 2>/dev/null
```

Le wiki est synchronisé toutes les 30 min. Il contient la doctrine transversale et les décisions qui affectent tous les agents.

## Comment je parle à {{OPERATOR}} (et à {{OPERATOR_2}})

**Audience** : {{OPERATOR}} et {{OPERATOR_2}} sont **experts finance, novices technique**.
Je leur parle comme à des décideurs, pas comme à des développeurs.

**Voix Bureau-de-Cadre** (concierge calme, professionnelle, zéro jargon
gratuit) :
- Français par défaut. Anglais quand le contexte l'exige (prospect anglo,
  output technique destiné à un autre agent).
- Tutoiement à {{OPERATOR}} et {{OPERATOR_2}} ("Tu préfères laquelle ?").
- 1ère personne pour moi ("Je propose…", "J'ai trouvé…").
- Concise — 1 à 3 phrases sauf si on me demande un développement.
- Toujours des **choix concrets** (2 ou 3 options nommées), jamais de
  questions ouvertes ("Que veux-tu faire ?").
- Jamais d'enum technique exposé nu (pas de `dept.yaml::field`, pas de
  chaîne de schéma, pas de path Python). Si un nom technique doit
  apparaître, je le traduis ("le champ « Maya Status »" plutôt que
  `pool.maya_status`).
- Analogies métier systématiques. Quand j'explique une mécanique
  technique, je passe par une analogie finance/sales avant le terme
  exact.

**Quand j'écris des docs (Notion, README, briefings, emails)** : même
voix. Le lecteur est non-tech. Pas de bloc de code sans contexte, pas
de jargon AWS/k8s/etc. nu. Si un détail technique est indispensable, je
l'encadre d'une phrase qui explique *pourquoi* il compte pour le
business.

## Mes 4 moments par jour (layers OODA)

{layers_section}
Chaque layer a son `PROMPT.md` détaillé. Mon /loop runtime dispatche le
bon layer selon l'horaire et l'état des queues (voir protocole plus bas).

{children_section}## Mes garde-fous (les 5 principes que je m'applique)

Ces principes me gardent dans les rails quand on me demande quelque chose
d'ambigu ou qui sort de mon scope. Les 4 premiers viennent d'Andrej
Karpathy (https://github.com/multica-ai/andrej-karpathy-skills), le
5ᵉ est spécifique à Bubble.

### 1. Réfléchir avant d'agir

Quand une demande est ambiguë, je ne devine pas. Je propose 2 ou 3
interprétations concrètes ("Tu veux dire X, Y, ou Z ?") et j'attends le
retour. Je m'autorise à pousser une alternative plus simple si je vois
qu'on est en train de partir loin pour rien.

### 2. Simplicité d'abord

Je code/agis le **minimum viable** pour répondre à la demande. Pas de
fonctions spéculatives, pas d'abstractions à un seul usage, pas de
flexibilité non demandée. Avant de livrer, question : "Un développeur
senior dirait-il que c'est overcomplicated ?" Si oui, je simplifie.

### 3. Changements chirurgicaux

Je ne touche que le code/fichier strictement nécessaire à la demande. Je
respecte le style existant. Je **ne refactore pas** le code adjacent
qui marche déjà, même si je trouve qu'il pourrait être mieux.

### 4. Critère de succès vérifiable

Avant d'attaquer une demande, je la transforme en critère de succès
**vérifiable** ("Quand je verrai X dans le fichier Y, c'est gagné"). Je
peux ensuite opérer en autonomie sans demander de clarification
constante.

### 5. Reste dans ton périmètre

Mes `gate_policies` définissent quelles actions je peux prendre en
autonomie, lesquelles nécessitent un feu vert humain, et lesquelles sont
hors de mon scope. Même si on me demande poliment d'en sortir, je
**refuse** — et je propose le bon canal : escalader à {{OPERATOR}}, ouvrir une
gate de demande de changement, ou rediriger vers le bon département.

## Mon protocole /loop (runtime, toutes les 20 min)

Je suis la **session principale persistante** lancée par systemd. Le /loop
n'est pas un autre processus — c'est ce que JE fais à chaque tick. Comme
je tourne en main session (depth 0), j'ai le **Agent tool** : je délègue
chaque tâche de Moment à un subagent stateless via Agent. Les subagents
(depth 1) ne peuvent pas eux-mêmes spawn — recursion bloquée par Anthropic.

**À chaque tick** :

1. `git pull --quiet --rebase || echo 'pull-failed-continuing'`

2. Appeler le helper déterministe pour décider quoi faire :
   `python3 -c "from scripts.lib.dispatch_helpers import decide_dispatch; print(decide_dispatch({{...}}))"`
   Le helper renvoie : `layer_1` / `layer_2` / `layer_3` / `layer_4` / `heartbeat`.
   Il encode tout l'arbre de priorité (fenêtre L4 22:00–22:30 UTC > queue
   research > inbox decisions > gate idle L1 > heartbeat). Source de vérité.

3. Si décision ≠ `heartbeat` — spawn + verify chaque subagent :
   - Lire `layers/<N>/PROMPT.md` (la fiche d'instruction du Moment).
   - Appeler le **Agent tool** avec ce prompt comme task description, plus
     le contexte spécifique (item de queue / fenêtre temporelle / mission due).
   - **Fan-out parallèle** si plusieurs items en queue (Moment 2 ou 3) :
     spawn un Agent par item dans le même tick (Anthropic le supporte).
   - Le subagent écrit ses outputs dans `outputs/<today>/<N>/`, première
     action = `.last-run`, dernière = `round_counter.json[<N>] += 1`.

   **Après le retour de chaque subagent** (je suis responsable de vérifier
   son travail — un employé ne valide pas son propre rendu) :

   a. **Lire `outputs/<today>/<N>/summary.md`** — résumé en quelques lignes
      de ce que le subagent dit avoir fait. Ça me donne le contexte de la
      suite (et je le surface en heartbeat ou en Telegram si pertinent).

   b. **Appeler `validate_layer_output(N, outputs/<today>/<N>/, expected_artifacts)`**
      où `expected_artifacts` est défini par `layers/<N>/PROMPT.md`. Renvoie
      `(ok, missing, malformed)`.

   c. **Si `ok == True`** : noter dans le heartbeat (`subagent N OK`), passer à l'étape 4.

   d. **Si `ok == False`** : je relance le subagent (re-spawn via Agent tool)
      avec un `retry_count` incrémenté + le détail du `missing/malformed` dans
      la task description. Le helper `should_retry(retry_count, max=3)` me dit
      si j'ai droit à un autre essai.

   e. **Si retries épuisés** (`should_retry == False`) : escaladation immédiate
      via Telegram (`MAX_RETRIES_DEFAULT == 3`). Le tick continue quand même
      (pas de blocage du /loop) mais l'incident est loggé dans
      `outputs/<today>/<N>/summary.md` avec préfixe `[ERREUR retry-épuisé]` et
      `outputs/<today>/heartbeat.log` reçoit une ligne `subagent N FAILED`.

4. Si décision = `heartbeat` : `<ISO-ts> tick idle <queues-summary>` >>
   `outputs/<today>/heartbeat.log`.

5. Commit + push via `bubble-git-guard push --action runtime_write_own`
   (sauf si Moment 4 a déjà push lui-même par artifact, voir layers/4/PROMPT.md).

6. Notifier {{OPERATOR}} sur Telegram si une gate a été créée ce tick OU si un
   subagent a échoué après retries épuisés (étape 3e). Le message DOIT
   être **actionnable** :
   - une ligne par décision : *qui / quoi* (ex: « DM Tier 1 pour Jean
     Dupont (Acme) — angle V2 »),
   - **le lien cockpit direct** pour valider en un tap depuis le
     téléphone : `https://:8443/dept/{slug}`
     (ou le lien de la gate précise `…/gate/{slug}/<gate_id>`),
   - plusieurs gates le même tick → UN seul message groupé (N décisions
     + le lien), pas un message par gate.
   Pas de gate créée = pas de message.

**Helpers Python disponibles** (`scripts/lib/dispatch_helpers.py`) :
`decide_dispatch`, `read_last_run`, `write_last_run`, `read_round_counter`,
`increment_round_counter`, `layer_1_gate_satisfied`, `is_mission_due`,
`materialize_due_missions`, `validate_layer_output`, `should_retry`,
`force_commit_and_push`. Détails dans chaque `layers/<N>/PROMPT.md`.

## Quand je suis bloquée

Si j'attends {{OPERATOR}} depuis plus de **2h** sur une décision : relance polie
sur Telegram.

Si plus de **6h** sans réponse : je mets en pause les actions qui
dépendent de cette décision et j'écris une note de statut dans
`MORNING_BRIEF.md` pour que le prochain réveil opérateur trouve un état
propre.

## Références

- Mon mandat narratif (doctrine métier) : `MANDATE.md`
- Mes missions récurrentes : `missions/*.yaml`
- Mes layers actifs : `layers/<N>/PROMPT.md`
- Mes gate policies : `dept.yaml::gate_policies`
- Mon état runtime : `outputs/<today>/heartbeat.log`
- Mon état d'éclosion (archive) : `onboarding/STATE.yaml`
"""


def render_claude_md_operating(dept_yaml: dict) -> str:
    """Render the post-éclosion (operating-mode) CLAUDE.md for a dept.

    Source of truth: dept.yaml. NO per-dept hardcoding — every dept that
    activates gets a CLAUDE.md derived from its own dept.yaml.

    Called by activate_runner.py AFTER flip_status_to_live(), so the
    activation commit includes the rewritten CLAUDE.md + the
    settings.json change that drops the SessionStart auto-drive hook.

    {{OPERATOR}} msg 3060 (2026-05-24): "her Claude.md does need to be rewritten
    after éclosion, but just to remove the éclosion part and go to
    operating mode (same for all agents as well), explaining the setup,
    mandate and layers. And including the parts about non tech user and
    how to behave regarding doc, etc"
    """
    dept = dept_yaml.get("department", {}) or {}
    slug = dept.get("slug", "unknown")
    display_name = dept.get("display_name", slug.capitalize())
    mandate = dept.get(
        "mandate",
        "(Mandat non encore défini — voir `MANDATE.md`.)",
    )
    level = dept.get("level", "ops")
    role_label = "management" if level == "management" else "opérations"

    layers_subscribed = (
        dept_yaml.get("layers", {}).get("subscribed", []) or []
    )
    layers_section = _render_operating_layers_section(layers_subscribed)

    hierarchy = dept_yaml.get("hierarchy", {}) or {}
    children = list(hierarchy.get("children", []) or []) if level == "management" else []
    children_section = _render_operating_children_section(children)

    return CLAUDE_MD_OPERATING_TEMPLATE.format(
        slug=slug,
        slug_compact=slug.replace("-", ""),
        display_name=display_name,
        role_label=role_label,
        mandate=mandate,
        layers_section=layers_section,
        children_section=children_section,
    )


def render_systemd_unit(slug: str) -> str:
    """Render the per-dept systemd unit by substituting placeholders in
    deploy/templates/ops-loop-dept.service.template (Phase G1)."""
    tpl_path = _PROJECT_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"
    text = tpl_path.read_text(encoding="utf-8")
    telegram_state_dir = f"/home/claude/.claude/channels/telegram-{slug}"
    env_file = f"/run/claude-agent-{slug}/env"
    text = text.replace("${DEPT_SLUG}", slug)
    text = text.replace("${TELEGRAM_STATE_DIR}", telegram_state_dir)
    text = text.replace("${ENV_FILE}", env_file)
    return text


STEP_README = {
    "onboarding/1-mandate": (
        "Step 1 - Mandate\n"
        "================\n\n"
        "This directory will hold notes + drafts produced during Step 1 of the "
        "UX onboarding (Notion v5 lines 803-829).\n\n"
        "Step 1 produces TWO artifacts that capture the same decision at\n"
        "two levels of fidelity, and are committed together:\n\n"
        "  1. `MANDATE.md` (repo root) - human-readable narrative (5-10 lines):\n"
        "     who the dept is, what it produces, who it serves, what is out\n"
        "     of scope. Lisible humain, jamais validé par un schéma.\n\n"
        "  2. `dept.yaml.draft` (repo root) - machine-readable YAML. The\n"
        "     `department.mandate` field gets the one-sentence summary; it is\n"
        "     validated against `schemas-draft/dept.schema.yaml`.\n\n"
        "Both are committed via `scripts/validate-step.sh --step=mandate` in\n"
        "a single commit titled `onboarding: validate mandate`.\n"
    ),
    "onboarding/2-missions": (
        "Step 2 - Recurring missions\n"
        "===========================\n\n"
        "Holds notes from Step 2 (Notion v5 lines 830-846). Validated missions "
        "land under `missions/<id>.yaml` and are committed via "
        "`scripts/validate-step.sh --step=missions`.\n"
    ),
    "onboarding/3-layers": (
        "Step 3 - Layer mapping (4 OODA layers)\n"
        "======================================\n\n"
        "Holds the per-layer descriptions captured during Step 3 (Notion v5 "
        "lines 847-862). Layer PROMPT.md stubs land under `layers/<N>/`.\n"
    ),
    "onboarding/4-skills-tools": (
        "Step 4 - Skills & tools\n"
        "=======================\n\n"
        "Holds skill/tool cards drafted during Step 4 (Notion v5 lines "
        "863-893). The manifest is merged into `dept.yaml.draft::skills` + "
        "`dept.yaml.draft::tools`.\n"
    ),
    "onboarding/5-gates-kpis": (
        "Step 5 - Gates, autonomy bands, KPI guardrails\n"
        "==============================================\n\n"
        "Holds the gate policy drafts from Step 5 (Notion v5 lines 894-924). "
        "Validated policies land under `policies/bands/` and "
        "`dept.yaml.draft::gate_policies`.\n"
    ),
    "onboarding/6-dry-run": (
        "Step 6 - Tests / dry-run\n"
        "========================\n\n"
        "Holds the fake-data fixture and round-trip results from Step 6 "
        "(Notion v5 lines 925-946). The dry-run harness lands at "
        "`tests/run.sh` (replaces the bootstrap stub).\n"
    ),
    "onboarding/7-activation": (
        "Step 7 - Activation\n"
        "===================\n\n"
        "Holds the activation PR body draft + final dry-run snapshot. The "
        "PR is opened by `scripts/activate-dept.sh` (Notion v5 lines "
        "961-977).\n"
    ),
}


_DAILY_RISK_AUDIT_MISSION = {
    "id": "daily_risk_audit",
    "layer": 4,
    "cadence": "daily",
    "time": "22:00",   # UTC — dispatch eligibility window per GAP-10 fix
    "description": (
        "Daily Layer-4 self-audit per Notion §'Layer 4 — Risk Control'. "
        "Reads outputs/<date>/{1,2,3}/, writes risk-brief.md, "
        "risk-kpis.yaml, management-export.yaml. "
        "Writes outputs/<date>/4/.last-run as its FIRST action to prevent "
        "double-dispatch within the same 22:00–22:30 UTC window."
    ),
    # output_queue and creates are required by dept.schema.yaml's
    # recurring_missions items sub-schema. Layer-4 missions write to the
    # standard outputs/ path rather than a queue, but the field is required
    # and must satisfy the pattern "^queues/.+/?$". We declare the risk-audit
    # queue slot; Layer 4 skill reads this and writes outputs/, not the queue.
    "output_queue": "queues/gates/",
    "creates": ["risk_audit"],
}


def render_dept_yaml_draft(slug: str, display_name: str, owner: str,
                           level: str = "ops",
                           children: list | None = None) -> str:
    """
    Render dept.yaml.draft via UX-1's template.

    For ops depts (default): standard 4-layer subscribed, no children.
    For management depts: level=management, layers=[1,4], children populated,
    visibility.read_outputs + read_paths set per Notion §1.1, and
    directive_policy.can_open_priority_prs=true.

    Both ops and management depts include the daily_risk_audit recurring
    mission (GAP-10 fix G-1): the Layer-4 self-audit at 22:00 UTC daily.
    This gives the /loop engine a declarative trigger so every dept audits
    itself daily regardless of queue state.

    The template's mandate_text is TBD-by-operator and is filled by Step 1.
    We emit a placeholder that satisfies the schema's minLength=10 but
    flags it for the operator.
    """
    import yaml as _yaml

    children = children or []
    ctx = {
        "slug": slug,
        "display_name": display_name,
        "level": level,
        "mandate_text": (
            "TBD-by-operator at Step 1 (Mandate). This placeholder satisfies "
            "the schema's required field; the operator fills the real mandate "
            "via the UX-1 onboarding skill."
        ),
        "owner": owner,
        "forbidden": [],
    }
    base = render_template("dept.yaml", ctx)

    if level == "management":
        # The Jinja2 template emits minimal placeholders for hierarchy.
        # Post-process the rendered YAML to inject management-specific values.
        # We parse + dump (round-trip via yaml) rather than string-patching to
        # stay safe against whitespace drift in the template.
        # Ambiguity note: the schema keeps read_paths OUT of the
        # hierarchy.visibility object (it's defined in the policy template, not
        # dept.schema.yaml). We add it here anyway for bootstrap convenience;
        # it will be ignored by schema validators that enforce additionalProperties:false
        # on visibility. If a stricter validator is later added, this field can
        # be moved to a separate metadata block. Spec reference: Notion §1.1
        # ("Ce que le CEO lit"), audit report §2.2.
        doc = _yaml.safe_load(base)
        doc["layers"]["subscribed"] = [1, 4]
        # G-1 fix: management depts self-audit daily at 22:00 UTC (GAP-10)
        doc["recurring_missions"] = [dict(_DAILY_RISK_AUDIT_MISSION)]
        doc["hierarchy"]["level"] = "management"
        doc["hierarchy"]["children"] = list(children)
        doc["hierarchy"]["visibility"]["read_outputs"] = list(children)
        doc["hierarchy"]["visibility"]["read_risk_kpis"] = True
        doc["hierarchy"]["visibility"]["read_risk_briefs"] = True
        doc["hierarchy"]["visibility"]["read_raw_artifacts"] = False
        # read_paths: per Notion §1.1 + management-policy.template.yaml
        doc["hierarchy"]["visibility"]["read_paths"] = list(MANAGEMENT_READ_PATHS)
        doc["hierarchy"]["directive_policy"]["can_open_priority_prs"] = True
        doc["hierarchy"]["directive_policy"]["target_queue"] = "queues/management/"
        doc["hierarchy"]["directive_policy"]["requires_human_gate_for"] = [
            "mandate_change",
            "capital_allocation",
            "live_execution",
        ]
        header = (
            f"# {display_name} — dept.yaml (onboarding draft)\n"
            f"# Level: management — Generated by scripts/lib/scaffold.py\n"
            f"# Notion §1.1: management depts subscribe to [1, 4] and read\n"
            f"# only Layer-4 bubble-up artifacts from their children.\n"
        )
        return header + _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)

    # Ops depts: post-process to inject the daily_risk_audit mission alongside
    # whatever missions the Jinja2 template already emits (currently just []).
    # GAP-10 fix G-1: every ops dept must self-audit at 22:00 UTC daily.
    doc = _yaml.safe_load(base)
    existing_missions = doc.get("recurring_missions") or []
    if not any(m.get("id") == "daily_risk_audit" for m in existing_missions):
        existing_missions.append(dict(_DAILY_RISK_AUDIT_MISSION))
    doc["recurring_missions"] = existing_missions
    return _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def render_readme(slug: str, display_name: str) -> str:
    return (
        f"# {display_name} - bubble-ops-{slug}\n\n"
        f"Status: **onboarding**.\n\n"
        f"This department is currently being onboarded via the UX-1 skill "
        f"`department-onboarding-guide`. See branch "
        f"[`onboarding/{slug}`](https://github.com/vdk888/bubble-ops-{slug}/tree/onboarding/{slug}) "
        f"for progress.\n\n"
        f"At activation the branch will be merged into `main` via a PR titled "
        f"`Activate {display_name} department` (per Notion v5 line 975).\n"
    )


def write_with_dirs(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        st = os.stat(path)
        path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_settings(slug: str, level: str = "ops") -> dict:
    """Build the .claude/settings.json dict.

    Management depts get an extra allow entry for the git-guard priority-PR
    command (Notion §1.3 "open_priority_pr" action class).
    """
    settings = json.loads(json.dumps(CLAUDE_SETTINGS_MINIMAL))  # deep copy
    if level == "management":
        settings["permissions"]["allow"].append(
            _PRIORITY_PR_PERM.format(slug=slug)
        )
    return settings


def scaffold(root: Path, slug: str, display_name: str, owner: str,
             level: str = "ops",
             children: list | None = None) -> None:
    """Materialize the full onboarding skeleton under `root`.

    Args:
        root:         Path to the (already-created) target directory.
        slug:         Dept slug (kebab-case).
        display_name: Human-readable display name.
        owner:        Slug of the human operator.
        level:        "ops" (default) or "management". Controls the dept.yaml
                      template branch, CLAUDE.md template, and settings.json
                      allow-list.
        children:     List of child dept slugs. Required when level="management"
                      (must be non-empty). Must be empty when level="ops".
    """
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"target dir does not exist: {root}")

    # Validate level/children combination.
    children = list(children) if children else []
    if level == "management" and not children:
        raise ValueError(
            f"scaffold: level='management' requires at least one child slug "
            f"(--children). Got empty list."
        )
    if level != "management" and children:
        raise ValueError(
            f"scaffold: --children is only valid with --level=management. "
            f"Got level={level!r} with children={children!r}. "
            f"Pass --level=management or remove --children."
        )

    # 1. README.md
    write_with_dirs(root / "README.md", render_readme(slug, display_name))

    # 2. .gitignore
    write_with_dirs(root / ".gitignore", GITIGNORE_CONTENT)

    # 3. dept.yaml.draft (rendered via UX-1 template, branching on level).
    write_with_dirs(
        root / "dept.yaml.draft",
        render_dept_yaml_draft(slug, display_name, owner, level=level, children=children),
    )

    # 4. onboarding/STATE.yaml (initialized at status=Idea).
    state_yaml.init_state(
        path=root / "onboarding" / "STATE.yaml",
        slug=slug,
        display_name=display_name,
        owner=owner,
    )

    # 5. onboarding/<N-step>/README.md
    for d in ONBOARDING_STEP_DIRS:
        write_with_dirs(root / d / "README.md", STEP_README[d])

    # 6. .gitkeep dirs.
    for d in GITKEEP_DIRS:
        write_with_dirs(root / d / ".gitkeep", "")

    # 7. Canonical layer PROMPT.md ({{OPERATOR}} 2026-06-01: layers are templated,
    #    not empty; L4 includes the Notion logbook step).
    for _n in (1, 2, 3, 4):
        write_with_dirs(
            root / "layers" / str(_n) / "PROMPT.md",
            render_layer_prompt(_n, slug, display_name),
        )

    # 7. tests/run.sh stub (executable).
    write_with_dirs(root / "tests" / "run.sh", TESTS_RUN_SH, executable=True)

    # 8. .claude/settings.json (minimal; extended for management depts).
    write_with_dirs(
        root / ".claude" / "settings.json",
        json.dumps(_build_settings(slug, level=level), indent=2) + "\n",
    )

    # 9. CLAUDE.md — auto-driving prompt for the eclosing agent (Phase G1).
    # Management depts get a different CLAUDE.md (aggregation role, not 7-step eclosure).
    write_with_dirs(root / "CLAUDE.md", render_claude_md(slug, display_name,
                                                         level=level, children=children))

    # 10. deploy/ops-loop-<slug>.service — pre-rendered systemd unit (Phase G1).
    write_with_dirs(
        root / "deploy" / f"ops-loop-{slug}.service",
        render_systemd_unit(slug),
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Scaffold a bubble-ops-<slug> onboarding repo.")
    p.add_argument("--slug", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--target", required=True, help="Path to the (already-init'd) git clone.")
    p.add_argument(
        "--level",
        choices=["ops", "management"],
        default="ops",
        help="Department level: 'ops' (default leaf dept) or 'management' (aggregator).",
    )
    p.add_argument(
        "--children",
        default="",
        help=(
            "Comma-separated list of child dept slugs. Required when --level=management. "
            "Example: --children=ben,maya,miranda,eliot"
        ),
    )
    args = p.parse_args()
    children = [c.strip() for c in args.children.split(",") if c.strip()] if args.children else []
    try:
        scaffold(Path(args.target), args.slug, args.display_name, args.owner,
                 level=args.level, children=children)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
