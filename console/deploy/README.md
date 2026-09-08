# bubble-ops-console — deploy

Single-binary FastAPI app. Notion v5 lines 1006-1020.

## Run locally (operator Mac)

```bash
cd ~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
export CONSOLE_BEARER_TOKEN="$(openssl rand -hex 24)"          # API clients only
export CONSOLE_GATE_RBAC='{"rick":["*"]}'                    # gate write grants
export READ_FROM_DISK="/Users/{{OPERATOR_USER}}/bubble-ops-depts"          # parent dir of bubble-ops-* clones
python3 -m uvicorn console.main:app --host 127.0.0.1 --port 8642
```

Then open `http://127.0.0.1:8642/login` and sign in with a user from
`CONSOLE_LOGIN_USERS`. The legacy `?token=<bearer>` browser bootstrap is no
longer accepted. `Authorization: Bearer` remains available to API/CI callers.

The console reads any subdir matching `bubble-ops-*` under `READ_FROM_DISK`. Each subdir must follow the on-disk shape from Notion v5 lines 751-762 (`dept.yaml`(.draft), `onboarding/STATE.yaml`, `queues/gates/*.yaml`, etc.).

## Expose via Tailscale (operator phone)

```bash
# on the operator Mac (already on the tailnet)
sudo tailscale serve --bg --tls-terminated-tcp 8642 http://127.0.0.1:8642
# then on your phone: open https://<mac-hostname>.<tailnet>.ts.net/
```

The console binds **only to `127.0.0.1`**. Tailscale terminates TLS and tunnels to the loopback — no clearnet exposure ever.

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `CONSOLE_BEARER_TOKEN` | (empty) | Optional API/CI bearer; query-string bearer bootstrap is disabled |
| `CONSOLE_LOGIN_USERS` | (empty) | JSON map of login usernames to password hashes |
| `CONSOLE_GATE_RBAC` | (empty -> deny gate writes) | JSON map of principal to allowed dept slugs; use `"bearer"` for the API bearer and `"*"` as an explicit all-dept grant |
| `READ_FROM_DISK` | (empty -> github mode) | Parent dir of `bubble-ops-<slug>` repos. v1 ships disk-mode only |
| `BUBBLE_OPS_GITHUB_ORG` | `Bubble-invest` | GitHub org hosting `bubble-ops-<slug>` repos (host=local decision PUTs) |
| `GH_CACHE_TTL` | `60` | Seconds to cache `gh api` responses |
| `CONSOLE_BIND_HOST` | `127.0.0.1` | Bind host (do NOT change to `0.0.0.0`) |
| `CONSOLE_BIND_PORT` | `8642` | Bind port |

## systemd unit (VPS)

Canonical unit source: `deploy/bubble-ops-console.service.template` — kept in sync with what production actually runs (board #1081). Install/update it on a box with `scripts/deploy-console-to-vps.sh` (see `deploy/INSTALL.md` step 2); `scripts/deploy-console-to-morty.sh` re-syncs it automatically on every ongoing git-pull deploy. Full `bubble-vps-platform` pyinfra integration is still a follow-up (UX-5).

## Routes

| Route | Purpose | Notion v5 ref |
|---|---|---|
| `GET /` | cross-dept kanban of pending gates | line 1014 |
| `GET /dept/<slug>` | per-dept detail | line 1015 |
| `GET /gate/<dept>/<id>` | decision card | line 1018 |
| `POST /gate/<dept>/<id>/decide` | writes `inbox/decisions/<id>.yaml` | line 1018 |
| `GET /settings/<slug>` | per-dept knobs (read-only v1) | line 1019 |
| `GET /health` | per (dept x layer) heartbeat freshness | line 1020 |
| `GET /agents` | live + à éclore nav | line 1016 |
| `GET /agents/new` | bootstrap form | line 749 ("+ New department") |
| `POST /agents/new` | invokes `scripts/bootstrap-dept.sh` | line 749 |
| `GET /agents/<slug>/onboarding` | 3-pane onboarding view | line 1017 |
| `GET /health-noauth` | unauthenticated liveness probe (Tailscale) | n/a |

## Tests

```bash
python3 -m pytest console/tests/ -v
```

21 tests. Zero network/GitHub side effects (mocked via conftest).

## Stack

- Python 3.9+ / FastAPI 0.128 / Jinja2 3.1
- Tailwind CSS 3 via CDN (no build pipeline)
- HTMX 1.9.12 via CDN
- IBM Plex Mono + Inter (Google Fonts CDN)

No JavaScript framework. Single binary. ~700 LOC of Python + ~500 LOC of templates.
