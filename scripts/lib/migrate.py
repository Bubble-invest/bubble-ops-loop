#!/usr/bin/env python3
"""
migrate.py — Sprint H+I Fix 7.

Brownfield-dept ingestion: takes a pre-existing workspace dir (with
config.yaml + CLAUDE.md) and produces a bubble-ops-<slug>/ tree that:

  - has the same scaffold as bootstrap-dept.sh (CLAUDE.md, dept.yaml.draft,
    STATE.yaml, the onboarding/ + .gitkeep + .claude/settings.json tree);
  - pre-seeds dept.yaml.draft::department.mandate from a sentence inferred
    from the source CLAUDE.md;
  - copies source CLAUDE.md content into MANDATE.md so the operator can
    review the inheritance;
  - pre-populates STATE.yaml::validated_steps = [mandate] and status =
    Configuring (further than the bootstrap Idea, since we already have
    a mandate);
  - emits a summary report: fields mapped + fields needing operator review.

Called by scripts/migrate-dept.sh.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import scaffold  # noqa: E402
import state_yaml  # noqa: E402


# Fields we know how to map from a brownfield config.yaml -> dept.yaml.draft.
# Anything else surfaces in the "needs operator review" report.
KNOWN_SOURCE_FIELDS_MAPPED: set[str] = {
    # Currently none of Maya's config.yaml fields have a direct home in the
    # bubble-ops dept.yaml shape (backend, notion.*, account_used_default,
    # quotas.* etc. are all Maya-internal knobs). The migration's value is
    # in scaffolding + mandate inheritance, not in field-by-field copy.
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_mandate_from_claude_md(claude_md_text: str) -> str:
    """Heuristic: pull the first non-trivial sentence from the source
    CLAUDE.md that describes what the dept does.

    Order of preference:
      1. The paragraph right after a heading containing 'Qui est' or
         'Mission' (FR conventions in Maya/Ben CLAUDE.md).
      2. The first paragraph that is > 40 chars and doesn't start with '#'.
      3. The first non-empty non-heading line.

    Result is a single line, truncated to 200 chars.
    """
    lines = claude_md_text.splitlines()

    # Pass 1: 'Qui est' / 'Mission' section content.
    for i, line in enumerate(lines):
        if re.search(r"^##.*\b(Qui est|Mission|Role|Rôle)\b", line):
            for body_line in lines[i + 1:]:
                stripped = body_line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    break
                return _truncate(stripped, 200)
            break

    # Pass 2: first paragraph > 40 chars.
    for line in lines:
        stripped = line.strip()
        if (not stripped) or stripped.startswith("#"):
            continue
        if len(stripped) >= 40:
            return _truncate(stripped, 200)

    # Pass 3: any non-empty non-heading line.
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return _truncate(stripped, 200)

    raise ValueError(
        "Could not extract a mandate from the source CLAUDE.md — file looks empty"
    )


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _classify_source_fields(config_doc: dict) -> tuple[list[str], list[str]]:
    """Walk the source config dict and return (mapped, unmapped) leaf field
    names so the report can name them concretely."""
    mapped: list[str] = []
    unmapped: list[str] = []

    def _walk(prefix: str, node):
        if isinstance(node, dict):
            for k, v in node.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    _walk(key, v)
                else:
                    if key in KNOWN_SOURCE_FIELDS_MAPPED:
                        mapped.append(key)
                    else:
                        unmapped.append(key)
        # We don't recurse into lists in this v1 — list items are usually
        # opaque to the mapping report.

    _walk("", config_doc or {})
    return mapped, unmapped


def migrate(
    source: Path,
    target: Path,
    slug: str,
    display_name: str,
    owner: str,
) -> dict:
    """Materialize the migrated tree at `target`. Returns a summary dict
    suitable for printing to stdout."""
    source = source.resolve()
    target = target.resolve()

    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(
            f"--source path does not exist or is not a directory: {source}"
        )

    src_config = source / "config.yaml"
    if not src_config.exists():
        raise FileNotFoundError(
            f"source has no config.yaml at {src_config}; cannot migrate"
        )

    if target.exists():
        raise FileExistsError(
            f"target {target} already exists — refusing to clobber. "
            f"Move or delete it manually, then re-run."
        )

    src_claude = source / "CLAUDE.md"

    # ---- 1. Render the standard scaffold (same as bootstrap). -------------
    target.mkdir(parents=True)
    scaffold.scaffold(target, slug=slug, display_name=display_name, owner=owner)

    # ---- 2. Extract mandate from source CLAUDE.md. ------------------------
    inherited_mandate = None
    mandate_md_body = None
    if src_claude.exists():
        src_claude_text = src_claude.read_text(encoding="utf-8")
        inherited_mandate = _extract_mandate_from_claude_md(src_claude_text)
        mandate_md_body = (
            "# Mandate (migrated from " + str(source) + ")\n\n"
            "**Phrase courte (machine-readable, copiée dans "
            "`dept.yaml.draft::department.mandate`) :**\n\n"
            f"> {inherited_mandate}\n\n"
            "**Narratif complet (hérité de l'ancien CLAUDE.md, à éditer "
            "par l'opérateur) :**\n\n"
            + src_claude_text
        )
    else:
        inherited_mandate = (
            f"{display_name} — mandate à compléter par l'opérateur (aucun "
            f"CLAUDE.md trouvé dans la source)."
        )
        mandate_md_body = (
            "# Mandate (migrated, no source CLAUDE.md)\n\n"
            "> " + inherited_mandate + "\n\n"
            "(L'opérateur doit rédiger le narratif complet ici.)\n"
        )

    (target / "MANDATE.md").write_text(mandate_md_body, encoding="utf-8")

    # ---- 3. Patch dept.yaml.draft::department.mandate. --------------------
    dept_doc = yaml.safe_load((target / "dept.yaml.draft").read_text(encoding="utf-8"))
    dept_doc["department"]["mandate"] = inherited_mandate
    (target / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # ---- 4. Pre-seed STATE.yaml: mandate validated, status Configuring. ----
    state_path = target / "onboarding" / "STATE.yaml"
    state_doc = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    state_doc["status"] = "Configuring"
    state_doc.setdefault("validated_steps", [])
    if "mandate" not in state_doc["validated_steps"]:
        state_doc["validated_steps"].append("mandate")
    state_doc.setdefault("commits", []).append({
        "step": "mandate",
        # No real git commit yet — placeholder SHA the operator will
        # replace at the migration-acceptance commit.
        "commit_sha": "0" * 7,
        "validated_at": _now_iso(),
    })
    state_doc["last_updated_at"] = _now_iso()
    state_path.write_text(
        yaml.safe_dump(state_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # ---- 5. Mapping report --------------------------------------------------
    src_config_doc = yaml.safe_load(src_config.read_text(encoding="utf-8")) or {}
    mapped, unmapped = _classify_source_fields(src_config_doc)

    return {
        "source": str(source),
        "target": str(target),
        "inherited_mandate": inherited_mandate,
        "mapped_fields": mapped,
        "unmapped_fields": unmapped,
    }


def format_report(summary: dict) -> str:
    mapped = summary["mapped_fields"]
    unmapped = summary["unmapped_fields"]
    lines = [
        "",
        "============================================================",
        f"  Migration of {summary['source']}",
        f"  -> target: {summary['target']}",
        "============================================================",
        "",
        f"Mandate inherited (copied into dept.yaml.draft + MANDATE.md):",
        f"  > {summary['inherited_mandate']}",
        "",
        f"Mapped {len(mapped)} field(s) from source config.yaml:",
    ]
    if mapped:
        for m in mapped:
            lines.append(f"  - {m}")
    else:
        lines.append("  (none — Maya/Ben-style configs have no direct "
                     "field-to-field mapping into the bubble-ops dept.yaml "
                     "shape; the migration value is the scaffold + mandate "
                     "inheritance)")

    lines += [
        "",
        f"{len(unmapped)} source field(s) need operator review "
        "(no canonical home in bubble-ops dept.yaml):",
    ]
    for u in unmapped:
        lines.append(f"  - {u}")

    lines += [
        "",
        "NEXT STEPS:",
        "  1. Review MANDATE.md — refine the inherited narrative.",
        "  2. Review dept.yaml.draft — the mandate field is pre-populated; "
        "everything else is empty.",
        "  3. Continue with step 2 (missions) via the "
        "department-onboarding-guide skill.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Migrate a brownfield dept workspace into a "
        "bubble-ops-<slug> repo skeleton."
    )
    p.add_argument("--source", required=True,
                   help="path to the existing workspace (e.g. ~/claude-workspaces/Maya_Sales)")
    p.add_argument("--target", required=True,
                   help="where to materialize the bubble-ops-<slug>/ tree")
    p.add_argument("--slug", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--owner", required=True)
    args = p.parse_args(argv)

    try:
        summary = migrate(
            source=Path(args.source),
            target=Path(args.target),
            slug=args.slug,
            display_name=args.display_name,
            owner=args.owner,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 64
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 65

    print(format_report(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
