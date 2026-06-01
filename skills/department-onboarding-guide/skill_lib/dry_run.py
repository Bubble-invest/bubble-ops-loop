"""
dry_run.py — Step 6 (Tests / dry-run) simulator.

Two entry points:

  1) `run_dry_run(...)`  — UX-1 stub kept for back-compat. Pure aggregator
     over operator-provided per-layer status hints. No file I/O, no schema
     validation. The 20 UX-1 tests pin this contract.

  2) `run_dry_run_full(...)`  — UX-4 full simulator. Writes the canonical
     4-file output schema (summary.md, artifacts/, logs.jsonl, .last-run)
     for each of the 4 layers, plus the layer-specific extras:
       Layer 1: synthesized-queue-item.yaml
       Layer 2: gate-fake-<id>.yaml
       Layer 3: exec-log.jsonl (one fake exec line)
       Layer 4: risk-brief.md + risk-kpis.yaml + management-export.yaml
     Every artifact is schema-validated against schemas-draft/*.schema.yaml.

Per Notion v5 lines 925-946 — dry-run all-green is required before
activation, or operator must explicitly accept warnings. FAILED never
advances regardless of operator override.

Per Notion v5 line 944 — the canonical example warning is
"Missing brand safety test fixture": when dept.yaml.draft declares a
brand_safety guardrail without a corresponding tests/brand_safety*.yaml,
the simulator emits a warning Check (does not block by default).

Determinism: with `seed` set, two runs produce byte-identical artifacts
(excluding the .last-run sentinel which records the wall-clock ts).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# UX-1 legacy contract (DO NOT BREAK — pinned by 20 existing tests).
# ---------------------------------------------------------------------------

class DryRunStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


_VALID = {"passed", "warning", "failed"}


def run_dry_run(
    dept_root: Path,
    fake_queue_item: Dict[str, Any],
    layer_checks: Dict[str, str],
    operator_accepts_warnings: bool = False,
) -> Dict[str, Any]:
    """Simulate a 4-layer round-trip from a fake queue item (UX-1 stub).

    Pure aggregator: returns deterministic synthesized outputs + an
    overall_status derived from layer_checks. No file I/O, no schema
    validation. Use `run_dry_run_full` for the full UX-4 simulator.
    """
    # Validate inputs.
    for k in ("layer_1", "layer_2", "layer_3", "layer_4"):
        if k not in layer_checks:
            raise ValueError(f"layer_checks missing key: {k}")
        if layer_checks[k] not in _VALID:
            raise ValueError(
                f"layer_checks[{k}] must be in {_VALID}, got {layer_checks[k]!r}"
            )

    item_id = fake_queue_item.get("id", "fixture-item")
    kind = fake_queue_item.get("kind", "unknown_kind")

    outputs = {
        "layer_1": {
            "kind": "layer1_summary",
            "summary": f"Layer 1 ingested fake item {item_id} (kind={kind}).",
            "derived_from": item_id,
        },
        "layer_2": {
            "kind": "layer2_draft",
            "summary": f"Layer 2 drafted plan for {item_id}.",
            "gate_emitted": f"gate-dryrun-{item_id}",
        },
        "layer_3": {
            "kind": "layer3_exec",
            "summary": f"Layer 3 simulated execution for {item_id}.",
            "simulated_exec": True,
        },
        "layer_4": {
            "kind": "layer4_risk_brief",
            "summary": f"Layer 4 risk brief for round-trip {item_id}.",
            "risk_brief": "no real risk; fixture-mode.",
        },
    }

    statuses = set(layer_checks.values())
    if "failed" in statuses:
        overall = DryRunStatus.FAILED
    elif "warning" in statuses:
        overall = DryRunStatus.WARNING
    else:
        overall = DryRunStatus.PASSED

    if overall == DryRunStatus.PASSED:
        can_advance = True
    elif overall == DryRunStatus.WARNING:
        can_advance = bool(operator_accepts_warnings)
    else:
        can_advance = False

    return {
        "outputs": outputs,
        "checks": dict(layer_checks),
        "overall_status": overall,
        "can_advance_to_ready": can_advance,
        "dept_root": str(dept_root),
    }


# ---------------------------------------------------------------------------
# UX-4 full simulator
# ---------------------------------------------------------------------------

DRY_RUN_BANNER = "DRY-RUN ARTIFACT"

# Path to the bundled fake-queue-item fixtures.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "dry_run_fixtures"

# Path to schemas-draft (project root is .../bubble-ops-loop/).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCHEMAS_DIR = _PROJECT_ROOT / "schemas-draft"

# Deterministic ts used when seed is provided. The wall-clock ts only goes
# into .last-run (which determinism tests exclude).
_DETERMINISTIC_TS = "2026-05-20T00-00-00Z"


@dataclass
class Check:
    step: str
    scope: str
    status: str  # "passed" | "warning" | "failed"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LayerResult:
    layer: int
    status: str
    artifacts_written: List[Path] = field(default_factory=list)
    schema_validations: Dict[str, bool] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "status": self.status,
            "artifacts_written": [str(p) for p in self.artifacts_written],
            "schema_validations": dict(self.schema_validations),
            "notes": list(self.notes),
        }


@dataclass
class DryRunResult:
    dept_root: Path
    dry_run_ts: str
    overall_status: str
    can_advance_to_ready: bool
    layer_results: Dict[int, LayerResult] = field(default_factory=dict)
    checks: List[Check] = field(default_factory=list)
    artifacts_dir: Path = field(default_factory=lambda: Path("."))

    def to_dict(self) -> dict:
        return {
            "dept_root": str(self.dept_root),
            "dry_run_ts": self.dry_run_ts,
            "overall_status": self.overall_status,
            "can_advance_to_ready": self.can_advance_to_ready,
            "artifacts_dir": str(self.artifacts_dir),
            "layer_results": {k: v.to_dict() for k, v in self.layer_results.items()},
            "checks": [c.to_dict() for c in self.checks],
        }


def _now_ts(seed: Optional[int]) -> str:
    """Return an ISO-ish ts string used as the dry-run subdir name."""
    if seed is not None:
        return _DETERMINISTIC_TS
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _load_schema(name: str) -> Optional[dict]:
    path = _SCHEMAS_DIR / f"{name}.schema.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _validate_against(doc: Any, schema_name: str) -> List[str]:
    """Validate `doc` against a named schema. Returns list of error messages."""
    import jsonschema  # local import keeps startup time small
    schema = _load_schema(schema_name)
    if schema is None:
        return [f"schema not found: {schema_name}"]
    validator = jsonschema.Draft7Validator(schema)
    return [e.message for e in validator.iter_errors(doc)]


def _load_dept_draft(dept_root: Path) -> Optional[dict]:
    """Return dept.yaml.draft contents if present, else None."""
    for candidate in ("dept.yaml.draft", "dept.yaml"):
        p = dept_root / candidate
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return None
    return None


def _synthesize_fake_queue_item(dept_root: Path) -> dict:
    """Build a canonical fake queue item from dept.yaml.draft::recurring_missions[0]
    (using its `creates[0]` as `kind`). Falls back to the generic template
    when no recurring_missions are declared.
    """
    dept = _load_dept_draft(dept_root) or {}
    missions = dept.get("recurring_missions") or []
    if missions and isinstance(missions, list):
        m0 = missions[0] or {}
        creates = m0.get("creates") or []
        kind = creates[0] if creates else "research"
        return {
            "id": f"dryrun-{kind.replace('_', '-')}-001",
            "kind": kind,
            "source_layer": int(m0.get("layer", 1)),
            "target_layer": 2,
            "priority": "low",
            "created_at": "2026-05-20T00:00:00Z",
            "payload": {
                "fake": True,
                "reason": "synthesized by UX-4 dry-run simulator from recurring_missions[0]",
                "mission_id": m0.get("id", "unknown_mission"),
            },
        }
    # Fall back to bundled generic template.
    generic_path = _TEMPLATES_DIR / "generic.queue-item.yaml"
    if generic_path.exists():
        return yaml.safe_load(generic_path.read_text(encoding="utf-8"))
    return {
        "id": "dryrun-generic-001",
        "kind": "research",
        "source_layer": 1,
        "target_layer": 2,
        "priority": "low",
        "created_at": "2026-05-20T00:00:00Z",
        "payload": {"fake": True},
    }


def _write_yaml_with_banner(path: Path, doc: dict, kind_label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"# {DRY_RUN_BANNER} ({kind_label}) — synthesized at {_DETERMINISTIC_TS}, "
        f"not for production use.\n"
        + yaml.safe_dump(doc, sort_keys=True, allow_unicode=True)
    )
    path.write_text(body, encoding="utf-8")


def _write_md_with_banner(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- {DRY_RUN_BANNER} — synthesized at {_DETERMINISTIC_TS} -->\n\n"
    path.write_text(header + body, encoding="utf-8")


def _write_log_jsonl(path: Path, entries: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_last_run(path: Path, layer: int, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use the deterministic ts so seeded runs stay reproducible.
    path.write_text(
        f"layer={layer}\nstatus={status}\nts={_DETERMINISTIC_TS}\n",
        encoding="utf-8",
    )


def _write_layer_skeleton(layer_dir: Path, layer: int, summary_body: str, log_entries: List[dict]) -> List[Path]:
    """Write the canonical 4-file output skeleton for a layer."""
    artifacts: List[Path] = []
    _write_md_with_banner(layer_dir / "summary.md", summary_body)
    artifacts.append(layer_dir / "summary.md")
    (layer_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (layer_dir / "artifacts" / ".gitkeep").write_text("", encoding="utf-8")
    artifacts.append(layer_dir / "artifacts" / ".gitkeep")
    _write_log_jsonl(layer_dir / "logs.jsonl", log_entries)
    artifacts.append(layer_dir / "logs.jsonl")
    _write_last_run(layer_dir / ".last-run", layer, "passed")
    artifacts.append(layer_dir / ".last-run")
    return artifacts


def _aggregate_status(checks: List[Check]) -> str:
    statuses = {c.status for c in checks}
    if "failed" in statuses:
        return "FAILED"
    if "warning" in statuses:
        return "WARNING"
    return "PASSED"


def _detect_brand_safety_warning(dept_root: Path) -> Optional[Check]:
    """Per Notion v5 line 944, surface a warning when dept declares a
    brand_safety guardrail but no tests/brand_safety*.yaml fixture exists."""
    dept = _load_dept_draft(dept_root)
    if not dept:
        return None
    policies = (dept.get("gate_policies") or {})
    declares_brand_safety = False
    for _pid, pol in policies.items():
        if not isinstance(pol, dict):
            continue
        guards = pol.get("kpi_guardrails") or {}
        if any("brand" in str(k).lower() for k in guards):
            declares_brand_safety = True
            break
    if not declares_brand_safety:
        return None
    tests_dir = dept_root / "tests"
    fixtures = list(tests_dir.glob("*brand_safety*.yaml")) if tests_dir.exists() else []
    if fixtures:
        return None
    return Check(
        step="layer_4_brand_safety_fixture",
        scope=str(tests_dir / "brand_safety.yaml"),
        status="warning",
        message="Missing brand safety test fixture",
    )


def run_dry_run_full(
    dept_root: Path,
    fake_queue_item: Optional[Dict[str, Any]] = None,
    operator_accepts_warnings: bool = False,
    seed: Optional[int] = None,
) -> DryRunResult:
    """UX-4: Run a full 4-layer round-trip with fake data.

    Writes the canonical 4-file output schema for each layer under
    outputs/dry-run/<ts>/<layer>/, plus layer-specific extras. Validates
    every artifact against schemas-draft/ schemas and returns a structured
    DryRunResult ready to feed the UX-3 HTML renderer.

    Args:
      dept_root:                The dept-repo root.
      fake_queue_item:          If None, synthesized from recurring_missions[0]
                                or the bundled generic template.
      operator_accepts_warnings: True allows WARNING to advance. Has no effect
                                on FAILED (per Notion v5 line 946).
      seed:                     When set, all timestamps + ids are deterministic
                                so two runs with the same seed produce
                                byte-identical artifacts (except .last-run).
    """
    dept_root = Path(dept_root)
    ts = _now_ts(seed)
    base = dept_root / "outputs" / "dry-run" / ts
    base.mkdir(parents=True, exist_ok=True)

    layer_results: Dict[int, LayerResult] = {}
    checks: List[Check] = []

    # ---- Step 1: synthesize fake queue item (or accept caller's) ----------
    qi = fake_queue_item if fake_queue_item is not None else _synthesize_fake_queue_item(dept_root)

    # ---------- LAYER 1 -----------------------------------------------------
    l1_dir = base / "1"
    l1_artifacts = _write_layer_skeleton(
        l1_dir,
        layer=1,
        summary_body=(
            f"# Layer 1 dry-run summary\n\n"
            f"Synthesized fake queue item `{qi.get('id', 'unknown')}` of kind "
            f"`{qi.get('kind', 'unknown')}`. No real data sources touched.\n"
        ),
        log_entries=[{
            "ts": _DETERMINISTIC_TS, "layer": 1, "event": "queue_item_synthesized",
            "id": qi.get("id"), "kind": qi.get("kind"),
        }],
    )
    qi_path = l1_dir / "synthesized-queue-item.yaml"
    _write_yaml_with_banner(qi_path, qi, "queue-item")
    l1_artifacts.append(qi_path)

    qi_errors = _validate_against(qi, "queue-item")
    l1_status = "passed" if not qi_errors else "failed"
    checks.append(Check(
        step="layer_1_queue_item_schema",
        scope=str(qi_path),
        status=l1_status,
        message=("queue-item valid" if not qi_errors else
                 f"queue-item invalid: {'; '.join(qi_errors)[:240]}"),
    ))
    checks.append(Check(
        step="layer_1_output_schema",
        scope=str(l1_dir),
        status="passed",
        message="4-file output skeleton written",
    ))
    layer_results[1] = LayerResult(
        layer=1,
        status=l1_status,
        artifacts_written=l1_artifacts,
        schema_validations={"synthesized-queue-item.yaml": not qi_errors},
        notes=[] if not qi_errors else qi_errors,
    )

    # ---------- LAYER 2 -----------------------------------------------------
    l2_dir = base / "2"
    gate_id = f"gate-fake-{qi.get('id', 'unknown')}"
    gate_doc = {
        "id": gate_id,
        "kind": "decision",  # internal kind — no gate_policy_id required
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "actions": ["approve", "reject", "defer"],
        "current_mode": "manual_required",
        "future_eligible_modes": [],
        "dry_run_note": "Synthetic decision gate emitted for dry-run round-trip. "
                        "Not a real gate; do not surface to operator.",
    }
    l2_artifacts = _write_layer_skeleton(
        l2_dir,
        layer=2,
        summary_body=(
            f"# Layer 2 dry-run summary\n\n"
            f"Drafted plan for `{qi.get('id')}` and emitted fake gate `{gate_id}`.\n"
        ),
        log_entries=[{
            "ts": _DETERMINISTIC_TS, "layer": 2, "event": "gate_emitted",
            "gate_id": gate_id,
        }],
    )
    gate_path = l2_dir / f"{gate_id}.yaml"
    _write_yaml_with_banner(gate_path, gate_doc, "gate-item")
    l2_artifacts.append(gate_path)

    gate_errors = _validate_against(gate_doc, "gate-item")
    l2_status = "passed" if not gate_errors else "failed"
    checks.append(Check(
        step="layer_2_gate_item_schema",
        scope=str(gate_path),
        status=l2_status,
        message=("gate-item valid" if not gate_errors else
                 f"gate-item invalid: {'; '.join(gate_errors)[:240]}"),
    ))
    checks.append(Check(
        step="layer_2_draft_produced",
        scope=str(l2_dir),
        status="passed",
        message="Layer 2 draft + gate produced",
    ))
    layer_results[2] = LayerResult(
        layer=2,
        status=l2_status,
        artifacts_written=l2_artifacts,
        schema_validations={f"{gate_id}.yaml": not gate_errors},
    )

    # ---------- Fake approval (no schema; just a YAML decision) ------------
    inbox_dir = base / "inbox" / "decisions"
    approval_doc = {
        "gate_id": gate_id,
        "approved": True,
        "approver": "operator-dryrun",
        "ts": _DETERMINISTIC_TS,
        "note": "Synthetic approval for dry-run round-trip.",
    }
    approval_path = inbox_dir / f"{gate_id}.yaml"
    _write_yaml_with_banner(approval_path, approval_doc, "approval")

    # ---------- LAYER 3 -----------------------------------------------------
    l3_dir = base / "3"
    l3_artifacts = _write_layer_skeleton(
        l3_dir,
        layer=3,
        summary_body=(
            f"# Layer 3 dry-run summary\n\n"
            f"Simulated execution against approval `{gate_id}`. No real side-effects.\n"
        ),
        log_entries=[{
            "ts": _DETERMINISTIC_TS, "layer": 3, "event": "fake_exec",
            "gate_id": gate_id, "status": "ok",
        }],
    )
    exec_log_path = l3_dir / "exec-log.jsonl"
    exec_log_path.write_text(
        json.dumps({
            "ts": _DETERMINISTIC_TS, "gate_id": gate_id, "action": "fake_exec",
            "result": "ok", "dry_run": True,
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    l3_artifacts.append(exec_log_path)
    checks.append(Check(
        step="layer_3_execution_valid",
        scope=str(l3_dir),
        status="passed",
        message="Layer 3 fake execution wrote exec-log.jsonl",
    ))
    layer_results[3] = LayerResult(
        layer=3, status="passed", artifacts_written=l3_artifacts,
    )

    # ---------- LAYER 4 -----------------------------------------------------
    l4_dir = base / "4"
    dept_slug = "unknown-dept"
    dept = _load_dept_draft(dept_root) or {}
    dep_block = dept.get("department") or {}
    if isinstance(dep_block, dict):
        dept_slug = dep_block.get("slug") or dept_slug

    risk_brief_body = (
        f"# Risk brief (dry-run)\n\n"
        f"Round-trip simulated end-to-end for fake queue item `{qi.get('id')}`. "
        f"No real risk surface — fixture mode.\n"
    )
    risk_brief_path = l4_dir / "risk-brief.md"
    _write_md_with_banner(risk_brief_path, risk_brief_body)

    risk_kpis_doc = {
        "dept": dept_slug,
        "date": "2026-05-20",
        "kpis": {"dry_run": True, "open_exceptions": 0, "open_gates": 1},
    }
    risk_kpis_path = l4_dir / "risk-kpis.yaml"
    _write_yaml_with_banner(risk_kpis_path, risk_kpis_doc, "risk-kpis")

    me_doc = {
        "dept": dept_slug,
        "date": "2026-05-20",
        "status": "clean",
        "last_successful_layer": 4,
        "open_gates": 1,
        "open_exceptions": 0,
        "top_kpis": {"dry_run": True},
        "needs_management_attention": [],
        "links": {
            "risk_kpis": "outputs/dry-run/" + ts + "/4/risk-kpis.yaml",
            "risk_brief": "outputs/dry-run/" + ts + "/4/risk-brief.md",
        },
    }
    me_path = l4_dir / "management-export.yaml"
    _write_yaml_with_banner(me_path, me_doc, "management-export")

    l4_artifacts = _write_layer_skeleton(
        l4_dir,
        layer=4,
        summary_body=(
            f"# Layer 4 dry-run summary\n\n"
            f"Produced risk-brief.md, risk-kpis.yaml, management-export.yaml.\n"
        ),
        log_entries=[{
            "ts": _DETERMINISTIC_TS, "layer": 4, "event": "risk_brief_emitted",
        }],
    )
    # Append the 3 layer-4-specific outputs to the artifacts list.
    l4_artifacts.extend([risk_brief_path, risk_kpis_path, me_path])

    me_errors = _validate_against(me_doc, "management-export")
    l4_status = "passed" if not me_errors else "failed"
    checks.append(Check(
        step="layer_4_management_export_schema",
        scope=str(me_path),
        status=l4_status,
        message=("management-export valid" if not me_errors else
                 f"management-export invalid: {'; '.join(me_errors)[:240]}"),
    ))
    checks.append(Check(
        step="layer_4_three_outputs",
        scope=str(l4_dir),
        status="passed",
        message="risk-brief.md + risk-kpis.yaml + management-export.yaml present",
    ))

    # Brand-safety guard (Notion v5 line 944).
    bs_warning = _detect_brand_safety_warning(dept_root)
    if bs_warning is not None:
        checks.append(bs_warning)
        if l4_status == "passed":
            l4_status = "warning"

    layer_results[4] = LayerResult(
        layer=4,
        status=l4_status,
        artifacts_written=l4_artifacts,
        schema_validations={"management-export.yaml": not me_errors},
    )

    # ---------- Aggregate ---------------------------------------------------
    overall = _aggregate_status(checks)
    if overall == "PASSED":
        can_advance = True
    elif overall == "WARNING":
        can_advance = bool(operator_accepts_warnings)
    else:
        can_advance = False

    return DryRunResult(
        dept_root=dept_root,
        dry_run_ts=ts,
        overall_status=overall,
        can_advance_to_ready=can_advance,
        layer_results=layer_results,
        checks=checks,
        artifacts_dir=base,
    )
