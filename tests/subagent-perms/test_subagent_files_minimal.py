"""
test_subagent_files_minimal.py — body-content sanity checks.

Per-subagent body assertions that complement the frontmatter-level
tests. The body is where path policies live (since tool-level perms
are binary) and where personas are reinforced for runtime readers.

Source: /tmp/notion_final.txt §"Les 4 layers" L438-L466. Specific
verbatim phrases enforced below cite their Notion source line.
"""

from __future__ import annotations


def test_mandate_guardian_body_states_pure_auditor(subagent_file):
    """L466 verbatim: 'Pure auditeur, jamais exécuteur'.

    Body must restate this (English 'Pure auditor' or French
    'Pure auditeur' both accepted, case-insensitive).
    """
    body = subagent_file("mandate-guardian")["body"].lower()
    assert "pure auditor" in body or "pure auditeur" in body, (
        "mandate-guardian body missing 'Pure auditor'/'Pure auditeur' "
        "(Notion L466 verbatim)."
    )


def test_mandate_guardian_body_lists_three_hierarchy_outputs(subagent_file):
    """L465: guardian writes 3 distinct hierarchy outputs.

    Per Notion §Layer 4 line 465, the guardian's outputs are
    risk-brief.md + risk-kpis.yaml + management-export.yaml. The body
    must enumerate all three so the persona is unambiguous.
    """
    body = subagent_file("mandate-guardian")["body"]
    for needle in ("risk-brief.md", "risk-kpis.yaml", "management-export.yaml"):
        assert needle in body, (
            f"mandate-guardian body missing {needle!r} "
            f"(Notion L465 lists this as a required output)."
        )


def test_executor_body_disclaims_web(subagent_file):
    """L458 + Step-5 doctrine: executor has no web access.

    Body must explicitly tell readers this (e.g. 'No web access' or
    'NEVER browse the web'). We accept any phrasing that contains the
    substring 'no web' OR 'never browse' (case-insensitive).
    """
    body = subagent_file("executor")["body"].lower()
    has_disclaimer = "no web" in body or "never browse" in body or "no web access" in body
    assert has_disclaimer, (
        "executor body missing explicit web-access disclaimer; "
        "should restate the 'pure executor — no web' persona."
    )


def test_data_curator_body_explains_dual_enforcement(subagent_file):
    """L445 + Step-5 deviation: path policy is body+git-guard, not tool-level.

    The body must explain WHY Write is in tools despite Notion saying
    'zéro Write hors de outputs/<date>/1/' — namely that Claude Code's
    tool perms are binary and the path scope is enforced elsewhere.
    Body must mention either 'prompt' or 'path policy' AND 'git guard'
    OR 'git-guard'.
    """
    body = subagent_file("data-curator")["body"].lower()
    has_policy_mention = "path policy" in body or "prompt" in body
    has_guard_mention = "git guard" in body or "git-guard" in body
    assert has_policy_mention and has_guard_mention, (
        "data-curator body missing dual-enforcement explanation; "
        "should cite both prompt-level policy AND git-guard enforcement."
    )


def test_task_orchestrator_body_describes_spawn_rule(subagent_file):
    """L451: orchestrator 'peut spawn N sub-task-subagents en parallèle'.

    Body must mention sub-agent spawning rules so runtime readers know
    this is the dispatch persona. We accept 'spawn' substring
    (case-insensitive) — that's the operative verb.
    """
    body = subagent_file("task-orchestrator")["body"].lower()
    assert "spawn" in body, (
        "task-orchestrator body missing 'spawn' verb; "
        "L451 makes spawning the orchestrator's defining capability."
    )


def test_all_subagents_have_nonempty_body(subagent_file):
    """Every subagent file has a non-trivial body (more than just frontmatter).

    Empty bodies would mean the file is just a frontmatter stub —
    fine for parsers but useless for the runtime reader. Step-5
    guarantees each file has a "## Tool / permission rationale"
    section; we assert the body is at least 200 chars.
    """
    for name in ("data-curator", "task-orchestrator", "executor", "mandate-guardian"):
        body = subagent_file(name)["body"]
        assert len(body.strip()) >= 200, (
            f"{name} body too short ({len(body.strip())} chars); "
            f"every subagent file should explain its persona + path policy."
        )


def test_every_subagent_cites_layer_prompt(subagent_file):
    """Each subagent body cites its layers/N/PROMPT.md (per Step-5 convention).

    Step-5 set up each subagent body to reference `layers/<N>/PROMPT.md`
    as the source-of-truth for mission and process. This connects the
    subagent file (perm scope) to the layer file (behavior).
    """
    layer_map = {
        "data-curator": "layers/1/PROMPT.md",
        "task-orchestrator": "layers/2/PROMPT.md",
        "executor": "layers/3/PROMPT.md",
        "mandate-guardian": "layers/4/PROMPT.md",
    }
    for name, prompt_path in layer_map.items():
        body = subagent_file(name)["body"]
        assert prompt_path in body, (
            f"{name} body missing reference to {prompt_path}; "
            f"every subagent should cite its layer prompt for behavior contract."
        )
