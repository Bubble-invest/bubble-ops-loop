// boot_rearm.ts — ops-loop /loop self-re-arm on poller startup.
//
// Problem ({{OPERATOR}} 2026-06-01): a systemd restart of an ops-loop dept agent
// does NOT re-arm its /loop. bubble-loop-reinit.sh tried to fix this by
// having the bot send a Telegram message, but a bot's own outbound message
// never returns as an inbound update (Telegram API), so the loop stays dead
// and the agent looks active while being silently idle.
//
// Fix: on poller startup the telegram plugin self-injects ONE synthetic
// "boot" turn straight into Claude via the same MCP channel notification
// that a real inbound message uses — bypassing Telegram entirely. That turn
// makes the agent run session-start + re-register its /loop.
//
// SAFETY: only fires when explicitly opted in via OPS_LOOP_BOOT_REARM=1
// (set in the dept systemd unit). Interactive Mac/Telegram sessions must
// NEVER auto-fire — injecting a phantom prompt into a human chat is exactly
// what we must not do.

export interface BootRearmNotification {
  method: "notifications/claude/channel"
  params: {
    content: string
    meta: {
      user_id: "system"
      source: "ops-loop-boot-rearm"
      ts: string
      dept?: string
    }
  }
}

/**
 * Decide whether to fire a boot re-arm turn, and build its payload.
 *
 * @param env  process.env (or a subset) — only OPS_LOOP_BOOT_REARM and
 *             OPS_LOOP_DEPT are read.
 * @returns the notification to fire, or null to do nothing.
 */
export function bootRearmNotification(
  env: Record<string, string | undefined>,
): BootRearmNotification | null {
  if (env.OPS_LOOP_BOOT_REARM !== "1") return null

  const dept = env.OPS_LOOP_DEPT
  const content =
    "[boot] Service (re)started — re-arm your /loop, SELF-PACED. " +
    "This is a system boot signal, not an operator instruction. " +
    "You woke via --resume so you have full context. " +
    "FIRST run ONE normal session-start + dispatch tick now — do this unconditionally on boot/heal (the floor timers are a net, not a substitute). " +
    "THEN " +
    "arm your OWN next wake with a single CronCreate (run CronList first and delete any stale/duplicate loop task so you never stack two). " +
    "The box's clock is UTC, not Paris: NEVER hand-write a Paris HH:MM as the cron literal (board #850 — treating 08:03 Paris as `3 8 * * *` fired a live market order 2h late). For any Paris-anchored target, derive the box-UTC cron via `scripts/arm-wake-cron.sh <Paris-HH:MM> [daily|one-shot]` (DST-safe — reads the tz database, never a hardcoded offset) and CronCreate the expression it prints. Sanity-check with `TZ=Europe/Paris date` at the top of every tick rather than trusting a stale header. " +
    "The CronCreate prompt is GENERATED, never authored by you (board #1483/#1484 — a self-written wake note is exactly how an uncited 'operator flagged spend — be cost-conscious' claim self-reinforced across ~36 wake prompts and silently dropped a mission deliverable for 12 days). Run `due_missions.py wake-prompt --dept-dir <your dept repo root>` — the same generator your floor/backup tick already uses (it lives in your host's bubble-ops-loop checkout, e.g. `/opt/bubble-ops-loop/scripts/due_missions.py` on the VPS or the sibling `bubble-ops-loop` checkout next to your dept workspace on Mac; ask your wiki/HANDOFF if unsure). " +
    "IF it exits 0: pass its ENTIRE stdout to CronCreate as the prompt, VERBATIM — never compose, paraphrase, edit, or append your own wording. Its output is guaranteed to be your full tick protocol text and NEVER a bare slash-command like /loop-now (a bare slash-command delivers as a malformed inbound that can trip the deaf-watchdog) — that invariant is now enforced by the generator, not by your own drafting. " +
    "IF it exits non-zero or prints nothing (FAIL-CLOSED by design — e.g. your dept.yaml doesn't yet use the loop.due_dispatch schema this generator understands, or nothing is live/due right now): do NOT retry composing your own DUE_MISSIONS text and do NOT treat empty output as 'nothing to do' — instead CronCreate your own full tick protocol text (STEP A-F per CLAUDE.md, the pre-#1484 behavior) for THIS arm only, AND record one line in your HEARTBEAT ONLY (never Telegram — this is a KNOWN gap on VPS until its follow-up lands, and every re-arm/compaction firing on Telegram would spam the operator) naming the wake-prompt failure (e.g. 'wake-prompt generator refused for this dept — used fallback text, see #1484') so the gap stays visible without paging anyone. " +
    "work pending or a layer still due today -> schedule toward that layer time; quiet but more may come today -> a longer cadence is fine (e.g. 0 */2 * * *, TZ-neutral); " +
    "all 4 layers done and nothing explicitly awaited -> run `scripts/arm-wake-cron.sh 08:03 one-shot` for the correct box-UTC one-shot and arm nothing else. " +
    "Never hardcode an hourly cron. Your loop-layer floor timers remain the safety net. " +
    "Do not reply to a human; just resume cadence."

  return {
    method: "notifications/claude/channel",
    params: {
      content,
      meta: {
        user_id: "system",
        source: "ops-loop-boot-rearm",
        ts: new Date().toISOString(),
        ...(dept ? { dept } : {}),
      },
    },
  }
}
