# Codex workers on the VPS

Use `scripts/codex-worker-vps.sh` for disposable Codex coding or research
clones. Do not create ad hoc `/tmp/codex-*` directories. A normal `0022`
umask makes their checked-out files world-readable, and a clone left behind
can make the plaintext-secret sweep mistake tracked source such as
`secrets_cli.py` for a leaked secret.

The wrapper applies the required convention:

- `umask 077` is set before the task brief or clone is created;
- the generated scratch directory is mode `0700`;
- the stdin task brief is stored mode `0600` inside that directory;
- Codex is pinned to `gpt-5.6-sol` with high reasoning;
- the command runs from the fresh clone;
- an `EXIT` trap removes only the wrapper-owned scratch directory, on success,
  failure, or a handled termination signal.

Typical coding dispatch:

```bash
ssh joris-cx33 'cd /opt/bubble-ops-loop && \
  scripts/codex-worker-vps.sh \
    --repo https://github.com/Bubble-invest/REPO.git \
    --sandbox workspace-write' <<'TASK'
<complete bounded task, including allowed and forbidden scope>
TASK
```

Use `--ref <branch-or-tag>` when the worker must start from a specific remote
ref. The default sandbox is `read-only`; coding tasks must explicitly select
`workspace-write`. The model and reasoning floor are fixed by the wrapper, so
callers cannot silently drift below the current fleet coding policy.

The wrapper exports `CODEX_WORK_ROOT`, `CODEX_WORK_REPO`, and
`CODEX_WORKER_TASK_FILE` to Codex. The child must push or otherwise preserve
any intended artifact before it exits. The clone is deliberately disposable.
There is no keep/debug option: copying a failed clone back into `/tmp`
recreates the original leak-audit noise. Re-run the bounded task instead.

This is a preventive control, not a scanner exception. Do not exclude Codex
paths from `secrets-tmp-sweep.sh`, weaken its filename patterns, or broadly
clean `/tmp`. The detector must continue to find real matching leftovers.

## Isolated worker checkout standard (#1306)

Every worker must have its own clone or worktree, even when workers edit
unrelated files. Never branch, stage, commit, or build in the live framework
checkouts `/home/claude/bubble-ops-loop` and `/opt/bubble-ops-loop`, anywhere
under `/srv/agents`, or a shared `scratch_work/*` checkout. Start from
`origin/main` unless the brief explicitly requires another base.

Prefer the wrapper above with `--ref main`. It checks the scratch base before
creating any files and checks the clone before launching Codex. A scratch
base inside any existing Git checkout is refused, as are managed live paths
and their symlink aliases. Keep scratch storage outside shared checkouts.
Python 3 and Git must be available; a failed preflight stops dispatch.

For a persistent worktree, use a separate development clone as the source
and a unique path/branch for each worker (substitute the issue number):

```bash
cd /home/claude/worktrees/framework-source
git fetch origin main
python3 scripts/worker-checkout-guard.py /home/claude/worktrees/issue-1306 &&
  git worktree add -b fix/1306 /home/claude/worktrees/issue-1306 origin/main
cd /home/claude/worktrees/issue-1306
python3 scripts/worker-checkout-guard.py "$PWD" || exit 2
```

Run this preflight before handing a checkout to any other worker launcher.
If refused, leave the checkout, branch and staged changes intact and dispatch
in a new isolated location. Do not reset, stash, clean or switch the live tree.
The guard is a dispatch preflight, not an OS sandbox: other launchers must call
it, and it does not intercept arbitrary shell commands or install Git hooks.
No deployment or emergency operator workflow is changed.

**FR :** Chaque worker utilise son propre clone ou worktree, créé depuis
`origin/main`. Ne jamais coder dans les checkouts actifs ci-dessus ni dans un
checkout partagé. Le lanceur vérifie le chemin avant toute écriture ; pour un
autre lanceur, appeler le même garde avant de démarrer le worker. En cas de
refus, préserver la branche et les fichiers existants et utiliser un nouvel
emplacement isolé.
