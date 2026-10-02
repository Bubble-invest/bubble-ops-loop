// Unit tests for the pure helpers in deploy/telegram-plugin/choices.block.ts.
// Run: env -u TELEGRAM_STATE_DIR bun test tests/choices_patch.test.ts
import { test, expect } from 'bun:test'
import { readFileSync, writeFileSync, mkdtempSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'

const src = readFileSync(join(import.meta.dir, '../deploy/telegram-plugin/choices.block.ts'), 'utf8')
const pure = src.slice(src.indexOf('const CHOICE_MAX'), src.indexOf('// ── state:'))
const f = join(mkdtempSync(join(tmpdir(), 'choices-')), 'pure.ts')
writeFileSync(f, pure + '\nexport { normalizeChoices, choiceRows, choiceData, parseChoiceData, escapeMdV2, pruneChoices }\n')
const h: any = await import(f)

test('normalize trims/truncates/limits', () => {
  expect(h.normalizeChoices(undefined)).toEqual([])
  expect(h.normalizeChoices([' a  b ', '', 'c'])).toEqual(['a b', 'c'])
  expect(h.normalizeChoices(['x'.repeat(60)])[0].length).toBe(40)
  expect(() => h.normalizeChoices(Array(9).fill('a'))).toThrow()
  expect(() => h.normalizeChoices('a')).toThrow()
})
test('rows of <=2', () => {
  expect(h.choiceRows(5)).toEqual([[0, 1], [2, 3], [4]])
  expect(h.choiceRows(1)).toEqual([[0]])
})
test('callback data <=64 bytes and round-trips', () => {
  const d = h.choiceData('abcdef0', 7)
  expect(Buffer.byteLength(d)).toBeLessThanOrEqual(64)
  expect(h.parseChoiceData(d)).toEqual({ id: 'abcdef0', idx: 7 })
  expect(h.parseChoiceData('perm:allow:abcde')).toBeNull()
  expect(h.parseChoiceData('ch:../x:1')).toBeNull()
})
test('mdv2 escape', () => expect(h.escapeMdV2('a.b-c')).toBe('a\\.b\\-c'))
test('prune drops expired and caps', () => {
  const now = Date.now()
  const mk = (ts: number) => ({ chat_id: '1', message_id: 1, text: 't', labels: ['a'], ts })
  const out = h.pruneChoices({ old: mk(now - 8 * 864e5), new: mk(now) }, now)
  expect(Object.keys(out)).toEqual(['new'])
  const big: any = {}
  for (let i = 0; i < 300; i++) big['k' + i] = mk(now - i)
  expect(Object.keys(h.pruneChoices(big, now)).length).toBe(200)
})
