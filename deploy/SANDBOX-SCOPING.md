# VPS Agent Sandbox — Scoping (2026-06-01)

Layer B of the agent-hardening plan ({{OPERATOR}} msg 3609/3611/3613). The OS-level
sandbox is the only layer that stops a **prompt-injected raw subprocess**
(`python -c "open('/etc/age/key.txt')"`) — which the Claude-Code-level deny
rules (Layer A, already shipped) do NOT catch.

Scoped by Rick + an Explore subagent (opus) in parallel; the subagent's
security-critical claims were independently **verified on the box** by Rick.
Tags: `[VERIFIED]` = Rick confirmed live; `[DOCS]` = official Anthropic docs.

---

## TL;DR — cannot deploy today, clear path to enable

The sandbox **cannot run on this box as-is.** `[VERIFIED]`
- `bwrap`, `socat`, npm `@anthropic-ai/sandbox-runtime` — **all three MISSING**.
- `kernel.apparmor_restrict_unprivileged_userns = 1` (Ubuntu 24.04.4 default) —
  **blocks** the unprivileged user namespace bwrap needs.
- Kernel primitives ARE ready: 6.8.0-117, `landlock: Up and running`,
  `unprivileged_userns_clone = 1`, `max_user_namespaces = 30852`.

Remediation (host-level posture change — review before running): `[DOCS]`
```bash
sudo apt-get install bubblewrap socat
sudo npm install -g @anthropic-ai/sandbox-runtime   # seccomp / unix-socket helper
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) { userns, include if exists <local/bwrap> }
EOF
sudo systemctl reload apparmor
```

## What it enforces & its limits `[DOCS]`

- Governs the **Bash tool + all its child processes** (OS-level). NOT
  Read/Edit/Write/WebFetch/MCP (those stay on the permission/deny layer = Layer A).
- **Per-process fs+network jail, NOT per-OS-user.** All 5 agents share user
  `claude`, so the sandbox does **not** isolate agent-from-agent. Per-agent
  separation stays with: managed `permissions.deny` + per-dept systemd unit +
  sops-guard. The sandbox's contribution: a prompt-injected Bash/python
  subprocess can't read secrets, can't exfil to arbitrary domains, can't write
  outside its repo.
- **Network proxy filters on hostname, does NOT inspect TLS.** Allowing
  `github.com`/`api.anthropic.com` leaves a domain-fronting exfil path. Residual
  risk (esp. cgp/PII). Real fix = MITM proxy + custom CA — a larger, later project.

## Top breakage risk `[VERIFIED]`

`git push` authenticates via **`sudo -n /usr/local/bin/bubble-gh-credential-helper.sh`**
(the helper wrapper sudos to mint the GitHub App token). A sandboxed push that
spawns `sudo` will likely fail. Mitigation: put the push chokepoint in
`sandbox.excludedCommands` (`/opt/bubble-git-guard/bin/bubble-git-guard *`) and
keep `allowUnsandboxedCommands: true` initially. Agents run **non-root**
(`User=claude`) `[VERIFIED]`, so `--dangerously-skip-permissions` is not
root-blocked.

## Doc ambiguity to TEST before rollout

Docs imply the sandbox holds under `--dangerously-skip-permissions` (orthogonal
layers) but never state it explicitly for headless `--print`. **Must verify
empirically (T2 below)** — this is the same skip-permissions flag the live
agents use, and Layer A already proved managed-deny holds under it, so the
expectation is good, but don't assume.

## Proposed managed `sandbox` block (review, not yet applied)

Lands in `/etc/claude-code/managed-settings.json` (root-owned, un-overridable).
NB: sandbox fs paths use plain `/abs` + `~/` (NOT the `//` of permission rules).
`allowWrite`/`allowedDomains` array-merge (depts widen); deny + managed-only
flags are the hard floor.

```jsonc
"sandbox": {
  "enabled": true,
  "failIfUnavailable": false,        // flip true ONLY after deps installed + T1 green
  "autoAllowBashIfSandboxed": true,
  "allowUnsandboxedCommands": true,  // keep true until excludedCommands proven
  "excludedCommands": [ "/opt/bubble-git-guard/bin/bubble-git-guard *", "sops *" ],
  "filesystem": {
    "denyRead":  [ "/etc/age","/srv/bubble-secrets","/etc/bubble",
                   "/run/claude-agent-tony","/run/claude-agent-maya","/run/claude-agent-cgp",
                   "/run/claude-agent-claudette","/run/claude-agent","/run/gws-tony",
                   "/root","/home/claude/.ssh" ],
    "denyWrite": [ "/etc","/usr/local/bin","/opt/bubble-mission-guard","/opt/bubble-token-broker",
                   "/opt/bubble-git-guard","/home/claude/.claude/settings.json","/home/claude/.bun/bin" ],
    "allowWrite":[ "/home/claude/.bun","/home/claude/.claude/plugins/cache",
                   "/home/claude/.claude/channels" ]  // channels only if poller is a Bash child (T5)
  },
  "network": {
    "allowManagedDomainsOnly": false,  // flip true after domain inventory (T4)
    "allowedDomains": [ "api.anthropic.com","api.telegram.org",
                        "github.com","api.github.com","codeload.github.com","objects.githubusercontent.com",
                        "registry.npmjs.org","*.notion.so","api.notion.com","www.data.gouv.fr" ],
    "allowAllUnixSockets": false       // needs sandbox-runtime installed to actually enforce
  }
}
```
Per-dept `.claude/settings.json` adds only its repo path to `filesystem.allowWrite`
+ any dept-specific `allowedDomains` (array-merge).

## Tests before any live rollout

- **T1** — after installing deps + AppArmor profile, confirm sandbox initializes
  (`/sandbox` deps green; a trivial sandboxed `Bash(ls)` runs).
- **T2** — managed `denyRead:["/etc/age"]` + `sandbox.enabled`, launch
  `--dangerously-skip-permissions --print`, attempt `Bash(cat /etc/age/key.txt)`
  → expect blocked. Also `Bash(curl https://not-allowed.example)` → blocked.
- **T3** — real `bubble-git-guard` push with sandbox on → token mint + push works
  (via excludedCommands) AND a structural push still gets the read-only-token 403
  (mission-lock still functions).
- **T4** — per-dept outbound-domain + out-of-repo-write inventory before flipping
  `allowManagedDomainsOnly`.
- **T5** — is the telegram bun poller a Bash child (needs allowances) or a host
  process (outside the sandbox, needs none)?

## Rollout order

1. Fixture repo (not a live dept): install deps + AppArmor, run T1–T5 via
   user/project settings (NOT managed — a mistake can't brick all 5).
2. Canary ONE dept (recommend maya — onboarding, lowest blast radius; not
   cgp/tony) via its own `.claude/settings.json`, `failIfUnavailable:false`,
   `allowManagedDomainsOnly:false`. Watch a full loop + telegram round-trip + push.
3. Promote validated block into managed-settings (covers all 5 un-overridably),
   still `failIfUnavailable:false`. Watch one cycle.
4. Flip `failIfUnavailable:true` + (after T4) `allowManagedDomainsOnly:true` last.
   Consider `allowUnsandboxedCommands:false` (strict) only once excludedCommands
   proven to cover push/sudo.
5. Rollback = revert the managed `sandbox` block (1 root edit); all other layers
   (deny rules, sops-guard, git-guard, mission-lock) stay intact throughout —
   sandbox is purely additive.

## Caveats to flag to {{OPERATOR}}

- Shared-user: sandbox does NOT give agent-from-agent isolation (that's the
  per-user split that was dropped — see vps-claude-nopasswd-all). This is a
  4th additive layer, not a replacement for the existing boundaries.
- TLS not inspected → allowed domains are exfil vectors (domain fronting).
- `failIfUnavailable:false` fails OPEN (missing dep silently disables sandbox);
  `true` is the real gate but bricks agents if deps absent — ordering matters.
- AppArmor `bwrap` profile re-permits unprivileged userns for that one binary —
  a host-level security change to review.
