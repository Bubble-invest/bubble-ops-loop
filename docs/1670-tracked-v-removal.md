# Board #1670: tracked `.v` removal preflight

This document records the bounded preflight performed on 2026-10-01 for
removing the legacy tracked `.v/` dependency tree from Git. It does **not**
authorize deleting a live virtual environment, changing a service, rewriting
history, or removing tracked runtime evidence.

## Finding

At `e2877d138caff591c144d9cafb6f47050566de05`, Git tracks 1,986 paths under
`.v/`. The directory is a stale macOS Python 3.9.6 virtual environment:

- `.v/bin/python3` points to the absent Xcode path
  `/Applications/Xcode.app/Contents/Developer/usr/bin/python3` in a clean
  worktree;
- `.v/bin/pip` has a shebang rooted at the old throwaway path
  `/private/tmp/w8-fix/.v/bin/python3`;
- the tree contains 42 installed distributions and Darwin CPython 3.9 binary
  extensions;
- no tracked path outside `.v/` refers to the literal `.v/` runtime path.

The repository's current runtime and rebuild paths are different and remain
unchanged:

- Mac loop tooling builds and uses `<repo>/.venv` from
  `scripts/requirements.txt` via `deploy/local/ensure-loop-venv.sh`;
- the VPS console deploy path uses `<repo>/venv/bin/python` and synchronizes
  `console/requirements.txt` in `scripts/bubble-deploy.sh`.

No production host or service was changed or queried for this preflight.

## Reproducible replacement evidence

`requirements/test-py312.in` names the source requirements and pytest;
`requirements/test-py312.lock` pins and hashes their complete 37-distribution
Python 3.12 closure. Build and verify it with the commands in `TESTING.md`; the
required checks are:

1. install the lock with `pip --require-hashes`;
2. `pip check` reports no broken requirements;
3. the default non-live test suite passes.

The pinned set was also resolved as 37 binary wheels for CPython 3.12 on
`manylinux_2_17_x86_64`, so the lock is not limited to the Mac wheel set used
for the local proof.

## Safest next reviewed change

After independent review of this preflight, a separate commit may remove only
`.v/` from the Git index and add no explicit runtime-deletion step:

```sh
git rm -r --cached .v
```

The command preserves the bytes in the authoring worktree while staging the
index deletion, and `.v/` is ignored so it is not re-added. A checkout that
later receives the merged deletion may remove its formerly tracked `.v/`
files, so operator review must first verify on the actual canary host that no
service or wrapper uses that path. Review must also verify that the staged diff
contains exactly the 1,986 legacy `.v/` paths and no runtime evidence. Operator
merge remains the gate.

## Deploy and rollback

Index removal needs no service restart and must not run `git clean` or delete
any `venv`, `.venv`, or `.v` directory. The deployment/canary check is limited
to verifying that existing service interpreter paths still point to `venv/`
or `.venv/`, then running their existing import/health checks.

Rollback is a normal revert of the index-removal commit. That restores the
tracked `.v/` snapshot in Git history; it does not replace, rebuild, stop, or
start a runtime environment. If a runtime environment ever needs rebuilding,
use its existing requirements-based path rather than the legacy tracked tree.
