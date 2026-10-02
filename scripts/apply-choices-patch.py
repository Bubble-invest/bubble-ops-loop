#!/usr/bin/env python3
"""apply-choices-patch.py — idempotent source transform for the telegram `choices` patch.

Usage: apply-choices-patch.py <server.ts> <choices.block.ts> [--check]
  --check : exit 0 if server.ts already carries the canonical patch (all 4 parts), 1 otherwise.
Otherwise rewrites server.ts in place (caller owns backup / bun-build / restore) and prints
"already-present" or "applied". Exit 3 = an anchor is missing (plugin drift) -> file untouched.
"""
import sys

BEGIN, END = "// === BUBBLE-CHOICES PATCH BEGIN ===", "// === BUBBLE-CHOICES PATCH END ==="
M_SCHEMA = "/* BUBBLE-CHOICES schema */"
M_SEND = "/* BUBBLE-CHOICES send */"
M_CB = "/* BUBBLE-CHOICES callback */"

A_HELPERS = "// Inline-button handler for permission requests."
A_SCHEMA = "          format: {\n            type: 'string',\n            enum: ['text', 'markdownv2'],"
A_DESC = "and files (absolute paths) to attach images or documents.',"
A_SEND = """            const sent = await bot.api.sendMessage(chat_id, chunks[i], {
              ...(shouldReplyTo ? { reply_parameters: { message_id: reply_to } } : {}),
              ...(parseMode ? { parse_mode: parseMode } : {}),
            })
            sentIds.push(sent.message_id)
"""
A_CB = "  const data = ctx.callbackQuery.data\n  const m = /^perm:"


def canonical(block_path):
    raw = open(block_path).read()
    return raw[raw.index(BEGIN): raw.index(END) + len(END)]


def has_all(s, patch):
    return patch in s and all(m in s for m in (M_SCHEMA, M_SEND, M_CB))


def transform(s, patch):
    if BEGIN in s:  # stale block -> refresh helpers only, edits are marker-guarded
        s = s[: s.index(BEGIN)] + patch + s[s.index(END) + len(END):]
    else:
        if s.count(A_HELPERS) != 1:
            raise KeyError("helpers anchor")
        s = s.replace(A_HELPERS, patch + "\n\n" + A_HELPERS, 1)
    if M_SCHEMA not in s:
        k = s.find(A_SCHEMA)  # first 'format' prop; edit_message has a second one
        if k < 0 or k > s.find("name: 'react'") or s.count(A_DESC) != 1:
            raise KeyError("schema anchor")
        prop = (
            "          choices: { " + M_SCHEMA + "\n"
            "            type: 'array',\n"
            "            items: { type: 'string' },\n"
            "            maxItems: 8,\n"
            "            description: 'Optional 1-8 short answer options. Sent as tappable inline buttons under the message; "
            "the tap arrives as a new inbound turn \"[choice] <label>\" with meta.reply_to_message_id = this message. Use for quick decisions.',\n"
            "          },\n"
        )
        s = s.replace(A_SCHEMA, prop + A_SCHEMA, 1)
        s = s.replace(
            A_DESC,
            "and files (absolute paths) to attach images or documents, and choices (1-8 short strings) as tappable answer buttons.',",
            1,
        )
    if M_SEND not in s:
        if s.count(A_SEND) != 1:
            raise KeyError("send anchor")
        new = (
            "            " + M_SEND + "\n"
            "            const _choiceKb = _choices.length > 0 && i === chunks.length - 1\n"
            "            const sent = await bot.api.sendMessage(chat_id, chunks[i], {\n"
            "              ...(shouldReplyTo ? { reply_parameters: { message_id: reply_to } } : {}),\n"
            "              ...(parseMode ? { parse_mode: parseMode } : {}),\n"
            "              ...(_choiceKb ? { reply_markup: newChoiceKeyboard((_choiceId = newChoiceId()), _choices) } : {}),\n"
            "            })\n"
            "            if (_choiceKb) registerChoice(_choiceId, { chat_id, message_id: sent.message_id, text: chunks[i]!, ...(parseMode ? { parse_mode: parseMode } : {}), labels: _choices, ts: Date.now() })\n"
            "            sentIds.push(sent.message_id)\n"
        )
        s = s.replace(A_SEND, new, 1)
        a = "        const sentIds: number[] = []\n"
        if s.count(a) != 1:
            raise KeyError("sentIds anchor")
        s = s.replace(a, a + "        const _choices = normalizeChoices(args.choices)\n        let _choiceId = ''\n", 1)
    if M_CB not in s:
        if s.count(A_CB) != 1:
            raise KeyError("callback anchor")
        s = s.replace(
            A_CB,
            "  const data = ctx.callbackQuery.data\n  " + M_CB + "\n"
            "  if (data.startsWith('ch:')) { await handleChoiceTap(ctx); return }\n  const m = /^perm:",
            1,
        )
    return s


def main():
    srv, blk = sys.argv[1], sys.argv[2]
    s, patch = open(srv).read(), canonical(blk)
    if has_all(s, patch):
        if "--check" in sys.argv:
            return 0
        print("already-present")
        return 0
    if "--check" in sys.argv:
        return 1
    try:
        out = transform(s, patch)
    except KeyError as e:
        print(f"anchor missing: {e}", file=sys.stderr)
        return 3
    open(srv, "w").write(out)
    print("applied")
    return 0


sys.exit(main())
