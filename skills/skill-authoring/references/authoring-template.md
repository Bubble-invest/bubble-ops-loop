# Authoring template

Locally bundled adaptation of [Anthropic skill-creator, Skill Writing Guide](https://github.com/anthropics/skills/blob/b0cbd3df1533b396d281a6886d5132f623393a9c/skills/skill-creator/SKILL.md#skill-writing-guide),
reviewed 2026-09-28. This carries its authoring structure, not its separate
benchmark runner. Our A3.1/A4/A5 gates remain authoritative.

```markdown
---
name: <short-kebab-name>
description: >-
  <Capability, concrete triggering situations, and relevant scope boundary.>
---

# <Skill title>

## Procedure
<Essential steps, required inputs, and checks that change the outcome.>

## Result
<Deliverable and a representative input/output example.>

## Supporting resources
<Link only files that exist; explain when to consult each.>
```

Before staging:
- Put selection cues in the description; they must work before the body loads.
- Keep the procedure concise. Move conditional detail into linked references.
- Use `scripts/` for reusable executable helpers, `references/` for on-demand
  guidance, and `assets/` for material copied into deliverables. Omit unused
  directories; do not invent helpers or dependencies.
- Replace placeholders; check YAML frontmatter and every relative link.
- Record candidate-specific description checks alongside the staged draft,
  then run the existing task eval. An EXTEND proposal includes the current
  skill's diff and still requires human review.
