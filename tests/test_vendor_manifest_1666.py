"""Read-only manifest and parity checks for board #1666.

This first increment deliberately does not make production scripts consume the
manifest.  It validates the declarative data and proves that the existing apply,
dry-run, and drift shell arrays describe the same ordered eight-file core set.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shlex
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/vendor-manifest.tsv"
VENDOR = ROOT / "scripts/vendor-dept-libs.sh"
REVENDOR = ROOT / "scripts/revendor-all-depts.sh"
DRIFT = ROOT / "scripts/check-vendor-drift.sh"
EXPECTED_ENTRY_COUNT = 8
DIRECTIVE_REL = "scripts/lib/directive_constants.py"


class ManifestError(ValueError):
    """The read-only vendor manifest is unsafe or malformed."""


def _validate_relative_path(value: str, *, line_number: int, field: str) -> None:
    if not value or value != value.strip():
        raise ManifestError(f"line {line_number}: {field} path is empty or padded")
    if "\\" in value:
        raise ManifestError(f"line {line_number}: {field} path contains a backslash")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise ManifestError(f"line {line_number}: {field} path is absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise ManifestError(f"line {line_number}: {field} path traverses")
    if path.as_posix() != value:
        raise ManifestError(f"line {line_number}: {field} path is not normalized")


def load_manifest(
    repository_root: Path,
    manifest: Path,
    *,
    expected_entry_count: int = EXPECTED_ENTRY_COUNT,
) -> list[tuple[str, str]]:
    """Strictly load manifest data without exposing a production write path."""

    if manifest.is_symlink() or not manifest.is_file():
        raise ManifestError(f"manifest is missing or not a regular file: {manifest}")

    entries: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()

    for line_number, raw_line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line or raw_line.startswith("#"):
            continue

        fields = raw_line.split("\t")
        if len(fields) != 2:
            raise ManifestError(
                f"line {line_number}: expected exactly two tab-separated fields"
            )
        source, destination = fields
        _validate_relative_path(source, line_number=line_number, field="source")
        _validate_relative_path(
            destination, line_number=line_number, field="destination"
        )

        if source in seen_sources:
            raise ManifestError(f"line {line_number}: duplicate source {source}")
        if destination in seen_destinations:
            raise ManifestError(
                f"line {line_number}: duplicate destination {destination}"
            )

        source_path = repository_root / source
        if source_path.is_symlink():
            raise ManifestError(f"line {line_number}: source is a symlink: {source}")
        if not source_path.is_file():
            raise ManifestError(
                f"line {line_number}: source is missing or not a regular file: {source}"
            )

        seen_sources.add(source)
        seen_destinations.add(destination)
        entries.append((source, destination))

    if len(entries) != expected_entry_count:
        raise ManifestError(
            f"expected {expected_entry_count} entries, found {len(entries)}"
        )
    return entries


def parse_shell_array(script: Path, array_name: str) -> list[tuple[str, str]]:
    """Parse the repository's simple quoted src/dst shell-array declarations."""

    lines = script.read_text(encoding="utf-8").splitlines()
    start_markers = [
        index
        for index, line in enumerate(lines)
        if line.strip() == f"{array_name}=("
    ]
    if len(start_markers) != 1:
        raise AssertionError(
            f"{script}: expected one {array_name} declaration, found {len(start_markers)}"
        )

    entries: list[tuple[str, str]] = []
    for raw_line in lines[start_markers[0] + 1 :]:
        line = raw_line.strip()
        if line == ")":
            return entries
        if not line or line.startswith("#"):
            continue

        shell_fields = shlex.split(line, posix=True)
        if len(shell_fields) != 1:
            raise AssertionError(f"{script}: malformed {array_name} item: {raw_line}")
        pair = shell_fields[0].split()
        if len(pair) != 2:
            raise AssertionError(f"{script}: malformed src/dst pair: {raw_line}")
        entries.append((pair[0], pair[1]))

    raise AssertionError(f"{script}: unterminated {array_name} declaration")


def _is_core_entry(entry: tuple[str, str]) -> bool:
    source, _destination = entry
    return source.startswith("scripts/lib/") or source == "tools/notify_layer.py"


class VendorManifestRepositoryTests(unittest.TestCase):
    def test_manifest_is_strict_read_only_data_with_real_sources(self) -> None:
        entries = load_manifest(ROOT, MANIFEST)

        self.assertEqual(len(entries), EXPECTED_ENTRY_COUNT)
        self.assertIn((DIRECTIVE_REL, DIRECTIVE_REL), entries)
        self.assertTrue(all(_is_core_entry(entry) for entry in entries))

        for script in (VENDOR, REVENDOR, DRIFT):
            self.assertNotIn(
                MANIFEST.name,
                script.read_text(encoding="utf-8"),
                f"{script.name} must not consume the manifest in this increment",
            )

    def test_manifest_has_exact_ordered_parity_with_existing_shell_arrays(self) -> None:
        manifest_entries = load_manifest(ROOT, MANIFEST)
        apply_entries = parse_shell_array(VENDOR, "MAP")
        dry_run_entries = [
            entry
            for entry in parse_shell_array(REVENDOR, "DRY_RUN_MAP")
            if _is_core_entry(entry)
        ]
        drift_entries = parse_shell_array(DRIFT, "MAP")

        self.assertEqual(manifest_entries, apply_entries)
        self.assertEqual(manifest_entries, dry_run_entries)
        self.assertEqual(manifest_entries, drift_entries)


class VendorManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.manifest = self.root / "vendor-manifest.tsv"
        self.entries = [
            (f"src/file-{index}.py", f"dest/file-{index}.py")
            for index in range(EXPECTED_ENTRY_COUNT)
        ]
        for source, _destination in self.entries:
            source_path = self.root / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(f"# source {source}\n", encoding="utf-8")
        self._write_manifest(self.entries)

    def _write_manifest(self, entries: list[tuple[str, str]]) -> None:
        self.manifest.write_text(
            "".join(f"{source}\t{destination}\n" for source, destination in entries),
            encoding="utf-8",
        )

    def test_malformed_row_is_rejected(self) -> None:
        lines = self.manifest.read_text(encoding="utf-8").splitlines()
        lines[0] = self.entries[0][0]
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ManifestError, "two tab-separated fields"):
            load_manifest(self.root, self.manifest)

    def test_duplicate_source_and_destination_are_rejected(self) -> None:
        duplicate_source = list(self.entries)
        duplicate_source[1] = (duplicate_source[0][0], duplicate_source[1][1])
        self._write_manifest(duplicate_source)
        with self.assertRaisesRegex(ManifestError, "duplicate source"):
            load_manifest(self.root, self.manifest)

        duplicate_destination = list(self.entries)
        duplicate_destination[1] = (
            duplicate_destination[1][0],
            duplicate_destination[0][1],
        )
        self._write_manifest(duplicate_destination)
        with self.assertRaisesRegex(ManifestError, "duplicate destination"):
            load_manifest(self.root, self.manifest)

    def test_source_and_destination_path_traversal_are_rejected(self) -> None:
        traversing_source = list(self.entries)
        traversing_source[0] = ("../escaped.py", traversing_source[0][1])
        self._write_manifest(traversing_source)
        with self.assertRaisesRegex(ManifestError, "source path traverses"):
            load_manifest(self.root, self.manifest)

        traversing_destination = list(self.entries)
        traversing_destination[0] = (
            traversing_destination[0][0],
            "../escaped.py",
        )
        self._write_manifest(traversing_destination)
        with self.assertRaisesRegex(ManifestError, "destination path traverses"):
            load_manifest(self.root, self.manifest)

    def test_missing_source_is_rejected(self) -> None:
        (self.root / self.entries[0][0]).unlink()

        with self.assertRaisesRegex(ManifestError, "source is missing"):
            load_manifest(self.root, self.manifest)

    def test_symlink_source_is_rejected(self) -> None:
        source_path = self.root / self.entries[0][0]
        target_path = self.root / "real-source.py"
        target_path.write_text("# real source\n", encoding="utf-8")
        source_path.unlink()
        source_path.symlink_to(target_path)

        with self.assertRaisesRegex(ManifestError, "source is a symlink"):
            load_manifest(self.root, self.manifest)


if __name__ == "__main__":
    unittest.main()
