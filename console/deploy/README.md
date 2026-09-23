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

### uid isolation (board #1463)

The console runs as a dedicated `bubble-console` system uid, **not** the
general-purpose `claude` uid — see the template's own header comment for the
full rationale (the App-signed structural-approval token and the
structural-path policy source must not sit in the same trust domain as
`cloud-wiki-compile@*`'s agentic `claude -p` session). `WorkingDirectory` is
the existing root-owned infra clone `/opt/bubble-ops-loop` (already kept
current every 15 min by `bubble-deploy-infra.timer`), not the claude-writable
checkout at `/home/claude/bubble-ops-loop` — that checkout still exists for
Rick's own git operations, it's just no longer what the console runs from.
`bubble-console` is a supplementary member of the `claude` unix group ONLY
(one-directional) so it can still read+write `/home/claude/agents`; `claude`
is not a member of `bubble-console`. One-time provisioning commands and the
full verification checklist are in the board #1463 PR's runbook — this is an
uid/ownership change on the live console, so it is applied by an operator,
not by merging code.

**Follow-up (board #1463, found during the #489 rollout): scoped ACL.**
Group membership alone was not enough — dept dirs under `/home/claude/agents`
are often `0700`/`0755`, so the console 500'd reaching e.g.
`inbox/decisions`. `scripts/deploy-console-to-vps.sh` now also applies (as
root, before restarting the service) a POSIX ACL scoped to exactly that
directory: `setfacl -R -m g:bubble-console:rwX /home/claude/agents` plus a
recursive default ACL (`setfacl -R -d -m g:bubble-console:rwX
/home/claude/agents`) so newly-onboarded depts inherit the grant too. This
is idempotent and narrower than the DAC-wide `claude`-group membership (an
independent security reviewer's suggested follow-up on the original PR) —
the group membership is left in place, the ACL is additive belt-and-suspenders
scoping. The board token file used by the Mac emitter fallback and VPS
claude-uid emitters (`/run/bubble-board/token`) is unrelated to this uid and
stays `root:claude`, as before #489 — it was briefly moved to
`root:bubble-console` during the rollout and reverted.

### Rick (`rnd`) read-mirror registration — post-merge only

Rick runs on Joris's Mac M4. The VPS paths are read-only views, never a second
Rick runtime: the cockpit discovers `/home/claude/agents/bubble-ops-rnd` from
its committed `onboarding/STATE.yaml`, while Tony reads the same mirror through
the sibling path `/srv/agents/bubble-ops-rnd`. After both #1269 and #1270 merge,
an operator may run this fail-stop block on the VPS. This PR does not run it.
Until the mirror is present and `status: Live`, the graph deliberately retains
the existing static Rick node; registration replaces it atomically with
`dept:rnd` rather than making Rick disappear during a staggered rollout.

```bash
(
set -eu
MIRROR=/home/claude/agents/bubble-ops-rnd
TONY_VIEW=/srv/agents/bubble-ops-rnd
test ! -e "$MIRROR"
test ! -e "$TONY_VIEW"
sudo -u claude git clone --filter=blob:none --branch main --single-branch \
  git@github.com:vdk888/bubble-rnd-workspace.git "$MIRROR"
test "$(sudo -u claude git -C "$MIRROR" branch --show-current)" = main
test -z "$(sudo -u claude git -C "$MIRROR" status --porcelain)"
test "$(sed -n 's/^host:[[:space:]]*//p' "$MIRROR/onboarding/STATE.yaml")" = local
sudo ln -s "$MIRROR" "$TONY_VIEW"
test "$(readlink -f "$TONY_VIEW")" = "$MIRROR"
)
```

Then run the already-deployed `sync-local-dept-clones.service` once and verify
its timer keeps the exact `vdk888/bubble-rnd-workspace` origin current. Only
after the mirror and symlink checks pass should the operator restart the
cockpit and allow Tony's next normal L1/L2/L4 cycle. Verify `/dept/rnd` shows
`host: local`, four layers, and the latest `management-export.yaml`; verify the
org graph has `dept:rnd` and no `local:rick` ghost. No service, clone, symlink,
or timer is changed merely by merging this code.

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
