"""directive_constants — the ONE canonical value the CEO-directive approval
gate and relay agree on.

Board #1550: `dispatch_directives.py` (this repo) hardcoded the literal
`"operator"` as the only `approved_by` value its relay's approval gate
accepts, while `skills/directive_writer.emit_directive` (bubble-ops-tony)
independently required CALLERS to pass the string `"joris"` — never checking
a real cockpit decision at all. The two labels never matched, so the one
directive that ever made it through the relay only did so because Tony
hand-edited `approved_by` after the fact to route around the mismatch
(`directive-accountant-20260926-01.yaml`), and every directive naively
emitted with `approved_by: joris` was silently SKIPped by the relay forever.

Fix: define this value ONCE, here, and have both sides import it rather than
hardcode it independently:
  - the relay (`scripts/dispatch_directives.py`, this repo) imports it
    directly;
  - the emitter (`skills/directive_writer` in bubble-ops-tony) imports the
    vendored copy of THIS file — see `scripts/vendor-dept-libs.sh`'s FILES
    list, the fleet's existing mechanism for keeping framework-owned code
    identical across dept trees without a shared pip package (board #1115 /
    the dept-lib-vendor model).

Neither repo may hardcode this string a third time. If you find another
`== "operator"` gating a directive/relay decision, import this constant
instead.

Not to be confused with the `{{OPERATOR}}` template placeholder used
elsewhere in this codebase's prose/comments/docstrings to genericize the
human operator's name — that is text substitution for readability, not a
runtime value. `DIRECTIVE_APPROVED_BY` is a fixed, lowercase field value the
relay's field-equality check compares against; it does not vary per operator
and is not meant to.
"""
from __future__ import annotations

DIRECTIVE_APPROVED_BY = "operator"
