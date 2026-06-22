# Phase 1 — Scaffold the repo (bootstrap-dept.sh)

Script: `scripts/bootstrap-dept.sh`

```bash
BUBBLE_GITHUB_OWNER=Bubble-invest ./scripts/bootstrap-dept.sh \
  --slug=accountant \
  --display-name="Geraldine" \
  --owner=operator \
  --level=ops \
  [--accept-existing-empty-repo]   # if the human pre-created the org repo via the UI
```

## What the script does (in order)
1. **Pre-flight validation** — slug regex, bot-handle length (<32), BotFather uniqueness warning.
2. **GitHub repo** — `gh repo create Bubble-invest/bubble-ops-<slug> --private`.
3. **Local clone** into `/tmp/bubble-ops-<slug>/`.
4. **Branch** — `git checkout -b onboarding/<slug>`.
5. **Skeleton** — `scripts/lib/scaffold.py` materializes the full 16-file tree (below).
6. **Vendor canonical libs** — copies `dispatch_helpers.py` + `notify.py` / `loop_notify.py` /
   `notion_logbook.py` / `tools/notify_layer.py` from the framework so the dept starts
   byte-identical (avoids stale-template drift — this is load-bearing).
7. **Initial commit** + **push both** `onboarding/<slug>` AND `HEAD:main` (so GitHub has a
   default branch). Uses injected `GH_TOKEN` for App-token auth.

## What scaffold.py produces
```
bubble-ops-<slug>/
├── README.md  .gitignore
├── dept.yaml.draft               ← skeleton config, status=onboarding
├── CLAUDE.md                     ← self-driving éclosure prompt (7 hatching steps)
├── onboarding/
│   ├── STATE.yaml                ← lifecycle state machine (Idea → Live)
│   └── 1-mandate … 7-activation/ README.md
├── missions/  (.gitkeep)
├── layers/{1,2,3,4}/PROMPT.md    ← templated L1 Observe / L2 Orient / L3 Decide(fenced) / L4 Risk+logbook
├── queues/{research,gates,management,improvements}/
├── inbox/decisions/  outputs/onboarding/  skills/  subagents/  tools/
├── tests/run.sh (executable stub)
├── .claude/settings.json          ← minimal onboarding perms (Read only)
├── deploy/
│   ├── ops-loop-<slug>.service     ← pre-rendered systemd unit
│   └── policies/<slug>-policy.yaml ← token-broker actor policy (canonical)
└── scripts/lib/                    ← vendored dispatch_helpers + notify stack
```

## Gotcha
The empty-repo check tests `default_branch==null`, but modern GitHub sets `default_branch=main`
on 0-commit repos → detection fails. Use `--accept-existing-empty-repo` when the repo was
pre-created. (Upstream fix pending.)

## Verify before moving on
- The 16-file tree exists in `/tmp/bubble-ops-<slug>/`.
- `dispatch_helpers.py` is byte-identical to the framework canonical (`md5sum` match) — no drift.
- Both branches pushed; `onboarding/<slug>` is the working branch.
