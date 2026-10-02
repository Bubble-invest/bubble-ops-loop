// choices.block.ts — CANONICAL source for the "choices" (inline-button) telegram-plugin patch.
//
// Gives the plugin's `reply` tool an optional `choices: string[]` param. The sent message gets an
// InlineKeyboard (one button per choice, rows of <=2). Tapping a button edits the message
// ("✅ <label>", keyboard removed) and delivers a `[choice] <label>` inbound turn to the session.
//
// NOT executable on its own: scripts/apply-choices-patch.py inserts the region between the
// BEGIN/END markers into server.ts just BEFORE the permission-callback handler comment, and makes
// 3 tiny marked edits (tool schema, reply send, callback_query dispatch). It relies on names already
// in server.ts: bot, mcp, InlineKeyboard, Context, STATE_DIR, join, readFileSync, writeFileSync,
// renameSync, randomBytes, loadAccess.
// === BUBBLE-CHOICES PATCH BEGIN ===
// ── pure helpers (unit-tested in tests/test_choices_patch.ts) ────────────────
const CHOICE_MAX = 8
const CHOICE_LABEL_MAX = 40
const CHOICE_TTL_MS = 7 * 24 * 3600 * 1000
const CHOICE_STORE_CAP = 200
type ChoiceEntry = {
  chat_id: string
  message_id: number
  text: string
  parse_mode?: 'MarkdownV2'
  labels: string[]
  ts: number
  answered?: string
}
function normalizeChoices(raw: unknown): string[] {
  if (raw == null) return []
  if (!Array.isArray(raw)) throw new Error('choices must be an array of strings')
  const out = raw.map(x => String(x ?? '').replace(/\s+/g, ' ').trim()).filter(x => x.length > 0)
  if (out.length > CHOICE_MAX) throw new Error(`choices: max ${CHOICE_MAX} entries`)
  return out.map(x => (x.length > CHOICE_LABEL_MAX ? x.slice(0, CHOICE_LABEL_MAX - 1) + '…' : x))
}
function choiceRows(n: number): number[][] {
  const rows: number[][] = []
  for (let i = 0; i < n; i += 2) rows.push(i + 1 < n ? [i, i + 1] : [i])
  return rows
}
function choiceData(id: string, idx: number): string {
  return `ch:${id}:${idx}`
}
function parseChoiceData(data: string): { id: string; idx: number } | null {
  const m = /^ch:([a-z0-9]{4,12}):(\d{1,2})$/.exec(data)
  return m ? { id: m[1]!, idx: Number(m[2]) } : null
}
function escapeMdV2(s: string): string {
  return s.replace(/[_*\[\]()~`>#+\-=|{}.!\\]/g, '\\$&')
}
function pruneChoices(store: Record<string, ChoiceEntry>, now: number): Record<string, ChoiceEntry> {
  const live = Object.entries(store).filter(([, e]) => now - e.ts < CHOICE_TTL_MS)
  live.sort((a, b) => b[1].ts - a[1].ts)
  return Object.fromEntries(live.slice(0, CHOICE_STORE_CAP))
}
// ── state: in-memory Map + JSON persisted under the channel STATE dir ────────
const CHOICES_FILE = join(STATE_DIR, 'choices.json')
const choiceMap = new Map<string, ChoiceEntry>()
function loadChoicesFile(): Record<string, ChoiceEntry> {
  try { return JSON.parse(readFileSync(CHOICES_FILE, 'utf8')) } catch { return {} }
}
function saveChoicesFile(): void {
  try {
    const store = pruneChoices({ ...loadChoicesFile(), ...Object.fromEntries(choiceMap) }, Date.now())
    const tmp = CHOICES_FILE + '.tmp'
    writeFileSync(tmp, JSON.stringify(store), { mode: 0o600 })
    renameSync(tmp, CHOICES_FILE)
  } catch (e) {
    process.stderr.write(`telegram choices: persist failed (non-fatal): ${String(e)}\n`)
  }
}
function getChoice(id: string): ChoiceEntry | undefined {
  return choiceMap.get(id) ?? (loadChoicesFile()[id] as ChoiceEntry | undefined)
}
function newChoiceKeyboard(id: string, labels: string[]): InlineKeyboard {
  const kb = new InlineKeyboard()
  const rows = choiceRows(labels.length)
  rows.forEach((row, r) => {
    for (const i of row) kb.text(labels[i]!, choiceData(id, i))
    if (r < rows.length - 1) kb.row()
  })
  return kb
}
function newChoiceId(): string {
  return randomBytes(5).toString('hex').slice(0, 7)
}
function registerChoice(id: string, entry: ChoiceEntry): void {
  choiceMap.set(id, entry)
  saveChoicesFile()
}
async function handleChoiceTap(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data ?? ''
  const parsed = parseChoiceData(data)
  const access = loadAccess()
  const senderId = String(ctx.from?.id)
  if (!access.allowFrom.includes(senderId)) {
    await ctx.answerCallbackQuery({ text: 'Not authorized.' }).catch(() => {})
    return
  }
  const entry = parsed ? getChoice(parsed.id) : undefined
  const label = parsed && entry ? entry.labels[parsed.idx] : undefined
  const msg = ctx.callbackQuery?.message
  if (!parsed || !entry || label == null || (msg && String(msg.chat.id) !== entry.chat_id)) {
    await ctx.answerCallbackQuery({ text: 'Choix expiré' }).catch(() => {})
    return
  }
  if (entry.answered != null) {
    await ctx.answerCallbackQuery({ text: 'Déjà répondu' }).catch(() => {})
    return
  }
  // mark first (no double answers), persist, then edit + deliver
  entry.answered = label
  choiceMap.set(parsed.id, entry)
  saveChoicesFile()
  await ctx.answerCallbackQuery({ text: `✅ ${label}` }).catch(() => {})
  const suffix = entry.parse_mode === 'MarkdownV2' ? `\n\n✅ ${escapeMdV2(label)}` : `\n\n✅ ${label}`
  await ctx
    .editMessageText(entry.text + suffix, {
      ...(entry.parse_mode ? { parse_mode: entry.parse_mode } : {}),
      reply_markup: new InlineKeyboard(),
    })
    .catch(() => ctx.editMessageReplyMarkup({ reply_markup: new InlineKeyboard() }).catch(() => {}))
  const from = ctx.from!
  void mcp
    .notification({
      method: 'notifications/claude/channel',
      params: {
        content: `[choice] ${label}`,
        meta: {
          chat_id: entry.chat_id,
          message_id: String(entry.message_id),
          reply_to_message_id: String(entry.message_id),
          choice: label,
          user: from.username ?? String(from.id),
          user_id: String(from.id),
          ts: new Date().toISOString(),
        },
      },
    })
    .catch(err => {
      process.stderr.write(`telegram channel: failed to deliver choice to Claude: ${err}\n`)
    })
}
// === BUBBLE-CHOICES PATCH END ===
