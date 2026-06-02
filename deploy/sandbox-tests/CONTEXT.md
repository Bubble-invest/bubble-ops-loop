# Sandbox Implementation — Shared Context Pack (for all subagents)

**Owner:** Rick (R&D). **Date:** 2026-06-02. **Status:** Wave 1, fixture-first.
**Parent plan (READ IT FIRST):** `../SANDBOX-SCOPING.md`
**Green light from Joris (msg 3621):** host-prep approved, fixture-first, quality over speed.

This file is the single source of ground truth. Every fact below was verified live
on the box by Rick on 2026-06-02 (tagged `[VERIFIED-LIVE]`). Do not assume anything
not written here; if you need a fact that isn't here, find it and report it back —
do not guess.

---

## 0. What we are building (one paragraph)

Layer B of agent hardening: an **OS-level sandbox** (Claude Code's built-in
`sandbox` setting, backed by `bwrap` + `socat` + npm `@anthropic-ai/sandbox-runtime`)
that jails the **Bash tool and all its child processes** so a prompt-injected raw
subprocess (`python -c "open('/etc/age/key.txt')"`) can't read secrets, can't exfil
to arbitrary domains, and can't write outside its repo. Layer A (managed-settings
deny rules + un-removable mission-guard hook) is already SHIPPED and live; the
sandbox is **purely additive** — it never becomes the sole control, and rollback is
always a single root edit.

## 1. The TDD discipline for THIS project

The unit of test is a **probe script**, not a Python unittest. This is live-infra
validation, so:

1. Write the probe FIRST. It must exit **non-zero (RED)** before the change exists.
2. Make the change.
3. Re-run the probe. It must exit **zero (GREEN)**.
4. **Independently confirm the user-visible truth** — don't trust the exit code
   alone. Example: for "secret read is blocked", actually attempt the read and
   confirm the secret bytes are NOT in the output, AND confirm the sandbox was
   genuinely ON (see the fail-open trap in §6). Rick has been burned before by
   "exit 0 ≠ actually working" — every green claim ships with the command output.

Probes live in `deploy/sandbox-tests/`. Each probe is a standalone bash script,
`set -euo pipefail`, self-describing header, prints `RED`/`GREEN` + evidence, exits
1 on RED / 0 on GREEN. They must be **idempotent and re-runnable**.

## 2. Box ground truth `[VERIFIED-LIVE 2026-06-02]`

- **Host:** Hetzner, Ubuntu 24.04.4, kernel `6.8.0-117-generic`. SSH alias from
  Rick's Mac: `hetzner-root` (root) / `hetzner` (claude). Subagents on the Mac reach
  it via `ssh hetzner-root '<cmd>'`.
- **Kernel sandbox primitives:** `landlock: Up and running`,
  `kernel.unprivileged_userns_clone = 1`, `user.max_user_namespaces = 30852`.
  Kernel is CAPABLE.
- **BLOCKER 1 — deps missing:** `bwrap` MISSING, `socat` MISSING,
  npm `@anthropic-ai/sandbox-runtime` MISSING. `node v22.22.2`, `npm 10.9.7` present
  (version is fine for the runtime).
- **BLOCKER 2 — AppArmor:** `kernel.apparmor_restrict_unprivileged_userns = 1`
  blocks the unprivileged userns that `bwrap` needs. Must add an AppArmor profile
  for `/usr/bin/bwrap` (see host-prep, §4).
- **Agents (all `User=claude`, one shared OS user uid=1000):**
  - `ops-loop-tony.service` (dept=tony) — LIVE, PII-adjacent management. DO NOT TOUCH.
  - `ops-loop-maya.service` (dept=maya) — LIVE, onboarding, lowest blast radius =
    the chosen Wave-3 canary. DO NOT TOUCH in Wave 1–2.
  - `ops-loop-cgp.service` (dept=cgp) — LIVE, PII. DO NOT TOUCH.
  - `claude-agent-claudette.service`, `claude-agent-morty.service` — LIVE. DO NOT TOUCH.
  - `bubble-ops-console.service` — operational frontend. DO NOT TOUCH.
- **THE FIXTURE (use this, do not create a new one):**
  - Dir: `/home/claude/agents/fixture/` ; `bubble-ops-fixture` is a symlink → `fixture`.
  - GitHub remote: `https://github.com/vdk888/bubble-ops-fixture.git`.
  - Has its own `.claude/settings.json` (workspace perms, NOT managed-settings).
  - `ops-loop-fixture.service` exists but is **DISABLED** → it is NOT a running
    agent. Safe to run `claude` headless against `/home/claude/agents/fixture` by
    hand, or enable the service deliberately for a controlled canary.

## 3. The push path = the #1 breakage risk (T3) `[VERIFIED-LIVE]`

git push authenticates through a **`sudo` chain**:
- `/usr/local/bin/bubble-gh-credential-helper.sh` (root, 5619 bytes) mints a
  GitHub App token; it must read `/srv/bubble-secrets/...sops.pem` which needs
  `/etc/age/key.txt` — both root-only, hence the sudo.
- Sudoers grant (`/etc/sudoers.d/bubble-cred`):
  `claude ALL=(root) NOPASSWD: /usr/local/bin/bubble-gh-credential-helper.sh`
- **CORRECTION (verified live, was wrong in v1):** the structural-403 / READ-ONLY-token
  decision lives INSIDE the cred-helper, not in bubble-git-guard. The cred-helper
  calls `bubble-is-structural-push.py`; if the un-pushed delta hits a
  `STRUCTURAL_PATH_GLOBS` path (broker policy `/opt/bubble-token-broker/src/policy.py`)
  it mints a `contents:read` token → push 403. `/opt/bubble-git-guard/bin/bubble-git-guard`
  (root, 390 b) is a SEPARATE broker CLI (`python3 -m src.cli`). So the structural
  lock fires within the `sudo -n` cred-helper call → `excludedCommands` (or
  `allowUnsandboxedCommands`) MUST let the sudo→cred-helper chain through, or both
  the token mint AND the structural-lock break.
- **Why this breaks under sandbox:** a sandboxed `git push` spawns `sudo`, and
  sudo + userns/seccomp jails interact badly. Mitigation in the plan:
  `sandbox.excludedCommands: ["/opt/bubble-git-guard/bin/bubble-git-guard *", "sops *"]`
  + keep `allowUnsandboxedCommands: true` initially. **T3 must prove BOTH**: a
  normal push succeeds AND a structural push still gets the read-only-token 403
  (mission-lock intact).

## 4. Host-prep (Joris APPROVED — but run deliberately, with rollback) `[DOCS]`

This is a host-level security-posture change (re-permits unprivileged userns for
the `bwrap` binary only). The `host-prep-scoper` subagent writes an idempotent
install script + a rollback script; it does **NOT execute them**. Rick + Joris
run the install once, by hand, after review.

```bash
# install (idempotent target)
sudo apt-get install -y bubblewrap socat
sudo npm install -g @anthropic-ai/sandbox-runtime
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo systemctl reload apparmor
# rollback = remove the profile + reload; apt/npm removal optional (additive, harmless)
```
The script must: detect already-installed deps and skip; verify `command -v bwrap`,
`command -v socat`, `npm ls -g @anthropic-ai/sandbox-runtime` after; verify the
AppArmor profile loaded (`sudo apparmor_status | grep bwrap`); print a clear
GREEN/RED summary; be safe to re-run.

## 5. Layer A managed-settings (already live — DO NOT EDIT in Wave 1–2) `[VERIFIED-LIVE]`

`/etc/claude-code/managed-settings.json` (root-owned, 2150 b). Contains:
- `permissions.deny` with **`//abs/path/**` syntax** (note the leading `//` for
  absolute paths — this is permission-rule syntax). Denies Read/Edit of
  `/etc/age`, `/srv/bubble-secrets`, `/etc/bubble`, `/run/claude-agent**`,
  `/run/gws-tony`, `/root`, + Edit-lock on `/etc/claude-code`, the guards,
  cred-helper, and `/home/claude/.claude/settings.json`.
- `hooks.PreToolUse` → `python3 /opt/bubble-mission-guard/mission-file-guard.py`
  (matcher `Edit|Write|Bash|NotebookEdit`, timeout 10000). This is the
  un-removable mission-guard.

**IMPORTANT syntax distinction (from the plan):** permission rules use `//abs`,
but the **sandbox `filesystem` paths use plain `/abs` + `~/`** (no double slash).
Do not copy the `//` style into the sandbox block.

## 6. Traps to respect (these have bitten us)

- **Fail-open:** `sandbox.failIfUnavailable: false` means a missing dep SILENTLY
  disables the sandbox. A "passing" block-test could just mean the sandbox is OFF.
  Every block-test probe MUST first assert the sandbox is genuinely ENABLED
  (e.g., a known-jailed action is jailed) before trusting that a deny worked.
  Keep `false` until deps installed + T1 green; flip `true` LAST.
- **TLS not inspected:** network filter matches hostname only → allowed domains
  (github.com, api.anthropic.com) are domain-fronting exfil paths. Residual risk,
  not fixable in this layer. Note it; don't try to solve it here.
- **Shared user:** sandbox is per-process, NOT per-OS-user. It does NOT isolate
  agent-from-agent (all share `claude`). Don't claim isolation it doesn't provide.
- **Never call `getUpdates` / disrupt a live agent's telegram poller** while
  probing. Wave 1–2 stay entirely on the fixture + host-level checks. No live
  agent (tony/maya/cgp/claudette/morty) is touched until Wave 3, human-gated.
- **Never edit `/etc/claude-code/managed-settings.json` from a subagent.** That's
  human-gated (Rick/Joris) in Wave 3 only.

## 7. Official docs (authoritative — read before guessing)

- Sandboxing: https://code.claude.com/docs/en/sandboxing
- Sandbox settings: https://code.claude.com/docs/en/settings#sandbox-settings
- General settings: https://code.claude.com/docs/en/settings

## 8. Definition of done for Wave 1

1. `deploy/sandbox-tests/` contains T0–T5 probes, each RED-now-by-design and
   documented, committed.
2. `deploy/host-prep-sandbox.sh` + `deploy/host-prep-sandbox-rollback.sh` written,
   idempotent, NOT executed, reviewed by Rick.
3. The fixture is confirmed usable for headless `claude` runs (no live agent touched).
4. Everything committed to the fixture repo or staged for Rick to commit; STATUS
   written back. No managed-settings edits. No live-agent edits.
