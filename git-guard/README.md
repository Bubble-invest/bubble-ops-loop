# bubble-git-guard

Path-allow-list enforcement at the **`git push` boundary** on the VPS.
Step 3c of the bubble-ops-loop MVP.

## Why this exists (Notion v4 line 725, verbatim)

> "GitHub ne fournit pas un vrai path-scope au niveau token `contents:write`.
> Les paths autorisés sont donc appliqués par wrapper local / git guard sur
> le VPS, CI path guard, branch protection et audit Layer 4. Les tokens
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
       |-- 1. read git diff --cached + git diff <remote>/<destination>..<source>
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
# On the VPS (assumes token-broker already at /opt/bubble-token-broker/)
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

## Push changeset (#1413, hardened by #543)

The guard checks staged paths plus the diff from the push destination's
**real remote state** to the push source. For example, `--remote origin
--ref HEAD:main` checks the destination `main` branch on `origin`; the
default `--ref HEAD` uses the current local branch name as the destination.
A branch name or a single `source:destination` branch refspec is supported.
Unsupported refspecs fail before token minting.

**The destination's state is asked from the remote over the network on every
call — never read from a local ref.** Concretely: `git ls-remote <remote>
refs/heads/<destination>` gets the authoritative current SHA, then `git
fetch` pulls that exact object into a guard-private ref (`refs/git-guard/base`,
never `refs/remotes/*`) and re-verifies the fetched object matches before
it is diffed against. `git diff A..B` compares trees, not ancestry, so this
is correct across a force-push too. If `ls-remote` positively confirms the
destination doesn't exist on the remote (a genuinely new branch), the
guard checks every path in the source commit's complete tree with `git
ls-tree -r --name-only`. It does not walk history: a normal `git log
--name-only` suppresses merge-commit diffs and can miss a path introduced
only in the merge result. Any `ls-remote`/`fetch` transport or auth error is
fail-closed: no path check, no broker call, no push.

The source ref is resolved to one immutable commit SHA before diffing and
policy evaluation. The final command pushes exactly
`<checked-sha>:refs/heads/<destination>`—never `HEAD` or another symbolic
source. It also carries
`--force-with-lease=refs/heads/<destination>:<ls-remote-sha>` (an empty
expected SHA for a branch verified absent), so a destination change between
the initial `ls-remote` and the push fails closed rather than silently
updating a different remote state.

The checkout's upstream, `origin/HEAD`, `BUBBLE_GUARD_DIFF_BASE`, and any
local `refs/remotes/<remote>/<destination>` value **do not** select this
base and are never read for this decision. This closes a critical bypass
(card #1413, PR #543 independent review): the guarded actor controls its
own checkout, so a forged `git update-ref refs/remotes/<remote>/<destination>
<anything>` — no push, no network, no broker call required — used to be
able to make `staged_paths_for_push()` return `[]` (or omit a path) while a
forbidden path still rode along in the real `git push`. See
`tests/test_1413_push_target.py::test_forged_refs_remotes_cannot_hide_a_structural_path`
for the regression test that proves this specific attack is now blocked.
This also still works when the sandbox makes `.git/config` read-only: no
upstream configuration change is required, and the guard's own fetch writes
only to `refs/git-guard/base`, never to `.git/config`.

Every Git subprocess is invoked with `-c core.hooksPath=/dev/null`. The final
push additionally uses `--no-verify`, so neither the checkout's default
`.git/hooks/pre-push` nor an attacker-controlled local `core.hooksPath` can
read the short-lived authorization header from the push environment. The
global hook override also neutralizes hooks such as `reference-transaction`
when the guard fetches its verified base. Repository-routing variables
(`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, object/alternate/common-dir
overrides, and `GIT_NAMESPACE`) are removed from every Git subprocess. The
guard verifies that `git rev-parse --absolute-git-dir` resolves inside the
requested `--repo-dir` both during inspection and immediately before push;
an ancestor or redirected repository fails closed.

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

These apply in **every** `bubble-ops-*` repo (`STRUCTURAL_PATH_GLOBS` in
`token-broker/src/policy.py`).

### Framework-repo-only structural — DENY for `runtime_write_own`, ALLOW for
`settings_pr`, **only when the target repo is `bubble-ops-loop` itself**
(`FRAMEWORK_STRUCTURAL_PATH_GLOBS`, `is_structural_for_repo()`):
- `scripts/**` (broadened #961; was `scripts/lib/**` + two named files only)
- `.github/**`, `token-broker/**`, `git-guard/**`

**#961 (2026-09-03)**: this list existed since governance fix 2026-06-09
(`#55`, commit `ce90bb2` — a direct reaction to commit `f3213b7` reverting
`scaffold.py` unreviewed) but `Policy.enforce()`'s `runtime_write_own` and
`settings_pr` branches never actually called `is_structural_for_repo()` —
only the repo-agnostic `_is_structural()` above. That gap let commit
`5513824` push `scripts/lib/budget.py` straight to `bubble-ops-loop/main`
with no PR/review, the same class of incident `#55` was meant to close.
`#961` wires `is_structural_for_repo()` into both enforcement branches, adds
the matching pre-commit-time check to `deploy/hooks/mission-file-guard.py`
(same repo-name derivation as `deploy/is-structural-push.py`), broadens the
framework glob from `scripts/lib/**`-only to `scripts/**`, and adds
`.github/CODEOWNERS` to `bubble-ops-loop` for the first time (it did not
exist on the canonical repo before this PR — only on the separate
`vdk888/bubble-ops-fixture` fixture repo `tests/test_branch_protection.py`
targets). Any change under a framework-only path — including a *contribution
to this doctrine section itself* — requires a PR: use `action=settings_pr`,
never `runtime_write_own`.

### `open_priority_pr` (e.g. Tony → child dept):
- target `queues/management/**` ONLY
- repo MUST be in policy `pull_requests.can_open_to`

## Threat model

| Threat | Mitigation |
|--------|-----------|
| **Path exfiltration** — stage 9 allowed + 1 structural file | Atomicity: any single deny fails the whole batch. No partial push. |
| **Forged `refs/remotes/<remote>/<destination>`** — the guarded actor's own checkout makes the destination look falsely "ahead" so a forbidden path is omitted from the checked set (card #1413/#543) | The diff base is never read from `refs/remotes/*`. It is fetched from the real remote (`ls-remote` + verified `fetch` into `refs/git-guard/base`) on every call. `ls-remote`/`fetch` failure is fail-closed. |
| **Source-ref TOCTOU** — the actor advances `HEAD` after path checks but before the broker returns | Resolve the source once, check that immutable SHA, and push exactly the same `<sha>:refs/heads/<destination>` refspec. Symbolic sources never reach `git push`. |
| **Destination-ref TOCTOU** — another writer changes or creates the remote branch after `ls-remote` | The push uses an explicit `--force-with-lease` expectation bound to the SHA (or verified absence) returned by that same `ls-remote`; mismatch fails closed. |
| **Merge-only path on a new branch** — `git log --name-only` hides a path added only in a merge result | New branches are checked by full-tree enumeration (`git ls-tree -r --name-only <checked-sha>`), so every path that would land is policy-checked. Existing branches use a base-tree-to-source-tree diff, which also includes merge results. |
| **Minted-token theft by a client-side Git hook** — `.git/hooks/pre-push` or local `core.hooksPath` reads `GIT_CONFIG_VALUE_0` from the push environment | Every Git command uses `-c core.hooksPath=/dev/null`; the final push also uses `--no-verify`. Real-subprocess regressions cover both exploit paths. |
| **Repository redirection through inherited Git environment** — `GIT_DIR`, work-tree/index/object/alternate/common-dir, or namespace variables select a different view | All repository-routing variables are scrubbed, and the resolved absolute Git directory must remain under the requested repo directory (rechecked just before push). |
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
| `ls-remote`/`fetch` against the real remote errors (auth/network/host) | exit 1, no path check, no broker call, no push |
| Fetched object doesn't match the SHA `ls-remote` reported | exit 1, no path check, no broker call, no push |
| Destination no longer matches the SHA (or absence) `ls-remote` reported | leased push rejected, exit 1, audit:push_failed |

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
python3 -m pytest tests/ -v                 # 142/142 passing
python3 -m pytest --cov=src tests/          # 90% coverage
```

21 test files cover:
- Staging detection (`git diff --cached` + `git diff <remote>/<destination>..<source>`)
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
  **As of #961 (2026-09-03), `main` on `Bubble-invest/bubble-ops-loop` has
  NO branch protection at all** (`gh api repos/.../branches/main/protection`
  -> 404) and `.github/CODEOWNERS` did not exist before this PR — so this
  layer is currently the ONLY layer, for anyone who bypasses the guard/
  credential-helper entirely (e.g. a human with a personal PAT/SSH key
  pushing straight to `main` — the class of push this guard cannot see at
  all, since it's not the caller). Enabling "require PR before merge" on
  `main` would ALSO block the loop's own routine direct pushes
  (`outputs/**`/`queues/**`/`inbox/**`), since GitHub has no native
  path-scoped branch protection — that's the whole reason this guard
  exists. Turning it on safely needs a real design (e.g. a required status
  check from a path-policy Action, per `tests/test_branch_protection.py`'s
  `test_path_policy_workflow_present`, rather than a blanket PR requirement)
  — flagged `needs:human`, out of scope for this PR.
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
│   └── staging.py                     # git diff --cached + ls-remote/fetch-verified diff vs the REAL remote destination
├── tests/                             # 21 files, 142 tests, 90% coverage
└── deploy/
    ├── bubble-git-guard.template.sh   # wrapper installed to /opt/bubble-git-guard/bin/
    └── INSTALL-ON-MORTY.md            # full operator runbook
```

## See also

- `token-broker/` — Step 3b: GitHub App installation-token broker (the "tokens limitent les repos et permissions" half of Notion line 725)
- `MVP-ROADMAP.md` §3 — full step-by-step roadmap
- Notion v4 §"GitHub access model" — canonical doctrine
