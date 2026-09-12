# skill-authoring — deploy (#1222)

The KNOW-HOW authoring + pruning half of the fleet's transcript-mining system.
wiki-compile detects skill gaps (STEP 4.6) and writes knowledge; this skill
**consumes** those candidates and authors (eval-gated) or flags dead skills for
pruning (usage-measured, human-gated). Runs on the VPS (joris-cx33) as user
`claude`, headless `claude -p` against `SKILL.md`, once a week.

## Where it plugs in — reuses cloud-wiki-compile's runner (no new service)

It rides the **existing** `cloud-wiki-compile@.service` templated unit via a new
mode, `skillsmith`, plus one new timer. This is the "reuse the weekly slot" ask
in #1222 — same launcher, same service, same auth-fix/env/logging — while the
actual instructions live in a **separate SKILL** (the doctrine boundary: wiki =
knowledge is cloud-wiki-compile's steps; skills = know-how is this SKILL).

| Mode        | Timer                                  | Cadence                       | Model  |
|-------------|-----------------------------------------|--------------------------------|--------|
| skillsmith  | `cloud-wiki-compile-skillsmith.timer`   | weekly, Sun 23:30 UTC          | haiku  |

Sunday order: synthesis(18:00) → pruning(19:00) → compile+STEP-4.6(22:00) →
skillsmith(23:30). It runs AFTER the 22:00 compile so this week's transcripts are
mined AND 4.6 has written `candidates.md` (which ARM A consumes), and still ON
Sunday so the ISO-week stamp matches the file (a Monday run rolls to the next ISO
week and would miss it). See the timer file for the full rationale.

`cloud-wiki-compile.sh skillsmith` loads `skill-authoring/SKILL.md` (not the wiki
skill), uses Haiku, and skips the shared-wiki git-repo precondition (it works the
skill registry + transcript corpus, not the wiki).

## Live artifact map

```
/home/claude/.claude/skills/skill-authoring/SKILL.md                     <- skills/skill-authoring/SKILL.md
/home/claude/.claude/skills/skill-authoring/scripts/lib/*.py             <- skills/skill-authoring/scripts/lib/*.py
/home/claude/scripts/cloud-wiki-compile.sh                               <- skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh (skillsmith mode added)
/etc/systemd/system/cloud-wiki-compile-skillsmith.timer                  <- deploy/templates/cloud-wiki-compile-skillsmith.timer
/etc/systemd/system/cloud-wiki-compile@.service                          (UNCHANGED — reused)
```

## Manual deploy

```bash
# on joris-cx33, as claude (sudo for the systemd bits):
cd /path/to/bubble-ops-loop
./scripts/install-cloud-wiki-compile.sh   # now also installs the skill-authoring SKILL + skillsmith timer
```

Idempotent. Installs the launcher (0755), both SKILLs, the skill-authoring lib
scripts (0755), the four timers + templated service, then `daemon-reload` +
`enable --now`.

Smoke test after install (a real run — costs a little Haiku budget):
```bash
sudo systemctl start cloud-wiki-compile@skillsmith.service
journalctl -u cloud-wiki-compile@skillsmith.service -f
```

Dry pre-checks that spend **no** tokens (verify the evidence collectors see the
right data on the box):
```bash
python3 /home/claude/.claude/skills/skill-authoring/scripts/lib/list_candidates.py --registry /home/claude/.claude/skills
python3 /home/claude/.claude/skills/skill-authoring/scripts/lib/skill_usage_count.py --registry /home/claude/.claude/skills --window-days 45
python3 /home/claude/.claude/skills/skill-authoring/scripts/lib/eval_harness.py --draft /home/claude/.claude/skills/skill-authoring/SKILL.md --dry-run
```

## Dependencies / ordering

- Depends on wiki-compile **STEP 4.6** (already on `main`) writing
  `/home/claude/monitoring/skill-updates/{ISO_YEAR}-W{ISO_WEEK}-workaround-candidates.md`.
  ARM A degrades gracefully (does nothing) if that file is absent.
- Depends on `emit_kanban_item.sh` (present) and, for nudges, the memory-hygiene
  notifier routing (present).

## Honest status — implemented vs needs live verify

- **Implemented + unit-tested:** the two evidence collectors (`list_candidates.py`,
  `skill_usage_count.py`) and the eval harness's plumbing (`--dry-run` path).
- **Needs live verify on the VPS (could not run here):** an end-to-end
  `skillsmith` run under systemd; that the corpus/registry/candidates paths
  resolve on the box; that `eval_harness.py` executes real Haiku WITH/WITHOUT
  runs within budget; that `--append-system-prompt` is the right isolation lever
  for the eval (vs. staging into a temp skills dir) — confirm on first run.
- **Not deployed by this PR** (reviewed-PR only, per mission): nothing is
  installed on the VPS; run `install-cloud-wiki-compile.sh` after merge.
