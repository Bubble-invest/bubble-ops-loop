# cloud-wiki-compile — deploy

Fleet-wide job that mines every agent's transcripts into the shared wiki
(`~/.claude/agent-memory/shared-wiki/`). Runs on the VPS (joris-cx33) as user
`claude`, headless `claude -p` against `SKILL.md`. Three modes, three timers:

| Mode        | Timer                                | Cadence                       |
|-------------|---------------------------------------|--------------------------------|
| compile     | `cloud-wiki-compile-compile.timer`    | nightly, 22:00 UTC             |
| synthesis   | `cloud-wiki-compile-synthesis.timer`  | weekly, Sun 18:00 UTC (opus)   |
| pruning     | `cloud-wiki-compile-pruning.timer`    | weekly, Sun 19:00 UTC          |

All three timers drive one templated service, `cloud-wiki-compile@.service`
(`%i` = mode), which execs `/home/claude/scripts/cloud-wiki-compile.sh %i`.

## This card (#627) — versioning only

This PR gives the job a **versioned home**. It does **not** change the live
deploy path or touch the running VPS copy. The files under
`skills/cloud-wiki-compile/` and `deploy/templates/cloud-wiki-compile*` in
this repo are byte-identical to what is running live on joris-cx33 as of
2026-07-16 (verified via direct diff, not assumed). Cutover — pointing the
VPS at THIS repo's vendoring path instead of hand-copy — is a follow-up card,
sequenced after this one merges (see #627 comments: Option A, narrow scope).

## Live artifact map (verified 2026-07-16)

```
/home/claude/.claude/skills/cloud-wiki-compile/SKILL.md   <- skills/cloud-wiki-compile/SKILL.md
/home/claude/scripts/cloud-wiki-compile.sh                <- skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh
/etc/systemd/system/cloud-wiki-compile@.service           <- deploy/templates/cloud-wiki-compile@.service
/etc/systemd/system/cloud-wiki-compile-compile.timer      <- deploy/templates/cloud-wiki-compile-compile.timer
/etc/systemd/system/cloud-wiki-compile-synthesis.timer    <- deploy/templates/cloud-wiki-compile-synthesis.timer
/etc/systemd/system/cloud-wiki-compile-pruning.timer      <- deploy/templates/cloud-wiki-compile-pruning.timer
/etc/systemd/system/cloud-wiki-compile@.service.d/onfailure.conf   (NOT included — see below)
```

`onfailure.conf` drop-in (`OnFailure=cron-failure-alert@%n.service`) wires the
service to the shared failure alerter used by other crons on the box. It is
**not vendored in this PR** — it isn't specific to this job, and copying it
here risks drifting from whatever manages that shared alerting convention
fleet-wide. Left as a live-VPS-only artifact for now; flag if it should move
too.

Adjacent but **out of scope**: `cloud-wiki-sync.sh` / `cloud-wiki-sync.timer`
(a different, more frequent 15-minute job also on the VPS) is NOT part of
this card — do not confuse it with `cloud-wiki-compile`.

## Manual deploy (until wired into the automatic vendor/revendor path)

```bash
# on joris-cx33, as claude (with sudo for the systemd bits):
cd /path/to/bubble-ops-loop
./scripts/install-cloud-wiki-compile.sh
```

This is idempotent — installs the launcher script (0755), the SKILL (0644),
the three timers + templated service under `/etc/systemd/system`, then
`daemon-reload` + `enable --now` on all three timers.

Smoke test after install:
```bash
sudo systemctl start cloud-wiki-compile@compile.service
journalctl -u cloud-wiki-compile@compile.service -f
```

## Drift note

At the time of this PR, the **live VPS copy had already drifted ahead of**
the previously-tracked copy in `vdk888/bubble-rnd-workspace`
(`projects/cloud-wiki-compile/vps/`) in two places:

1. `SKILL.md` — the "PRUNING STEP" section on live reflects the pre-#653
   doctrine (detect-only, no self-heal); the personal-repo copy already had
   the post-#653 self-heal text. Live is what's actually running, so live
   wins here — this PR's SKILL.md matches live, NOT the personal-repo copy.
2. `cloud-wiki-compile.sh` — live writes its run log to
   `/home/claude/logs/bubble-wiki` (claude-owned; the sudo/root-owned
   `/var/log/bubble-wiki` path failed silently, per a 2026-06-20 fix noted
   inline). The personal-repo copy still has the old sudo-based
   `/var/log/bubble-wiki` write path.

This is exactly the kind of silent divergence card #627 exists to end —
this PR captures live as the one true source going forward.

## Follow-up (not this card)

- Wire this into `scripts/revendor-all-depts.sh` (or a sibling) so
  `/home/claude/.claude/skills/cloud-wiki-compile/` and
  `/home/claude/scripts/cloud-wiki-compile.sh` are kept in sync from this
  repo automatically instead of via manual `install-cloud-wiki-compile.sh`
  runs.
- Retire the personal-repo copy in `vdk888/bubble-rnd-workspace` once this
  is confirmed as the source of truth (leave a pointer, don't delete
  history).
- Decide the fate of `onfailure.conf` (vendor it too, or document why not).
