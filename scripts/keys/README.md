# scripts/keys/ — decision-signing PUBLIC key

Board #1476. `decision-signing-ed25519.pub.pem` is the Ed25519 **public** key
executors use to verify `inbox/decisions/<gate_id>.yaml` (see
`scripts/lib/decision_signing.py`). Its CONTENT is safe to commit — only the
matching PRIVATE key (held by uid `bubble-console` on the VPS, SOPS-injected,
never in this repo) can produce a signature this key accepts.

**Independent security review (2026-09-24, PR #492): do NOT vendor this
file into dept checkouts, and do NOT add it to
`scripts/vendor-dept-libs.sh`'s MAP.** `decision_signing.py`'s trusted
default (`DEFAULT_PUBLIC_KEY_PATH`) is now a FIXED, root-owned framework
path — `/opt/bubble-ops-loop/scripts/keys/decision-signing-ed25519.pub.pem`
on the VPS (confirmed root:root 0755, not writable by any `agent-<slug>`
uid) — precisely so a dept-writable vendored copy is never the trust
anchor. `scripts/lib/*.py` is vendored (copied, not symlinked, no sticky
bit on the destination directory) into each dept's own agent-writable
checkout; a uid that owns that `0775` directory can `unlink()`+recreate
any file inside it, so a vendored copy of THIS key would let the very uid
Ed25519 signing is meant to gate replace it with one whose private half it
controls. `load_public_key()` also runtime-checks ownership of the
resolved default path and its parent directories before trusting it (fails
closed / degrades via the same `DECISION_SIGNATURES=warn|enforce` gate as
every other verification failure) as defense in depth, in case this
constraint is ever violated later.

**This file does not exist yet at `/opt/bubble-ops-loop/scripts/keys/`.**
It is added by the one-time key-generation step in board #1476's PR body
(run once, by Joris/root, directly on the VPS at the fixed path above —
never in a dept agent's session, and never by adding it to the vendor
MAP). Until it's added, `load_public_key()` fails closed
(`FileNotFoundError`), which `verify_decision()` treats exactly like any
other verification failure — logged + loudly alerted + allowed in the
default `DECISION_SIGNATURES=warn` mode, refused once a dept flips to
`enforce`.

Do not commit a private key, a `.sops` file, or any decrypted key material
here — public key only.
