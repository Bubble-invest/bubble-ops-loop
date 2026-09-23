"""Shared structural-path policy — single source of truth (board #1432 follow-up).

Re-exports `is_structural_for_repo` from `token-broker/src/policy.py`, the SAME
function `.github/scripts/structural_merge_guard.py`'s Action (and the runtime
push guard) already use to decide whether a changed path is a structural/
mission-definition file requiring App-signed approval. Importing it here — via
the identical `sys.path.insert` idiom already used elsewhere in `console/`
(see `console/routes/agents.py`'s `scripts/lib` import) — means the cockpit's
"is this PR structural?" UI hint can never drift from the guard's own truth:
one policy, read by both the CI check and the console.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKEN_BROKER_SRC = _PROJ_ROOT / "token-broker" / "src"
if str(_TOKEN_BROKER_SRC) not in sys.path:
    sys.path.insert(0, str(_TOKEN_BROKER_SRC))

from policy import is_structural_for_repo  # noqa: E402  (re-exported below)

__all__ = ["is_structural_for_repo"]
