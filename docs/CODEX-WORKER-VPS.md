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
