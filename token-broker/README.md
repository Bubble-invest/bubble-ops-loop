# bubble-token-broker

GitHub App installation-token broker for **Morty VPS**. Implements **Step 3b**
of `bubble-ops-loop` MVP-ROADMAP v2.

Mints short-lived (≤60 min) installation access tokens from a GitHub App
private key, **in-memory only**, scoped down per action class, with a metadata-
only audit log. No PAT classique anywhere; no long-lived per-dept token; no
plaintext PEM or token ever touches disk.

> **Doctrine (Notion v4, "GitHub access model"):** *Pas de PAT long-lived par
> département. Un département a une identité logique et une policy d'accès,
> pas un secret permanent. Les tokens GitHub sont mintés à la demande, courts,
> scoped, jamais stockés dans Git.*

---

## Why this exists

Per **Notion v4** (`/tmp/notion_final.txt`, §"GitHub access model — identities,
tokens & PR boundaries", lines ~563-725):

- The bubble-ops `/loop` agent on Morty needs to `git push` outputs/queues/inbox
  files to per-dept GitHub repos every tick.
- Using a long-lived PAT was the v1 fallback. It's **explicitly forbidden in v4**:
  *"Installation access token long-lived / PAT classique / GITHUB_TOKEN dans
  logs"* are all listed under "**À ne jamais stocker**" (lines 716-724).
- The replacement is a GitHub App (`bubble-ops-bot`, ID `3782718`, installation
  `134075326` for `bubble-ops-fixture`) whose private key sits SOPS-encrypted on
  Morty. The broker mints **short-lived installation tokens on demand**.

---

## Security model (5 invariants)

| # | Invariant | Where enforced |
|---|-----------|----------------|
| 1 | **Tokens are in-memory only** | `Broker._cache` is a Python dict, never serialized to disk by `src/`. Cleared on process exit. |
| 2 | **PEM is in-memory only** | `pem_provider: Callable[[], bytes]` injection; production wraps `sops --decrypt …` stdout. The broker never `open(…, "w")`s the PEM. |
| 3 | **TTL ≤ 60 min** (Notion v4 audit example) | `MAX_TTL_MINUTES = 60`. Token cache refresh buffer = 60s (refreshes at `expires_at - 60s`). |
| 4 | **Permission down-scoping at mint time** | `PERMISSION_CLASSES` maps each action → minimal `permissions` dict, sent in the POST body. GitHub mints the token already restricted. |
| 5 | **Audit metadata only, never the token value** | `audit.py` (a) drops fields named `token`/`pem`/`secret`/etc., (b) raises `ValueError` on any string value starting with `ghs_`. |

Citations to Notion v4 in code comments:
- `src/broker.py` header → "Token broker Morty" (lines 592-614)
- `src/policy.py` header → "Classes de tokens éphémères" (lines 616-622), "Policies par type d'acteur" (lines 623-694)
- `src/audit.py` header → audit schema example (lines 605-614)
- `src/cli.py` examples → §"Secrets" (lines 702-724)

---

## Action classes (Notion v4 lines 619-622)

| Class | `permissions` sent | Allowed paths | Mode |
|-------|--------------------|---------------|------|
| `runtime_read` | `contents:read, metadata:read` | (n/a, read-only) | Direct |
| `runtime_write_own` | `contents:write, metadata:read` | `outputs/**, queues/**, inbox/**` in own repo | Direct commit |
| `open_priority_pr` | `contents:write, pull_requests:write, metadata:read` | `queues/management/**` in target child repo | PR only |
| `settings_pr` | `contents:write, pull_requests:write, metadata:read` | structural paths (`dept.yaml`, `layers/**`, `.claude/agents/**`, `skills/**`, `tools/**`, `.claude/settings.json`, …) | PR only |

> **Notion v4 line 725:** GitHub does not provide a true path-scope at the
> `contents:write` token level. So **paths are enforced by THIS module** at
> mint-time (against an actor policy YAML), AND by the Morty git guard
> (Step 3c, separate component) at push-time. Belt and suspenders.

---

## Install

### On Mac (dev / test)

```bash
cd token-broker
python3 -m pip install -r requirements.txt
python3 -m pytest tests/ -v
python3 -m pytest --cov=src tests/
```

### On Morty (production)

See [`deploy/INSTALL-ON-MORTY.md`](deploy/INSTALL-ON-MORTY.md) for the full
runbook (SOPS env decrypt chain, smoke tests, real-mint validation, audit log
location, wiring into `ops-loop-fixture.service`).

---

## CLI usage

```bash
# 1) Mint a token for runtime writes to own repo (production shape)
bubble-token-broker mint \
  --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
  --paths outputs/2026-05-20/1/summary.md

# Reads env (set via /run/claude-agent/env → EnvironmentFile= drop):
#   GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE,
#   GITHUB_APP_PRIVATE_KEY_PATH, BUBBLE_TOKEN_BROKER_POLICY.
# Prints the token to STDOUT (single line, no newline). Audit to file.

# 2) Policy dry-run (no PEM, no API, no token leaves)
bubble-token-broker check \
  --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
  --paths outputs/2026-05-20/1/summary.md \
  --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml

# 3) Offline mint (no GitHub API) — useful for CI / dev
bubble-token-broker mint \
  --dept fixture --action runtime_read --repo bubble-ops-fixture \
  --app-id 3782718 --installation-id 134075326 \
  --pem-path /tmp/test.pem --no-sops --mock-github
```

`bubble-token-broker --help` shows full reference + examples.

---

## Consumption pattern (inside `ops-loop-fixture` wrapper)

```bash
TOKEN=$(bubble-token-broker mint \
  --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
  --paths "$STAGED_PATHS")

# Inject only into the single git push, never export globally
git -c "http.https://github.com/.extraheader=AUTHORIZATION: bearer $TOKEN" push origin HEAD
unset TOKEN  # gone
```

---

## Architecture (file map)

```
token-broker/
├── README.md                                # this file
├── requirements.txt                         # cryptography, requests, pyyaml
├── src/
│   ├── __init__.py
│   ├── broker.py        — JWT mint + installation-token mint + in-memory cache
│   ├── policy.py        — actor policy YAML loader + 4-class enforcement
│   ├── cli.py           — argparse CLI (mint + check) + env resolution
│   └── audit.py         — JSONL metadata-only audit; FORBIDDEN_FIELDS guard
├── tests/               — 65 tests, 95% coverage on src/
│   ├── conftest.py                           — in-memory RSA-2048 PEM fixture
│   ├── test_jwt_mint.py                      — RS256, iat/exp/iss claims
│   ├── test_installation_token_mint.py       — endpoint, headers, body
│   ├── test_token_expiry.py                  — cache hit/miss, refresh buffer
│   ├── test_policy_runtime_read.py
│   ├── test_policy_runtime_write_own.py
│   ├── test_policy_open_priority_pr.py
│   ├── test_policy_settings_pr.py
│   ├── test_audit_log_no_secret_leak.py
│   ├── test_cli_env_resolution.py
│   └── test_cli_e2e.py                       — CLI: token→stdout, audit→file
└── deploy/
    ├── bubble-token-broker.service.template
    ├── INSTALL-ON-MORTY.md
    └── policies/
        ├── fixture-policy.yaml                  — live policy for bubble-ops-fixture
        ├── ops-leaf-policy.template.yaml        — template for ops leaves (Maya, Ben, …)
        ├── management-policy.template.yaml      — template for management depts (Tony, …)
        └── console-policy.template.yaml         — drop-in for bubble-ops-console
```

---

## Instantiating a policy for a new dept

The `deploy/policies/` directory ships ONE live policy (`fixture-policy.yaml`)
and three TEMPLATES. To onboard a new dept, copy the right template and fill
the placeholders:

```bash
# Ops leaf (Maya, Ben, Miranda, Eliot):
cp deploy/policies/ops-leaf-policy.template.yaml \
   deploy/policies/<dept_slug>-policy.yaml
sed -i 's/<DEPT_SLUG>/maya/g' deploy/policies/maya-policy.yaml

# Management (Tony):
cp deploy/policies/management-policy.template.yaml \
   deploy/policies/tony-policy.yaml
sed -i 's/<DEPT_SLUG>/tony/g' deploy/policies/tony-policy.yaml
sed -i 's/<CHILD_SLUG_1>/ben/g'    deploy/policies/tony-policy.yaml
sed -i 's/<CHILD_SLUG_2>/maya/g'   deploy/policies/tony-policy.yaml
# (add as many CHILD lines as needed in the file before the sed pass)

# Console (drop-in, no placeholders):
cp deploy/policies/console-policy.template.yaml \
   deploy/policies/console-policy.yaml
```

Then validate the new YAML loads cleanly:

```bash
python3 -m src.cli check --dept <dept_slug> --action runtime_read \
  --repo bubble-ops-<dept_slug> \
  --policy deploy/policies/<dept_slug>-policy.yaml
```

> **Templates are reference shapes only.** Each placeholder `<DEPT_SLUG>` and
> `<CHILD_SLUG_*>` MUST be replaced before deployment. The broker does not
> sanitize unfilled placeholders — an unfilled `<DEPT_SLUG>` will fail the
> `actor` match check at mint time (intentional: fail loud).

---

## What this broker does NOT do (deliberately out of scope)

- **Path-level enforcement at push time** — that's the Morty **git guard**
  (Step 3c, separate component). The broker only down-scopes the token's repo
  and permission class; the guard verifies the actual files being pushed.
- **Webhook handling** — no inbound webhooks. The broker is a pull-only minter.
- **Multi-installation support** — v1 is single-installation
  (`bubble-ops-fixture` only). Multi-dept arrives in v2 when Tony/Maya/Ben land.
- **Long-running daemon** — v1 is oneshot CLI. The systemd unit template in
  `deploy/` is for future daemon mode.

---

## References

- Notion v4 architecture page → `/tmp/notion_final.txt`, lines 563-725
- GitHub App auth → https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app
- Installation token endpoint → https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
- MVP-ROADMAP v2 → `./MVP-ROADMAP.md`
