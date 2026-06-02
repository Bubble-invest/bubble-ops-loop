# Sandbox probes (T0–T5) — Wave 1

TDD "tests" for **Layer B** of VPS agent hardening: Claude Code's OS-level Bash
sandbox (`bwrap` + `socat` + `@anthropic-ai/sandbox-runtime`) on the Hetzner box.

Read these first — they are the source of truth:
- [`CONTEXT.md`](./CONTEXT.md) — ground-truth pack (box facts, traps, push chain).
- [`../SANDBOX-SCOPING.md`](../SANDBOX-SCOPING.md) — parent plan (T1–T5, rollout, proposed managed block).
- Docs: <https://code.claude.com/docs/en/sandboxing> · <https://code.claude.com/docs/en/settings#sandbox-settings>

## The probe model

The unit of test is a **probe script**, not a Python unittest — this is
live-infra validation. Each probe is a standalone `bash`, `set -euo pipefail`,
self-describing, prints `RED`/`GREEN` + evidence, exits **1 on RED / 0 on
GREEN**, and is **idempotent / re-runnable**.

### Red → green lifecycle

1. Probe is written FIRST and must exit **RED** before the change exists.
2. Host-prep + the managed `sandbox` block land (separate, human-gated steps).
3. Re-run the probe; it must exit **GREEN**.
4. **Independently confirm the user-visible truth** — never trust the exit code
   alone (CONTEXT.md §1). The block-tests assert the *content* (e.g. secret
   bytes absent), and assert the sandbox is genuinely ON before trusting any
   "blocked" verdict (the **fail-open trap**, CONTEXT.md §6).

In Wave 1, **T0–T3 are RED by design** (deps not installed yet). T4 and T5 are
read-only and can go GREEN earlier.

## The probes

| Probe | Proves | RED now means | GREEN means |
|-------|--------|---------------|-------------|
| `T0_deps_present` | `bwrap`, `socat`, npm `@anthropic-ai/sandbox-runtime`, and the AppArmor `bwrap` profile are all present | one+ missing (host-prep not run) | all four present |
| `T1_sandbox_init` | headless `claude -p` in the fixture initializes the sandbox; a trivial sandboxed `Bash(ls)` runs and an out-of-cwd write is refused | deps missing / init failed | sandbox engaged |
| `T2_skip_perms_holds` | **critical**: under `--dangerously-skip-permissions --print`, secret read (`/etc/age/key.txt`) and exfil to an unallowed domain are BLOCKED — after first proving the sandbox is genuinely ON (positive control) and that the secret bytes are absent (content check) | sandbox not installed | containment proven |
| `T3_git_push` | with sandbox on, a **normal** fixture push SUCCEEDS through the sudo cred-helper, AND a **structural** push still gets the read-only-token **403** (mission-lock intact) | sandbox not installed | push survives + mission-lock holds |
| `T4_domain_write_inventory` | read-only inventory of outbound domains + out-of-repo writes for the fixture → feeds `allowedDomains` / `allowWrite` before `allowManagedDomainsOnly` is flipped | report not generated | complete report at `T4_inventory_report.md` |
| `T5_poller_topology` | read-only: is the telegram `bun` poller a CHILD of `claude` (needs sandbox allowances) or a SEPARATE host process (needs none)? | no poller running / ambiguous | clear determination |

## How to run

```bash
cd projects/bubble-ops-loop/deploy/sandbox-tests
./run_all.sh                 # T0..T5, summary table, exit 1 if any RED
./T0_deps_present.sh         # or run a single probe
```

All probes reach the box read-only via `ssh hetzner-root '<cmd>'` (override with
`SSH_HOST=...`). Fixture path defaults to `/home/claude/agents/fixture`
(override with `FIXTURE=...`).

## Safety / scope (Wave 1 = zero blast radius)

- Probes **install nothing**, never edit `/etc/claude-code/managed-settings.json`,
  never touch a live agent (tony/maya/cgp/claudette/morty/console), never
  restart a service, and never call `getUpdates`.
- The only probe that **writes** is `T3`, and it pushes **only** to the
  throwaway fixture repo (`vdk888/bubble-ops-fixture`), to a unique
  `probe/t3-<epoch>` branch that is deleted on every exit path. It hard-aborts
  if the fixture's `origin` is anything else.
- T1/T2/T3 run `claude` headless **as the `claude` user in the fixture** with an
  **ephemeral `--settings` overlay** — they never persist sandbox config.

## Open items flagged for Rick (search `TODO(Rick)` in the scripts)

- **T1** — no documented machine-readable "sandbox active" signal for headless
  `-p` (the `/sandbox` panel is a TUI). T1 uses a behavioral proxy + negative
  control; confirm a canonical signal (e.g. `claude --debug` init log line).
- **T2** — verify the overlay keys against the box's `claude --version` once
  deps land (key names confirmed against docs; see report).
- **T3** — the probe's `excludedCommands` are deliberately permissive; replace
  with the MINIMAL set the real managed block ships and re-confirm leg (a).
