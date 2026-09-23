# scripts/keys/ — decision-signing PUBLIC key

Board #1476. `decision-signing-ed25519.pub.pem` is the Ed25519 **public** key
executors use to verify `inbox/decisions/<gate_id>.yaml` (see
`scripts/lib/decision_signing.py`). It is safe to commit and to vendor to
every dept checkout exactly like `scripts/lib/*.py` already is — only the
matching PRIVATE key (held by uid `bubble-console` on the VPS, SOPS-injected,
never in this repo) can produce a signature this key accepts.

**This file does not exist yet.** It is added by the one-time key-generation
step in board #1476's PR body (run once, by Joris/root, on the VPS — never
in a dept agent's session). Until it's added, `load_public_key()` fails
closed (`FileNotFoundError`), which `verify_decision()` treats exactly like
any other verification failure — logged + allowed in the default
`DECISION_SIGNATURES=warn` mode, refused once a dept flips to `enforce`.

Do not commit a private key, a `.sops` file, or any decrypted key material
here — public key only.
