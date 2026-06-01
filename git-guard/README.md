# bubble-git-guard

Path-allow-list enforcement at the **`git push` boundary** on Morty.
Step 3c of the bubble-ops-loop MVP.

## Why this exists (Notion v4 line 725, verbatim)

> "GitHub ne fournit pas un vrai path-scope au niveau token `contents:write`.
> Les paths autorisés sont donc appliqués par wrapper local / git guard sur
> Morty, CI path guard, branch protection et audit Layer 4. Les tokens
> limitent les repos et permissions ; les guards limitent les chemins."

GitHub's `contents:write` permission is **repo-wide**: a token that can write
ANY file in `bubble-ops-fixture` can write `dept.yaml`, `MANDATE.md`,
`.claude/settings.json`, or any other governance file. The token-broker (Step
3b) handles the **repo + permission class** dimension; this guard handles the
**path** dimension.

## Architecture (one slide)

```
ops-loop-fixture (the /loop agent)
       |
       v
  bubble-git-guard push --dept --action --repo --policy
       |
       |-- 1. read git diff --cached + git diff @{upstream}..HEAD
       |
       |-- 2. policy.enforce(each_path)   <-- reuses token-broker's Policy class
       |       any deny? ---> audit:denied + exit 1, NO broker call, NO push
       |
       |-- 3. subprocess: bubble-token-broker mint --paths ...
       |       broker exits non-zero ---> audit:mint_failed + exit 1, NO push
       |
       |-- 4. git -c http.extraheader='AUTHORIZATION: bearer <token>' push
       |       (token captured into local var; GITHUB_TOKEN env stripped)
       |
       |-- 5. audit:pushed / audit:push_failed
       v
  exit 0 (success) | exit 1 (any fail, fail-CLOSED)
```

## Install

See `deploy/INSTALL-ON-MORTY.md` for the operator runbook. TL;DR:

```bash
# On Morty (assumes token-broker already at /opt/bubble-token-broker/)
sudo tar xzf /tmp/bubble-git-guard.tar.gz -C /opt/bubble-git-guard --strip-components=1
sudo install -m 0755 /opt/bubble-git-guard/deploy/bubble-git-guard.template.sh \
    /opt/bubble-git-guard/bin/bubble-git-guard
sudo ln -sf /opt/bubble-git-guard/bin/bubble-git-guard /usr/local/bin/bubble-git-guard
```

## Usage

```bash
# Runtime push (the loop's normal path)
bubble-git-guard push \
    --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --broker /opt/bubble-token-broker/bin/bubble-token-broker \
    --audit-log /var/log/bubble-git-guard/audit.jsonl

# Dry-run (offline, no broker, no network)
bubble-git-guard push --dept fixture --action runtime_write_own \
    --repo bubble-ops-fixture \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --dry-run

# Tony opens a priority PR to a child dept
bubble-git-guard push --dept tony --action open_priority_pr \
    --repo bubble-ops-fixture \
    --policy /opt/bubble-token-broker/deploy/policies/tony-policy.yaml \
    --ref tony/directive/buy-aapl
```

## Path policy (canonical, from Notion v4 line 620 + 700)

### `runtime_write_own` — direct commit/push allowed for:
- `outputs/**`
- `queues/**` (incl. `queues/management/**` for the dept itself)
- `inbox/**`

### Structural — DENY for `runtime_write_own`, ALLOW for `settings_pr`:
- `dept.yaml`, `MANDATE.md`, `CLAUDE.md`
- `layers/**`, `subagents/**`, `skills/**`, `tools/**`
- `templates/**`, `policies/**`
- `.claude/**`

### `open_priority_pr` (e.g. Tony → child dept):
- target `queues/management/**` ONLY
- repo MUST be in policy `pull_requests.can_open_to`

## Threat model

| Threat | Mitigation |
|--------|-----------|
| **Path exfiltration** — stage 9 allowed + 1 structural file | Atomicity: any single deny fails the whole batch. No partial push. |
| **Token leak via audit** | `FORBIDDEN_FIELDS` drops `token`/`access_token`/`pem`/`private_key`/`jwt`/`secret`. Any value starting with `ghs_` raises `ValueError` before write. |
| **Token leak via stderr** | Token captured into LOCAL var, never `print()`ed. `git push` stderr is redacted (token replaced with `<TOKEN-REDACTED>`) before being surfaced. |
| **Fallback to PAT / env GITHUB_TOKEN** | `GITHUB_TOKEN` is stripped from the env passed to `git push`. No code path reads it. |
| **Bypass: call `git push` directly** | The wrapper script is the only sanctioned push call site; broker `Policy.enforce` runs server-side too; branch protection on `main` is the third layer. |
| **Broker exits 0 but returns garbage** | Guard checks `_token.startswith("ghs_")` before invoking git. Otherwise → `mint_failed`. |
| **Broker missing** | `FileNotFoundError` → `mint_failed`, no push, exit 1. |
| **Network failure on push** | Captured exit code + redacted stderr → `push_failed` in audit. |
| **Malformed policy YAML** | YAML parse error caught at CLI, fail-CLOSED with clear error message. |
| **Unknown action class** | Rejected before broker call, fail-CLOSED. |

## Fail-closed everywhere

| Situation | Behavior |
|-----------|----------|
| Policy file missing | exit 1, no broker call |
| Policy YAML malformed | exit 1, no broker call |
| Action class unknown | exit 1, no broker call |
| Any path denied | exit 1, no broker call |
| Empty path set (nothing staged) | exit 1, no broker call (audit:denied with reason="empty path set") |
| Broker binary not in PATH | exit 1, no push |
| Broker exits non-zero | exit 1, no push |
| Broker stdout doesn't start with `ghs_` | exit 1, no push |
| `git push` exits non-zero | exit 1, audit:push_failed |

## Atomicity

If the staged set is `[outputs/x.md, MANDATE.md, queues/y.yaml]`:
- 2 paths individually pass policy
- 1 path (`MANDATE.md`) is denied
- **The entire push is denied.** No "push the 2, ignore the 1" behavior.

This prevents an attacker from interleaving an exfiltration file inside an
otherwise-legitimate push.

## Tests

```bash
cd git-guard
python3 -m pytest tests/ -v                 # 67/67 passing
python3 -m pytest --cov=src tests/          # 90% coverage
```

13 test files cover:
- Staging detection (`git diff --cached` + `git diff @{upstream}..HEAD`)
- Allow paths: `outputs/`, `queues/`, `inbox/` for `runtime_write_own`
- Deny paths: `dept.yaml`, `MANDATE.md`, `CLAUDE.md`, `layers/`, `subagents/`, `skills/`, `tools/`, `.claude/`
- Settings-PR class: same structural paths ALLOWED under `settings_pr`
- Atomicity: 1 deny among many → all denied
- Broker invocation: NOT called when paths denied
- Git push invocation: NOT called when broker fails
- Audit schema: allow/deny lines have right fields
- Audit no-leak: token value never appears in JSONL
- CLI dry-run: full plan shown without side effects
- Failure modes: missing policy, missing broker, network error, malformed YAML, unknown action

## What this does NOT do

- **It does NOT validate file CONTENTS** — only file PATHS. A pre-commit hook
  for secret-scanning is a separate concern (Layer 4 audit covers it).
- **It does NOT enforce branch protection** — that's GitHub-side config.
  See Notion v4 line 725 for the four layers; this guard is layer 1.
- **It does NOT prevent direct `git push`** — that's the deploy wrapper's job.
  The `loop-autostart.sh` calls `bubble-git-guard push`, not `git push`.

## Files

```
git-guard/
├── README.md                          # this file
├── requirements.txt                   # pyyaml + pytest
├── src/
│   ├── __init__.py
│   ├── audit.py                       # JSONL audit, FORBIDDEN_FIELDS + ghs_ raise
│   ├── cli.py                         # argparse, push subcommand
│   ├── guard.py                       # Guard class: check_paths + push pipeline
│   ├── policy_loader.py               # imports token-broker's Policy via spec_from_file_location
│   └── staging.py                     # git diff --cached + git diff @{upstream}..HEAD
├── tests/                             # 13 files, 67 tests, 90% coverage
└── deploy/
    ├── bubble-git-guard.template.sh   # wrapper installed to /opt/bubble-git-guard/bin/
    └── INSTALL-ON-MORTY.md            # full operator runbook
```

## See also

- `token-broker/` — Step 3b: GitHub App installation-token broker (the "tokens limitent les repos et permissions" half of Notion line 725)
- `MVP-ROADMAP.md` §3 — full step-by-step roadmap
- Notion v4 §"GitHub access model" — canonical doctrine
