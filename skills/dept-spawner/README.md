# dept-spawner

Operator-facing skill that orchestrates spawning a new Bubble ops department end-to-end,
wrapping the existing framework tooling (`scripts/bootstrap-dept.sh`, `scripts/lib/scaffold.py`,
`scripts/deploy-to-morty.sh`, `scripts/activate-dept.sh`) and the self-driving
`skills/department-onboarding-guide`. It is a thin orchestrator + the human-step checklist —
it does not reinvent the spawn process.

See `SKILL.md` for the workflow and `references/` for the per-phase detail. The full
as-practiced record (the Ben + Geraldine spawns) lives in the R&D workspace at
`Rick_RnD/projects/dept-spawner/EXISTING-PROCESS.md`.

Status: v1, promoted from the R&D dev workspace 2026-06-20 (board #21).
