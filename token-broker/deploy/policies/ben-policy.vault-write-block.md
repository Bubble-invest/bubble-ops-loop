# Ben policy — add `bubble-ben-vault` write access (#benvault)

**Deploy step (root, on the VPS) — apply AFTER this PR merges + the broker code
redeploys.** The live per-dept policy lives at
`/opt/bubble-token-broker/deploy/policies/ben-policy.yaml` (root-owned, not in
this repo). This PR fixes the *code* guard (policy.py now permits a repo that has
an explicit write-rule, not only `own_repo`); the policy below grants Ben's vault
repo those rules.

## Why
When `vault/` was split into its own repo `Bubble-invest/bubble-ben-vault`
(2026-06-03), Ben's push-policy was never extended to it. Result: every guarded
vault push was DENIED (`allowed_paths []`), silently — Ben's research commits
piled up local (3 unpushed as of 2026-07-26). Tony surfaced it as "Ben can't
push his vault." Confirmed in the guard audit log:
`repo 'bubble-ben-vault' is not the actor's own_repo ... no write rules declared`.

## Apply (root)
Add `bubble-ben-vault` to `read:` and add this second entry under `write:` in
`/opt/bubble-token-broker/deploy/policies/ben-policy.yaml`:

```yaml
  read:
    - bubble-ops-ben
    - bubble-ben-vault      # ADD — vault split out 2026-06-03 (#benvault)
    - bubble-shared-wiki

  write:
    - repo: bubble-ops-ben
      allowed_paths: [ ...unchanged... ]
      mode: direct_runtime_commit
    - repo: bubble-ben-vault          # ADD (#benvault)
      allowed_paths:
        - value-chains/**
        - themes/**
        - investment-cases/**
        - clusters/**
        - positions/**
        - asset_classes/**
        - research-archive/**
        - "*.md"                      # hot.md, index.md, README.md, vault-README.md
      mode: direct_runtime_commit
```

Paths derived from `git ls-files` on the live vault (the actual content dirs Ben
writes). Non-structural content only — no secrets/structural paths (those still
require settings_pr). After editing: no restart needed (the guard reads the
policy file per-invocation), but verify with a real `bubble-git-guard push
--dept ben --repo bubble-ben-vault ... --dry-run` and then Ben's next L2/L4 push.

## Prereq
The GitHub App installation for the `ben` dept must include `bubble-ben-vault`
(the broker mints an installation token scoped to the repo — cli.py
`repositories=[repo]`). Verify the App is installed on that repo; if not, that
install is a separate one-time root/GitHub-admin step.
