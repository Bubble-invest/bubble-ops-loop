# RUNBOOK — #748 Mac launcher alignment (+ #1133 harness selector)

**What this is.** The Mac ops-loop launchers had drifted into **three different
hand-edited shapes** (audit 2026-09-07):

| Machine | Agent | Secrets | Model | Hermes | Shape |
|---|---|---|---|---|---|
| Joris Mac | Rick (rnd) / Tonio (main) | SOPS vault + `.env` fb | `claude-opus-4-8[1m]` ✓ | yes | vault→tmux |
| M5 | Géraldine (accountant) | SOPS vault + `.env` fb | `claude-opus-4-8[1m]` ✓ | yes | vault→tmux, `env -u` OAuth |
| M1 | content (socials) | **plaintext `.env`** | **`--model opus` (drifts→Opus 5)** | **no** | plain exec |
| M5 | Ellie | bespoke `com.bubble.ellie` (not `ops-loop-*`) | — | — | separate |

This change folds every customization that had been **hand-edited into each live
wrapper** back into `render_loop_wrapper` (`deploy/local/lib/local_loop_lib.sh`) as
flags on `install-local-loop.sh`, so **one aligned wrapper** renders them all, plus
the **#1133 harness selector** (`<selector-dir>/harness-<slug>` → claude default |
hermes). No live wrapper is hand-edited any more: change the lib, re-render.

## The alignment flags (see `install-local-loop.sh --help`)

`--vault --legacy-env --age-key-file --model --chrome --continue --inline-env
--env-unset --harness-selector-dir --hermes-bin`. With none set, the plain generic
wrapper is rendered (backward-compatible). The selector block is always emitted
(default harness = claude, a no-op until a switch flips the file).

## Per-agent invocation (the aligned config)

Run **on each target Mac**, in that Mac's `bubble-ops-loop` checkout. Paths use the
target user's `$HOME`. **Always dry-render first (NO `--activate`), diff vs the live
wrapper, and only then `--activate`.** The only expected functional diffs vs the
current live wrappers are: `PATH` gains a `:$PATH` suffix, the `mktemp` label
becomes `<slug>-sops`, and the new selector block + `start_hermes` + the harness
`if/else` wrapping the existing claude launch. Everything else is byte-identical.

### Joris Mac — Tonio (main)  [pilot: regenerate + restart FIRST]
```
install-local-loop.sh --dept-dir ~/claude-workspaces/Tony_CEO --slug main \
  --claude-bin ~/.local/bin/claude --tmux-bin ~/.local/bin/tmux \
  --telegram-state-dir ~/.claude/channels/telegram \
  --vault ~/claude-workspaces/Tony_CEO/secrets.sops.env \
  --model 'claude-opus-4-8[1m]' --chrome --continue \
  --inline-env "TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN"
```
> `--inline-env` is REQUIRED for every aligned agent: the default is now EMPTY (a
> no-knobs render is the plain bare-exec generic wrapper, so uncustomized depts
> never get a token baked into the tmux argv). Agents that need secrets to reach
> the harness through tmux opt in explicitly, as above.

### Joris Mac — Rick (rnd)  [regenerate LAST; do NOT restart — this is the live session]
```
install-local-loop.sh --dept-dir ~/claude-workspaces/Rick_RnD --slug rnd \
  --claude-bin ~/.local/bin/claude --tmux-bin ~/.local/bin/tmux \
  --telegram-state-dir ~/.claude/channels/telegram-rnd \
  --vault ~/claude-workspaces/Rick_RnD/secrets.sops.env \
  --model 'claude-opus-4-8[1m]' --continue \
  --inline-env "TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN"
```
Rick's wrapper regenerates but takes effect only on the **next** restart — never
`launchctl kickstart` Rick from within Rick's own session.

### M5 — Géraldine (accountant)
```
install-local-loop.sh --dept-dir ~/claude-workspaces/bubble-ops-accountant --slug accountant \
  --telegram-state-dir ~/.claude/channels/telegram-accountant \
  --vault ~/claude-workspaces/bubble-ops-accountant/secrets.sops.env \
  --model 'claude-opus-4-8[1m]' --chrome --continue \
  --inline-env "TELEGRAM_BOT_TOKEN NOTION_API_KEY QONTO_LOGIN QONTO_SECRET_KEY IMAP_PASSWORD_FIRM YOUSIGN_API_KEY" \
  --env-unset "CLAUDE_CODE_OAUTH_TOKEN"      # keychain login wins (Chrome-ext override)
```

### M1 — content (socials)  [needs the prerequisite migration first]
Currently plaintext `.env` + `--model opus` (drift) + no hermes. Before regenerating:
1. **Provision an age key** on M1 (`auth` skill `provision-age-key.sh`) + create
   `secrets.sops.env` from the existing `.env` values (encrypt in place ON M1 — the
   plaintext never leaves the box).
2. **Install hermes** on M1 (uv) so the selector's hermes branch works.
3. Then:
```
install-local-loop.sh --dept-dir <content-dept-dir> --slug content \
  --telegram-state-dir ~/.claude/channels/telegram-socials \
  --vault <content-dept-dir>/secrets.sops.env \
  --model 'claude-opus-4-8[1m]' --chrome --continue \
  --inline-env "TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN"
```

### M5 — Ellie  [fold decision: yes]
Ellie's bespoke `com.bubble.ellie` launcher is migrated onto the same aligned
pattern (slug `ellie`), so the whole fleet is one shape. Capture her current
customizations the same way (vault/model/inline-env) and regenerate; retire the old
`com.bubble.ellie` plist once the aligned one is verified.

## Rollout order (blast-radius-safe)
1. **Tonio** (pilot): dry-render → diff → `--activate` → `launchctl kickstart -k
   gui/$(id -u)/com.bubble.ops-loop-main` → verify tmux up + `running harness: claude`.
2. **Géraldine** (M5), then **content** (M1, after its migration), then **Ellie**.
3. **Rick** LAST: regenerate only, effective next restart — never restarted here.
Each agent: post-restart Telegram round-trip health check (getMe + fresh session
line, not just `is-active`).

## Switching (the #1133 controller, separate change)
The Mac controller flips `<selector-dir>/harness-<slug>` then
`launchctl kickstart -k gui/<uid>/com.bubble.ops-loop-<slug>`; the wrapper re-reads
the selector at launch. Same fence-then-start model as the VPS
`bubble-switch-harness`. (Mac controller portability fix + the Mac commander bot
are tracked separately; NO VPS→Mac SSH — the Mac commander is Mac-hosted.)
