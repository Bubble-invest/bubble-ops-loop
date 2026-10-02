"""Read-only release metadata and caller/callee compatibility for #1666.

This stacked increment pins the independently reviewed #572 framework commit and
its eight inert manifest entries.  It deliberately adds no vendor/apply writer.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "scripts/vendor-release.json"
MANIFEST = ROOT / "scripts/vendor-manifest.tsv"
REVIEWED_FRAMEWORK_COMMIT = "e56d39a77bcd272ac28a6e18df17ec144437a5a2"
PRODUCTION_VENDOR_SCRIPTS = (
    ROOT / "scripts/vendor-dept-libs.sh",
    ROOT / "scripts/revendor-all-depts.sh",
    ROOT / "scripts/check-vendor-drift.sh",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_entries() -> list[tuple[str, str]]:
    entries = []
    for raw_line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        source, destination = raw_line.split("\t")
        entries.append((source, destination))
    return entries


def _release_metadata() -> dict:
    return json.loads(RELEASE.read_text(encoding="utf-8"))


def _loop_notify_imports(caller: Path) -> set[str]:
    tree = ast.parse(caller.read_text(encoding="utf-8"), filename=str(caller))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "loop_notify"
        for alias in node.names
    }


def _function_definition(module: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(definitions) == 1, f"{module}: expected one definition for {name}"
    definition = definitions[0]
    assert isinstance(definition, ast.FunctionDef), f"{name} must stay synchronous"
    return definition


def _calls_to(caller: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(caller.read_text(encoding="utf-8"), filename=str(caller))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _assert_call_accepted(call: ast.Call, definition: ast.FunctionDef) -> None:
    args = definition.args
    positional = [*args.posonlyargs, *args.args]
    positional_names = [argument.arg for argument in positional]
    required_positional = positional_names[: len(positional) - len(args.defaults)]
    keyword_only_names = [argument.arg for argument in args.kwonlyargs]
    required_keyword_only = {
        argument.arg
        for argument, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    }

    assert not any(isinstance(argument, ast.Starred) for argument in call.args)
    assert all(keyword.arg is not None for keyword in call.keywords)
    provided_keywords = {
        keyword.arg for keyword in call.keywords if keyword.arg is not None
    }

    if args.vararg is None:
        assert len(call.args) <= len(positional_names), (
            f"{definition.name} accepts at most {len(positional_names)} positional "
            f"arguments, caller passes {len(call.args)}"
        )

    bound_positionals = set(positional_names[: len(call.args)])
    assert set(required_positional) <= bound_positionals | provided_keywords, (
        f"caller omits required arguments for {definition.name}"
    )
    assert not (bound_positionals & provided_keywords), (
        f"caller binds an argument twice for {definition.name}"
    )

    if args.kwarg is None:
        accepted_keywords = set(positional_names) | set(keyword_only_names)
        unexpected = provided_keywords - accepted_keywords
        assert not unexpected, (
            f"caller passes unsupported keywords to {definition.name}: "
            f"{sorted(unexpected)}"
        )

    assert required_keyword_only <= provided_keywords, (
        f"caller omits required keyword-only arguments for {definition.name}"
    )


def test_release_pins_reviewed_commit_manifest_and_all_eight_source_hashes() -> None:
    release = _release_metadata()

    assert set(release) == {
        "schema_version",
        "framework_commit",
        "stack_dependency",
        "manifest",
        "compatibility_contracts",
    }
    assert release["schema_version"] == 1
    assert release["framework_commit"] == REVIEWED_FRAMEWORK_COMMIT
    assert re.fullmatch(r"[0-9a-f]{40}", release["framework_commit"])
    assert release["stack_dependency"] == {
        "repository": "Bubble-invest/bubble-ops-loop",
        "pull_request": 572,
        "commit": REVIEWED_FRAMEWORK_COMMIT,
    }

    manifest = release["manifest"]
    assert manifest["path"] == "scripts/vendor-manifest.tsv"
    assert manifest["sha256"] == _sha256(MANIFEST)

    entries = _manifest_entries()
    assert len(entries) == 8
    assert manifest["entries"] == [
        {
            "source": source,
            "destination": destination,
            "sha256": _sha256(ROOT / source),
        }
        for source, destination in entries
    ]

    for script in PRODUCTION_VENDOR_SCRIPTS:
        assert RELEASE.name not in script.read_text(encoding="utf-8"), (
            f"{script.name} must not consume release metadata in this increment"
        )


def test_notify_layer_calls_are_accepted_by_pinned_loop_notify_contract() -> None:
    release = _release_metadata()
    contracts = release["compatibility_contracts"]
    assert contracts == [
        {
            "caller": "tools/notify_layer.py",
            "callee": "scripts/lib/loop_notify.py",
            "symbols": [
                "_configured_brief_filename",
                "log_notify_event",
                "notify_layer_fired",
                "notify_layers_batched",
            ],
        }
    ]

    contract = contracts[0]
    caller = ROOT / contract["caller"]
    callee = ROOT / contract["callee"]
    manifest_sources = {source for source, _destination in _manifest_entries()}
    assert {contract["caller"], contract["callee"]} <= manifest_sources

    declared_symbols = set(contract["symbols"])
    assert declared_symbols == _loop_notify_imports(caller)

    for symbol in contract["symbols"]:
        calls = _calls_to(caller, symbol)
        assert calls, f"compatibility symbol {symbol} is never called"
        definition = _function_definition(callee, symbol)
        for call in calls:
            _assert_call_accepted(call, definition)
