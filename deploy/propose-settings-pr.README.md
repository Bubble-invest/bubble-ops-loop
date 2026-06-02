# `propose-settings-pr` — WS5 deliverable (install + doc snippets)

Root-owned helper that lets a dept agent open a JUSTIFIED structural-change PR on
its OWN repo while the in-session mission-guard stays fully fail-closed.

- Helper source (this repo): `deploy/bin/propose-settings-pr`
- Tests: `tests/propose-settings-pr/test_propose_settings_pr.sh` (13/13 green)
- Install target: `/usr/local/bin/propose-settings-pr` (ROOT, mode 0755)

## Install (Rick-only, root)

```bash
# from the VPS framework repo, as root:
install -m 0755 -o root -g root \
  /home/claude/bubble-ops-loop/deploy/bin/propose-settings-pr \
  /usr/local/bin/propose-settings-pr
```

Why root-owned: the mission-guard (`/opt/bubble-mission-guard/mission-file-guard.py`)
only inspects `Edit`/`Write`/`NotebookEdit` and `git add|commit|mv|rm` **tool
calls**. A Bash call of `propose-settings-pr ...` has `argv[0] != "git"`, so the
guard's `_bash_targets()` returns `[]` and never fires. The structural write +
commit happen INSIDE this opaque root script — the agent never Edits the
structural file directly, so the guard remains fully fail-closed.

## `bootstrap-dept.sh` vendoring note (for new depts)

`bootstrap-dept.sh` already provisions a per-dept broker policy. Two additions
make a new dept self-improvement-ready at scaffold:

1. **Ensure `/usr/local/bin/propose-settings-pr` exists** (install once,
   fleet-wide — it is dept-agnostic; it derives the dept slug+repo from the
   working tree's `origin` remote, exactly like `resolve_push_target`). The
   bootstrap can assert its presence and emit a one-line install reminder if
   missing.
2. **Ensure `/opt/bubble-token-broker/deploy/policies/<slug>-policy.yaml`
   exists** with `settings_pr` resolvable for `own_repo` (the broker's
   `same_own` branch — present by default for any policy whose `own_repo ==
   bubble-ops-<slug>`). maya/tony/cgp policies already exist; new depts get one
   generated from the fixture template.

The helper itself needs NO per-dept customization — one binary serves all depts.

## CLAUDE.md doc snippet (DRAFT — Rick PRs this into each dept CLAUDE.md)

> ### Changing your own mission files (the sanctioned path)
>
> Your mission-definition files — `CLAUDE.md`, `MANDATE.md`, `dept.yaml`,
> `layers/**`, `skills/**`, `tools/**`, `.claude/settings.json`, etc. — are
> **frozen spec**. You cannot Edit, Write, or `git add`/`commit` them yourself;
> the mission-guard will deny it, and a direct push gets a read-only token (403).
> This is by design: your mission changes only via a PR that Joris or Jade merges.
>
> When you have a real, justified structural change (even one Joris approved in
> chat), DO NOT try to self-edit. Open a settings PR with the root helper:
>
> ```bash
> propose-settings-pr \
>   --repo-dir "$(pwd)" \
>   --paths layers/1/PROMPT.md \
>   --topic remove-stale-ipo-watch \
>   --justification "Why this change is needed (goes into the PR + audit trail)." \
>   --content-from /tmp/new-prompt.md     # or: --patch /tmp/change.diff
> ```
>
> The helper REFUSES anything that isn't your own repo, isn't structural, lacks a
> justification, or targets a branch other than `main`. On success it prints
> `PR_URL=<url>`; a human reviews and merges. Never declare a mission change
> "official" until the PR is merged.

## Sandbox caveat (UNPROVEN step — flag)

The helper runs inside the bwrap userns jail. `bubble-git-guard push` survives
the jail via `allowUnsandboxedCommands: true` (PROVEN — maya canary `fa5acb3`).
`bubble-gh pr create` is the SAME `sudo -n bubble-gh-credential-helper.sh` path
but is **NOT yet proven** under the sandbox. Live pr-create smoke is the one
unverified step. Fallback if it fails: add `"/usr/local/bin/bubble-gh *"` to
`sandbox.excludedCommands` in `/etc/claude-code/managed-settings.json` (creating
the key — live has none). That is a Rick-only root edit; do NOT make it
speculatively.
