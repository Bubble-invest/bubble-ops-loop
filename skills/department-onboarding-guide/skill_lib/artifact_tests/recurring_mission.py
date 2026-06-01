"""
artifact_tests/recurring_mission.py — Refonte #2 of 3, Deliverable B.

Per-mission semantic tester. Called by MissionsRunner before committing
a mission to disk. Combines:

  1. JSON-schema conformity (recurring-mission.schema.yaml)
  2. id uniqueness within the dept (no doublon with existing
     missions/*.yaml)
  3. cadence pattern (already enforced by schema, double-checked here
     for a cleaner FR error message)
  4. layer in {1,2,3,4}
  5. creates[] non-empty
  6. "Test mission" simulation: synthesise a queue item from the
     mission's creates[] and validate it against queue-item.schema.yaml
     (Notion v5 line 846)

Returns a FR Bureau-de-Cadre TestResult.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .base import TestResult, register_tester


# ----- schema loading (cached) -----

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
# .../skills/department-onboarding-guide/skill_lib/artifact_tests/recurring_mission.py
# parents:   .../artifact_tests → .../skill_lib → .../onboarding-guide → .../skills → .../bubble-ops-loop
_SCHEMAS_DIR = _PROJECT_ROOT / "schemas-draft"

_CADENCE_RE = re.compile(r"^(daily|weekly|hourly|every_\d+h|every_\d+m|cron:.+)$")
_KIND_RE = re.compile(r"^[a-z][a-z_]+$")


def _load_schema(name: str) -> Dict[str, Any]:
    path = _SCHEMAS_DIR / f"{name}.schema.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_RECURRING_SCHEMA: Optional[Dict[str, Any]] = None
_QUEUE_SCHEMA: Optional[Dict[str, Any]] = None


def _recurring_schema() -> Dict[str, Any]:
    global _RECURRING_SCHEMA
    if _RECURRING_SCHEMA is None:
        _RECURRING_SCHEMA = _load_schema("recurring-mission")
    return _RECURRING_SCHEMA


def _queue_schema() -> Dict[str, Any]:
    global _QUEUE_SCHEMA
    if _QUEUE_SCHEMA is None:
        _QUEUE_SCHEMA = _load_schema("queue-item")
    return _QUEUE_SCHEMA


# ----- the test mission simulation (Notion 846) -----


def simulate_queue_item_from_mission(mission: Dict[str, Any]) -> Dict[str, Any]:
    """Produce a fake queue item the mission *would* create on one tick.

    Used both by the tester and by the "Test mission" UX action so the
    operator sees what a real mission tick will drop into queues/.
    """
    kind = (mission.get("creates") or ["generic_task"])[0]
    if not _KIND_RE.match(kind):
        kind = "generic_task"
    layer = mission.get("layer", 1)
    # source = the layer that owns the mission; target = next layer.
    source = int(layer) if isinstance(layer, int) else 1
    target = source + 1 if source < 4 else 4
    return {
        "id": f"{mission.get('id', 'unknown')}-dryrun-001"[:60].replace("_", "-"),
        "kind": kind,
        "source_layer": source,
        "target_layer": target,
        "priority": "low",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload": {
            "mission_id": mission.get("id"),
            "topic": mission.get("description", ""),
        },
    }


simulate_queue_item_from_mission.__test__ = False  # type: ignore[attr-defined]


# ----- the main tester -----


def test_recurring_mission(payload: Dict[str, Any], ctx: Optional[Any] = None) -> TestResult:
    """Validate one mission dict and return a TestResult.

    `ctx` may be a dict with `dept_root` (Path) — used to check id
    uniqueness against existing `missions/<id>.yaml` files.
    """
    issues: List[str] = []
    suggestions: List[str] = []

    if not isinstance(payload, dict):
        return TestResult(
            passed=False,
            issues=["La mission n'est pas un dictionnaire — impossible à valider."],
            summary_md="**Mission refusée** — format inattendu.",
        )

    mission_id = payload.get("id") or "?"

    # 1. Schema conformity (jsonschema). Done in a tolerant way — any
    # error is surfaced as a single FR issue.
    try:
        import jsonschema  # local import — keeps base.py free of heavy deps
        validator = jsonschema.Draft7Validator(_recurring_schema())
        for err in sorted(validator.iter_errors(payload), key=lambda e: e.path):
            # Translate a few common ones; everything else is shown as-is.
            field = ".".join(str(p) for p in err.path) or "(root)"
            issues.append(f"Schéma : `{field}` — {err.message}")
    except Exception as exc:  # pragma: no cover — defensive
        issues.append(f"Validation schéma : exception inattendue ({exc!r}).")

    # 2. Cadence pattern (clean French message).
    cadence = payload.get("cadence", "")
    if not isinstance(cadence, str) or not _CADENCE_RE.match(cadence):
        issues.append(
            f"Cadence `{cadence}` invalide. Attendu : "
            "`daily` / `weekly` / `hourly` / `every_<N>h` / "
            "`every_<N>m` / `cron:<expr>`."
        )

    # 3. Layer in {1,2,3,4}.
    layer = payload.get("layer")
    if not (isinstance(layer, int) and layer in (1, 2, 3, 4)):
        issues.append(
            f"Layer `{layer}` invalide. Attendu : 1, 2, 3 ou 4."
        )

    # 4. creates[] non-empty.
    creates = payload.get("creates")
    if not (isinstance(creates, list) and len(creates) >= 1):
        issues.append(
            "La mission ne déclare rien dans `creates[]`. "
            "Une mission doit créer au moins un type de queue item."
        )

    # 5. id uniqueness.
    dept_root: Optional[Path] = None
    updating_mission_id: Optional[str] = None
    if isinstance(ctx, dict):
        dept_root = ctx.get("dept_root")
        updating_mission_id = ctx.get("updating_mission_id")
    elif ctx is not None:
        dept_root = getattr(ctx, "dept_root", None)
        updating_mission_id = getattr(ctx, "updating_mission_id", None)
    if dept_root is not None and isinstance(mission_id, str):
        missions_dir = Path(dept_root) / "missions"
        if missions_dir.exists():
            for existing in missions_dir.glob("*.yaml"):
                try:
                    existing_doc = yaml.safe_load(existing.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if existing_doc.get("id") == mission_id:
                    # If the caller flagged this as an in-place update of
                    # the same mission id, skip the duplicate check.
                    if updating_mission_id == mission_id:
                        continue
                    # Allow re-validation of the *same* mission (same body).
                    if existing_doc != payload:
                        issues.append(
                            f"Doublon : la mission `{mission_id}` existe "
                            f"déjà (`missions/{mission_id}.yaml`). "
                            "Choisis un id unique ou édite l'existante."
                        )
                        break

    # 6. Test mission simulation (Notion 846).
    if not issues:
        try:
            item = simulate_queue_item_from_mission(payload)
            import jsonschema
            jsonschema.Draft7Validator(_queue_schema()).validate(item)
        except Exception as exc:
            issues.append(
                f"La simulation de queue item échoue ({exc.__class__.__name__})."
            )

    # 7. Soft polish suggestions.
    desc = payload.get("description", "")
    if isinstance(desc, str) and 0 < len(desc) < 30:
        suggestions.append(
            "La description est courte. Une phrase plus précise aidera "
            "le Layer 2 subagent à dispatcher correctement."
        )

    passed = not issues
    if passed:
        creates_str = ", ".join(payload.get("creates", []))
        kind_str = (payload.get("creates") or ["?"])[0]
        summary = (
            f"**Mission `{mission_id}` validée.**\n\n"
            f"Elle créera un queue item de type `{kind_str}` à chaque "
            f"tick `{payload.get('cadence', '?')}`, dans "
            f"`{payload.get('output_queue', '?')}`.\n"
            f"- Layer : {payload.get('layer', '?')}\n"
            f"- Crée : `{creates_str}`"
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            f"**Mission `{mission_id}` — à corriger.**\n\n"
            "Voici ce qui bloque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_recurring_mission.__test__ = False  # type: ignore[attr-defined]
register_tester("recurring_mission", test_recurring_mission)
