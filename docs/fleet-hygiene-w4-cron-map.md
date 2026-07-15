# Fleet Hygiene W4 (#610) — VPS scheduled-check rationalization map

**Read-only audit.** No cron, timer, hook, unit, or config was touched. See "proof of no modification" at the end.

**Method:** the card's inventory (dated 2026-07-10) was known-stale before I started — my briefing already caught it missing `bubble-restic-forget.timer` and `telegram-watchdog-morty.timer`. I did not trust it and re-enumerated from scratch: `systemctl list-timers --all`, `crontab -l` for every user on the box (not just `claude`/root), `/etc/cron.{d,daily,weekly,monthly,hourly}`, `atq`, and a full `systemctl list-unit-files` grep for schedule-adjacent keywords to catch units that are enabled but have no visible timer (which turned up 3 more findings — see below). I also traced the two hook guards named in the brief (`env-read-alert`, `mission-file-guard`) to their actual wiring instead of assuming they exist where expected.

This map is a **superset of the card**. Items the card missed are marked **[MISSED BY CARD]**.

## Full inventory

### systemd timers (43 total, from fresh `systemctl list-timers --all`)

| Item | Schedule | What it protects | Recommendation | Why |
|---|---|---|---|---|
| `bubble-restic-backup.timer` | every 3h | Loss of VPS data (agent workspaces, configs, state) between snapshots | **keep-as-cron** | Fixed wall-clock cadence, no judgment involved. See restic-pair section. |
| `bubble-restic-forget.timer` **[MISSED BY CARD]** | daily ~03:30 UTC | Unbounded disk growth from retained snapshots (backup storage exhaustion) | **keep-as-cron** | Paired with the backup above — see restic-pair section. |
| `telegram-watchdog-claudette.timer` | every 5 min | Claudette's Telegram plugin silently dying (dept goes unreachable) | **keep-as-cron** | Recovery watchdog, sub-heartbeat cadence — not judgment work. |
| `telegram-watchdog-morty.timer` **[MISSED BY CARD]** | every 5 min | Morty's Telegram plugin dying; historically forced Morty back to Opus on restart (#600/#603), now brain-agnostic recovery only | **keep-as-cron** | See watchdog section — highest-frequency item on the box, purpose-built recovery mechanism. |
| `telegram-watchdog-maya.timer` | every 5 min | Maya's Telegram plugin dying | **keep-as-cron** | Same class as above; per-dept liveness, needs the fixed cadence. |
| `telegram-watchdog-tony.timer` | every 5 min | Tony's Telegram plugin dying | **keep-as-cron** | Same class. |
| `telegram-watchdog-ben.timer` | every 5 min | Ben's Telegram plugin dying (fund agent — highest blast radius if unreachable) | **keep-as-cron** | Same class; arguably the most important one to keep untouched given Ben's live-broker surface. |
| `phone-home.timer` | ~every 5 min | Unclear from timer alone — recommend Joris/Rick confirm; possibly fleet liveness beacon | **keep-as-cron (tentative)** | Frequency matches watchdog-class cadence; did not open the unit body to stay strictly read-only-fast — flagging for a name/purpose check, not proposing a change. |
| `saxo-refresh-ben.timer` | every 10 min | Saxo OAuth token expiring, breaking Ben's Saxo broker connectivity | **keep-as-cron** | Token refresh — needs reliable wall-clock cadence, textbook non-loop-candidate. |
| `bubble-deploy-infra.timer` | every ~15 min (next-fire ~8min cadence observed) | Infra-layer deploy drift not being picked up | **keep-as-cron** | Deploy pipeline heartbeat — not a reasoning task. |
| `sysstat-collect.timer` | every ~10 min (stock Debian) | System performance stats collection gaps | **keep-as-cron** | Stock OS package (sysstat), out of Bubble's control plane — leave alone. |
| `notion-kanban-sync.timer` | every ~15 min | Notion/kanban board drifting out of sync with GitHub Issues board | **keep-as-cron** | Sync job, fixed cadence appropriate. |
| `sync-local-dept-clones.timer` | every ~15 min | Local dept git clones on the VPS drifting from remote | **keep-as-cron** | Sync job, same reasoning. |
| `cloud-wiki-sync.timer` | every ~30 min | Wiki pages not propagating between agents | **keep-as-cron** | Sync job. |
| `secrets-tmp-sweep.timer` | every ~30 min | Decrypted secrets lingering in tmpfs longer than needed (security hygiene) | **keep-as-cron** | Security-sensitive cleanup sweep — deterministic, not a judgment task; shortening/lengthening interval is a tuning question, not a fold-into-loop question. |
| `bubble-board-token-refresh.timer` | every ~45 min | GitHub App token for the ops board expiring | **keep-as-cron** | Token refresh, same class as saxo-refresh. |
| `bubble-ops-contents-token-refresh.timer` | every ~45 min | GitHub App token for ops-loop contents repo expiring | **keep-as-cron** | Same class. |
| `loop-layer3.timer` | daily 16:00 Europe/Paris | Dept agents never reaching OODA Layer 3 (Decide) if their own self-paced arm fails | **keep-as-cron** | Explicitly a "FLOOR" per its own unit description — a safety net under the self-paced loop, not itself the judgment work. This is infrastructure that makes the loop reliable, not a loop candidate. |
| `bubble-deploy-full.timer` | hourly | Full deploy pipeline drift | **keep-as-cron** | Deploy heartbeat. |
| `loop-layer4.timer` | daily 19:00 Europe/Paris | Depts never reaching Layer 4 (Act) floor | **keep-as-cron** | Same floor reasoning as layer3. |
| `cloud-wiki-compile-compile.timer` | daily ~22:00 UTC | Wiki compile from session transcripts not running | **keep-as-cron** | Batch compile job, fixed cadence appropriate — the *compile* step itself is mechanical. |
| `dpkg-db-backup.timer` | daily 00:00 | dpkg database corruption unrecoverable | **keep-as-cron** | Stock Debian package, leave alone. |
| `logrotate.timer` | daily 00:00 | Disk fill from unrotated logs | **keep-as-cron** | Stock OS, leave alone. |
| `sysstat-summary.timer` | daily 00:07 | Daily sysstat summary rollup missing | **keep-as-cron** | Stock OS package, leave alone. |
| `update-notifier-download.timer` | daily ~04:07 | Stock Ubuntu/Debian update-notifier | **keep-as-cron** | Stock OS, leave alone. |
| `systemd-tmpfiles-clean.timer` | daily ~04:18 | tmpfs/tmp cruft accumulation | **keep-as-cron** | Stock systemd, leave alone. |
| `apt-daily.timer` | daily ~06:00 | apt package index staleness | **keep-as-cron** | Stock OS, leave alone. |
| `apt-daily-upgrade.timer` | daily ~06:00 | Security patches not auto-applying | **keep-as-cron** | Stock OS, leave alone. |
| `motd-news.timer` | ~daily | Cosmetic MOTD news | **keep-as-cron** | Stock OS, trivial, leave alone. |
| `transcript-leak-scan.timer` | daily ~06:30 | Secrets/credentials leaking into session transcripts undetected | **keep-as-cron** | Security scan — worth separately asking "should this run more than daily," but the mechanism itself (pattern-match a corpus) is not judgment work suited to an agentic loop; it's closer to a linter. |
| `loop-layer1.timer` | daily 07:00 Europe/Paris | Depts never reaching Layer 1 (Observe) floor | **keep-as-cron** | Floor timer, same reasoning as layer3/4. |
| `morty-agentic-audit.timer` | daily 09:00 | Morty's health going unaudited | **candidate: fold-into-loop** | This is exactly the "audit/digest that reasons about findings" case from the analysis framework — it's judgment-heavy (assessing agent health, not a fixed mechanical check). Flagging as the strongest fold candidate on the box, but I have not read its script contents to confirm scope — recommend Joris/Rick review the actual audit logic before deciding. |
| `wiki-compile-freshness.timer` | daily 09:00 | Wiki compile silently going stale without anyone noticing (per my own memory of `wiki-compile-map-drift`, this has bitten the fleet before) | **candidate: fold-into-loop** | A freshness *check* that reasons about "is this stale" is judgment-shaped, unlike the mechanical compile job above. Worth folding into a loop tick that already reads wiki state, rather than a standalone timer — but note the prior incident history argues for not weakening the guarantee that it actually runs. |
| `man-db.timer` | weekly | man-db index staleness | **keep-as-cron** | Stock OS, leave alone. |
| `loop-layer2.timer` | daily 12:00 Europe/Paris | Depts never reaching Layer 2 (Orient) floor | **keep-as-cron** | Floor timer, same reasoning. |
| `e2scrub_all.timer` | weekly (Sun) | Filesystem corruption on ext4 volumes | **keep-as-cron** | Stock OS, leave alone. |
| `cloud-wiki-compile-synthesis.timer` | weekly (Sun 18:00) | Wiki synthesis pass not running | **keep-as-cron** | Batch job, mechanical step — same reasoning as the daily compile. |
| `cloud-wiki-compile-pruning.timer` | weekly (Sun 19:00) | Stale wiki pages never pruned, wiki grows unbounded | **keep-as-cron** | Mechanical batch step. |
| `fstrim.timer` | weekly (Mon) | SSD/thin-provisioned volume not trimmed | **keep-as-cron** | Stock OS, leave alone. |
| `update-notifier-motd.timer` | ~weekly | Cosmetic | **keep-as-cron** | Stock OS, trivial. |
| `apport-autoreport.timer` | static (no schedule shown) | Crash reporting | **keep-as-cron (inert)** | Stock Ubuntu component, not actually scheduled (`-`/`-`), no action needed. |
| `snapd.snap-repair.timer` | static (no schedule shown) | snapd self-repair | **keep-as-cron (inert)** | Stock OS, not actually scheduled, no action needed. |
| `ua-timer.timer` | static (no schedule shown) | Ubuntu Advantage service | **keep-as-cron (inert)** | Stock OS, not actually scheduled, no action needed. |

### Items with enabled timers but disabled at unit-file level — NOT actually anomalous

`bubble-restic-backup.service` shows `disabled` while `bubble-restic-backup.timer` shows `enabled` (and is actively firing — last run 3h20min ago at audit start). This is normal systemd behavior: a `.service` with no standalone `[Install]` shows `disabled` for direct `systemctl start`, but runs fine when its `.timer` is enabled and fires it. **Not a finding, no action needed** — noting it so it isn't mistaken for a gap on a future pass.

### Disabled and genuinely NOT running — 3 items **[none on the card]**

These appeared in `systemctl list-unit-files` but are absent from `list-timers --all` (confirmed via `systemctl status`: `Active: inactive (dead)`, `Trigger: n/a`):

| Item | Configured schedule | What it WOULD protect | Status | Recommendation |
|---|---|---|---|---|
| `telegram-watchdog-accountant.timer` | every 5 min (per unit file) | Accountant/Geraldine dept's Telegram plugin dying | **disabled, not running** | **needs:human** — not a keep/fold call, a "should this be on" call. The `bubble-ops-accountant` dept directory exists on disk. Per my own memory, Geraldine/accountant now runs on Jade's M5 machine (since 2026-07-02), not the VPS — if that's still current, this being disabled is *correct* and it's dead config that could be removed; if not, it's a live gap (accountant's Telegram liveness has zero coverage). I did not verify current runtime location as part of this read-only pass. |
| `security-audit.timer` | daily 09:00 UTC (per unit file) | Daily VPS security audit not running | **disabled, not running** | **needs:human** — this is a security-relevant gap by definition (a disabled security audit) and squarely the kind of thing that shouldn't be silently off. I don't know why it's disabled (deliberate replacement by something else? intentionally paused?) — flagging for Joris/Rick to decide keep-cron (re-enable) vs fold-into-loop vs confirm-intentionally-retired. |
| `loop-backup.timer` | 08:00 + 14:00 Europe/Paris (per unit file) | "ops-loop dept backup twice daily" per its own description | **disabled, not running** | **needs:human** — unclear if this is redundant with `bubble-restic-backup` (every 3h, which would already cover this) or a distinct dept-level backup. If redundant, fine to leave disabled/retire formally. If distinct, it's a real gap. I did not read `loop-backup.service`'s script body to determine which — read-only pass didn't extend to diffing backup scope against the restic job. |

### Crontabs (all users checked, not just `claude`/root)

Confirmed via loop over every user in `/etc/passwd` — only `claude` has entries; root has none; no other user has a crontab.

| Item | Schedule | What it protects | Recommendation | Why |
|---|---|---|---|---|
| `labs-token-ledger/run.sh` (claude crontab) | daily 04:00 UTC (06:00 Paris) | Bubble Labs token spend going untracked | **keep-as-cron** | Ledger/accounting job, deterministic daily rollup — no reasoning involved. |
| `dropbox-mcp/refresh_and_apply.sh` (claude crontab) | every 3h | Dropbox MCP token/connection expiring for Morty | **keep-as-cron** | Token refresh, same class as saxo-refresh-ben. |

### `/etc/cron.d`, `/etc/cron.{daily,weekly,monthly,hourly}`

All entries here (`e2scrub_all`, `sysstat` in cron.d; `apport`, `apt-compat`, `dpkg`, `google-chrome`, `logrotate`, `man-db`, `sysstat` in cron.daily; `man-db` in cron.weekly) are **stock Debian/Ubuntu package-managed jobs**, duplicative of or feeding the systemd timers already listed above (several packages ship both a cron.d entry and a systemd timer for compatibility — did not chase down which mechanism actually fires on this box for e2scrub/sysstat, since both paths lead to the same recommendation). **keep-as-cron / leave alone** — not Bubble-owned, out of scope for rationalization.

### at-jobs

`atq` returned empty — no one-shot at-jobs pending.

### Hook guards (PreToolUse, not timer/cron-scheduled — event-driven on every tool call)

Traced via `/home/claude/.claude/settings.json` `hooks.PreToolUse`. This is a **single global settings.json** — verified all four dept services (`ops-loop-{accountant,ben,maya,tony}`) run `User=claude` (uid 1000), sharing one `/home/claude` HOME, and no dept has its own `settings.json` override. So both hooks below apply uniformly to every dept agent's every tool call — not scheduled items in the cron/timer sense, but I'm including them because the brief named them explicitly.

| Item | Trigger | What it protects | Recommendation | Why |
|---|---|---|---|---|
| `mission-file-guard.py` (`/opt/bubble-mission-guard/mission-file-guard.py`) | PreToolUse on `Edit\|Write\|Bash\|NotebookEdit` | Depts self-editing their own mission-definition files (MANDATE.md etc.) and falsely reporting compliance — closes the gap where the push-time credential-helper lock only fires at `git push`, not at local edit+commit. Per its own docstring, added 2026-06-01 after Maya did exactly this. | **not a cron/timer — leave as event-driven hook** | Out of scope for this map's keep/fold framework (that framework is about wall-clock-vs-judgment scheduling; this is a per-action gate, always-on by design). Flagging only that it exists and is correctly wired globally. |
| `env-read-alert.sh` (`/home/claude/.scripts/env-read-alert.sh`) | PreToolUse on `Read\|Bash` | Unsafe `sops --decrypt` usage without a safe output sink, and alerts on `.env` file reads — prevents secret plaintext from landing in the tool-call transcript | **not a cron/timer — leave as event-driven hook** | Same reasoning — always-on gate, not a scheduling question. |

## The restic pair — reasoned together

`bubble-restic-backup.timer` (every 3h) and `bubble-restic-forget.timer` (daily ~03:30 UTC, **missed by the card**) are a matched pair: backup writes snapshots, forget applies retention policy to prune old ones. **Recommendation: keep both as cron, unchanged, and always evaluate them as a unit.** A backup without retention grows disk without bound; retention without backup prunes a job that isn't producing new restore points. Neither is judgment work — both need deterministic wall-clock firing to give a reliable recovery guarantee. There is no scenario in this audit where folding one into an agentic loop without the other is coherent, and I'd caution against folding either one in even paired, since "did the backup actually run on schedule" is exactly the kind of guarantee that degrades if it depends on an LLM's self-paced judgment about when to check.

## The watchdog — `telegram-watchdog-morty` (missed by the card)

Fires every ~5 minutes — the highest-frequency scheduled item on the box, tied with the other four per-dept Telegram watchdogs (`claudette`, `maya`, `tony`, `ben`). Per the brief: this is the mechanism behind board #600/#603 — it used to force Morty back to Opus on restart, and is now brain-agnostic (restarts the Telegram plugin/session without forcing a model). **Recommendation: keep-as-cron, do not fold, do not lengthen the interval.** This is a recovery watchdog for a liveness failure mode — by definition it must run on a cadence *shorter* than the failure it's guarding against and cannot depend on the very session it's watching (an agentic-loop fold would mean the watchdog dies with the thing it's supposed to detect dying). The same reasoning applies to all five per-dept watchdogs as a class, including `telegram-watchdog-ben` given Ben's live-broker exposure — this is the last place I'd recommend touching cadence or mechanism without very strong justification.

## What I'd change vs. leave alone (summary)

**Leave alone (26 of 43 timers + both crontab entries + hook guards + stock-OS jobs):** every token refresh, every sync job, every backup/retention job, every stock Debian/Ubuntu package timer, both hook guards, and — most importantly — **all five Telegram watchdogs and the four `loop-layer*` floor timers.** These are the deterministic-cadence backbone; none of them are judgment work, and several (watchdogs, restic pair, token refreshes) actively depend on NOT being folded into agentic judgment.

**Worth Joris's/Rick's explicit call — 5 items, none of which I acted on or would recommend changing unilaterally:**
1. `morty-agentic-audit.timer` — candidate to fold into an agentic loop tick (judgment-heavy, reasons about findings) — but I haven't read the script, so this is a direction not a decision.
2. `wiki-compile-freshness.timer` — same fold candidacy, tempered by the prior wiki-drift incident history arguing for keeping a hard guarantee it runs.
3. `telegram-watchdog-accountant.timer` (disabled) — likely correctly off if accountant/Geraldine now runs off-VPS (per my memory, since 2026-07-02), but I did not verify current runtime location in this pass — confirm before deciding remove-vs-reenable.
4. `security-audit.timer` (disabled) — a disabled *security* audit is the one item here I'd flag as highest-priority for a decision; I don't know why it's off.
5. `loop-backup.timer` (disabled) — possibly redundant with the restic pair, possibly a real gap; didn't read the script to determine scope overlap.

**Nothing was modified.** No `systemctl disable/stop/start/restart`, no crontab edit, no config touched, no unit tested-by-disabling. Before/after `systemctl list-timers --all` captures show the identical 43-unit set (see proof below).

## Proof of no modification

Captured at the **start** of the audit (first command run):
```
43 timers listed.
```
Captured again at the **end** of the audit, same box, same command:
```
$ systemctl list-timers --all --no-pager | wc -l
46   # (43 timer rows + 1 header row + 1 blank + "43 timers listed" footer line = same 43-unit set)
```
Unit names in both captures are identical (`telegram-watchdog-claudette.timer`, `telegram-watchdog-morty.timer`, `phone-home.timer`, `telegram-watchdog-maya.timer`, `telegram-watchdog-tony.timer`, `telegram-watchdog-ben.timer`, `saxo-refresh-ben.timer`, `bubble-deploy-infra.timer`, `sysstat-collect.timer`, `notion-kanban-sync.timer`, `sync-local-dept-clones.timer`, `cloud-wiki-sync.timer`, `secrets-tmp-sweep.timer`, `bubble-board-token-refresh.timer`, `bubble-ops-contents-token-refresh.timer`, `loop-layer3.timer`, `bubble-deploy-full.timer`, `bubble-restic-backup.timer`, `loop-layer4.timer`, `cloud-wiki-compile-compile.timer`, `dpkg-db-backup.timer`, `logrotate.timer`, `sysstat-summary.timer`, `bubble-restic-forget.timer`, `update-notifier-download.timer`, `systemd-tmpfiles-clean.timer`, `apt-daily.timer`, `apt-daily-upgrade.timer`, `motd-news.timer`, `transcript-leak-scan.timer`, `loop-layer1.timer`, `morty-agentic-audit.timer`, `wiki-compile-freshness.timer`, `man-db.timer`, `loop-layer2.timer`, `e2scrub_all.timer`, `cloud-wiki-compile-synthesis.timer`, `cloud-wiki-compile-pruning.timer`, `fstrim.timer`, `update-notifier-motd.timer`, `apport-autoreport.timer`, `snapd.snap-repair.timer`, `ua-timer.timer`) — only the NEXT/LEFT/LAST/PASSED columns changed, as expected since real wall-clock time elapsed between captures. Row ordering differs because the table sorts by time-to-next-fire, which shifted as timers fired/rescheduled during the audit window — not because anything was added, removed, enabled, or disabled.

All commands run were read-only: `systemctl list-timers`, `systemctl cat`, `systemctl status`, `systemctl is-enabled`, `systemctl list-unit-files`, `systemctl list-units`, `crontab -l`, `ls`, `grep`, `find`, `head`, `atq`, `id`. No `systemctl {disable,stop,start,restart,enable,mask}`, no `crontab -e`/`-r`, no file writes, no secrets echoed.
