"""Thin facade over the token-broker's Policy class.

We re-import the SAME Policy implementation used by the broker (Step 3b)
so the guard's path-allow-list logic is byte-identical to the broker's
policy enforcement. This prevents drift: if the broker allows a path, the
guard does too, and vice versa.

Path-resolution strategy (in order, fail-CLOSED on miss):
  1. Env override: $BUBBLE_BROKER_POLICY_PY points at the broker's
     `src/policy.py` file directly (used in deploy when packages may be
     installed separately).
  2. Sibling-layout discovery: `<git-guard>/../token-broker/src/policy.py`
     (the dev layout on Mac and Morty's `/opt/` install).
  3. Raise ImportError loudly — never silently fall back to a permissive
     default.

Both 1 and 2 use `importlib.util.spec_from_file_location` so we avoid the
"both packages named src" collision that arises with plain
`importlib.import_module('src.policy')`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


def _find_broker_policy_py() -> Path:
    """Locate the broker's policy.py file. Fail-CLOSED with ImportError if missing."""
    env_override = os.environ.get("BUBBLE_BROKER_POLICY_PY")
    if env_override:
        p = Path(env_override).expanduser().resolve()
        if p.is_file():
            return p
        raise ImportError(
            f"BUBBLE_BROKER_POLICY_PY={env_override!r} but file does not exist"
        )
    # Sibling layout: ../token-broker/src/policy.py relative to THIS file
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent / "token-broker" / "src" / "policy.py"
    if candidate.is_file():
        return candidate
    raise ImportError(
        "Cannot locate token-broker/src/policy.py. Either install the broker "
        "alongside the guard, or set BUBBLE_BROKER_POLICY_PY."
    )


def _load_module_from_path(path: Path, module_name: str = "bubble_broker_policy") -> Any:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"failed to build module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_policy_mod = _load_module_from_path(_find_broker_policy_py())

Policy: Any = _policy_mod.Policy
KNOWN_ACTIONS = _policy_mod.KNOWN_ACTIONS
STRUCTURAL_PATH_GLOBS = _policy_mod.STRUCTURAL_PATH_GLOBS


def load_policy(yaml_path: Path | str) -> Any:
    """Load a Policy from a YAML file path.

    Mirrors the broker's `Policy.from_yaml`. Raises FileNotFoundError if
    `yaml_path` doesn't exist (fail-CLOSED — never default to permissive).
    """
    p = Path(yaml_path)
    if not p.is_file():
        raise FileNotFoundError(f"policy YAML not found: {p}")
    return Policy.from_yaml(p)
