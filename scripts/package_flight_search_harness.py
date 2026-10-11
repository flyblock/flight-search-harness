"""Build and byte-verify the complete Flight Search Harness Plugin."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


class SuitePackageError(ValueError):
    pass


PLUGIN_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SKILL_REQUEST_CONTRACTS = {
    "low-cost-one-way-travel-flight-search": (
        "one_way",
        "flight_search_harness/schemas/requests/one-way-request.schema.json",
    ),
    "low-cost-random-loop-flight-search": (
        "random_loop",
        "flight_search_harness/schemas/requests/random-loop-request.schema.json",
    ),
    "low-cost-round-trip-flight-search": (
        "round_trip",
        "flight_search_harness/schemas/requests/round-trip-request.schema.json",
    ),
}
PUBLIC_SHARED_INTENT_FIELDS = {
    "base_currency",
    "adult_count",
    "baggage_policy",
    "overnight_allowed",
    "self_transfer_policy",
}
INTERNAL_PRODUCT_INPUT_FIELDS = {
    "intent_id",
    "trip_scope",
    "trip_mode",
    "currency_decimal_places",
    "intent_version",
    "product_version",
    "node_scope",
    "beam_width",
    "history_limit",
    "source_strategy",
    "diversity",
    "max_portfolio_offers",
    "max_solver_offers",
}


def _display_path(path: Path, base: Path | None = None) -> str:
    resolved = path.resolve()
    origin = (base or Path.cwd()).resolve()
    try:
        relative = os.path.relpath(resolved, origin)
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuitePackageError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuitePackageError(f"manifest must be an object: {path}")
    return value


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SuitePackageError("package path must be non-empty")
    path = PurePosixPath(value.replace("\\", "/").strip())
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise SuitePackageError(f"unsafe package path: {value}")
    return path.as_posix()


def _resolver_constants(path: Path) -> tuple[str, dict[str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise SuitePackageError(f"cannot inspect Skill resolver {path}: {exc}") from exc
    values: dict[str, Any] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {
            "HARNESS_VERSION",
            "REQUEST_SCHEMAS",
        }:
            continue
        try:
            values[target.id] = ast.literal_eval(statement.value)
        except (TypeError, ValueError) as exc:
            raise SuitePackageError(
                f"Skill resolver {target.id} must be a literal: {path}"
            ) from exc
    version = values.get("HARNESS_VERSION")
    schemas = values.get("REQUEST_SCHEMAS")
    if not isinstance(version, str) or not isinstance(schemas, dict):
        raise SuitePackageError(
            f"Skill resolver must declare HARNESS_VERSION and REQUEST_SCHEMAS: {path}"
        )
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in schemas.items()):
        raise SuitePackageError(f"Skill resolver REQUEST_SCHEMAS is invalid: {path}")
    return version, schemas


def _validate_distribution_contracts(
    project_root: Path,
    suite_manifest: dict[str, Any],
    files: list[str],
) -> None:
    packaged = set(files)
    plugin = _read(project_root / "plugin.json")
    harness_manifest = _read(project_root / "flight_search_harness" / "PRODUCT-MANIFEST.json")
    if suite_manifest.get("package_id") != plugin.get("name"):
        raise SuitePackageError(
            "Plugin package identity disagrees between plugin.json and suite manifest"
        )
    versions = {
        "plugin.json": plugin.get("version"),
        "FLIGHT-SEARCH-HARNESS-MANIFEST.json": suite_manifest.get("version"),
        "flight_search_harness/PRODUCT-MANIFEST.json": harness_manifest.get("version"),
    }
    if not all(isinstance(value, str) and value for value in versions.values()) or len(
        set(versions.values())
    ) != 1:
        raise SuitePackageError(f"Plugin and Harness versions disagree: {versions}")

    resolver_paths = [
        project_root / "skills" / skill_name / "scripts" / "resolve_harness.py"
        for skill_name in SKILL_REQUEST_CONTRACTS
    ]
    resolver_hashes = {_hash(path) for path in resolver_paths}
    if len(resolver_hashes) != 1:
        raise SuitePackageError("standalone Skill resolver copies are not byte-identical")
    resolver_version, resolver_schemas = _resolver_constants(resolver_paths[0])
    if resolver_version != plugin["version"]:
        raise SuitePackageError(
            "Skill resolver HARNESS_VERSION does not match Plugin version"
        )
    expected_schema_map = {
        skill_name: schema_ref
        for skill_name, (_, schema_ref) in SKILL_REQUEST_CONTRACTS.items()
    }
    if resolver_schemas != expected_schema_map:
        raise SuitePackageError(
            "Skill resolver REQUEST_SCHEMAS does not match the public request contracts"
        )

    required_paths = {
        "plugin.json",
        "FLIGHT-SEARCH-HARNESS-MANIFEST.json",
        "flight_search_harness/PRODUCT-MANIFEST.json",
        "flight_search_harness/request_values.py",
        "flight_search_harness/schemas/requests/common-defs.schema.json",
    }
    for skill_name, (journey_shape, schema_ref) in SKILL_REQUEST_CONTRACTS.items():
        skill_root = f"skills/{skill_name}"
        template_ref = f"{skill_root}/assets/request-template.json"
        required_paths.update(
            {
                f"{skill_root}/SKILL.md",
                f"{skill_root}/agents/openai.yaml",
                template_ref,
                f"{skill_root}/references/product-workflow.md",
                f"{skill_root}/scripts/resolve_harness.py",
                schema_ref,
            }
        )
        template = _read(project_root / template_ref)
        schema = _read(project_root / schema_ref)
        request_version = schema.get("properties", {}).get("request_version", {}).get("const")
        schema_shape = schema.get("properties", {}).get("journey_shape", {}).get("const")
        if template.get("request_version") != request_version or request_version != "4.0":
            raise SuitePackageError(
                f"Skill template and public Schema request_version disagree: {skill_name}"
            )
        if template.get("journey_shape") != journey_shape or schema_shape != journey_shape:
            raise SuitePackageError(
                f"Skill template and public Schema journey_shape disagree: {skill_name}"
            )
        template_intent = template.get("intent")
        schema_intent = schema.get("properties", {}).get("intent", {})
        if not isinstance(template_intent, dict) or not isinstance(schema_intent, dict):
            raise SuitePackageError(
                f"Skill template and public Schema require object intents: {skill_name}"
            )
        schema_required = set(schema_intent.get("required", []))
        if not PUBLIC_SHARED_INTENT_FIELDS <= set(template_intent) or not (
            PUBLIC_SHARED_INTENT_FIELDS <= schema_required
        ):
            raise SuitePackageError(
                f"Skill template and public Schema omit shared intent fields: {skill_name}"
            )
        leaked_fields = sorted(INTERNAL_PRODUCT_INPUT_FIELDS & set(template_intent))
        if leaked_fields:
            raise SuitePackageError(
                f"Skill template leaks Product Kernel input fields: {skill_name}: {leaked_fields}"
            )
    missing_contract_paths = sorted(required_paths - packaged)
    if missing_contract_paths:
        raise SuitePackageError(
            f"distribution manifests do not close over public contracts: {missing_contract_paths}"
        )

    groups = suite_manifest.get("runtime_groups", [])
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        root = _relative(group.get("root"))
        manifest_ref = _relative(group.get("manifest"))
        expected_manifest_ref = f"{root}/PRODUCT-MANIFEST.json"
        if manifest_ref != expected_manifest_ref or manifest_ref not in packaged:
            raise SuitePackageError(
                f"runtime_groups[{index}] manifest is outside its declared Module"
            )


def resolve_package_files(project_root: Path) -> list[str]:
    project_root = project_root.resolve()
    manifest = _read(project_root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json")
    if manifest.get("installable") is not True:
        raise SuitePackageError("suite manifest is not installable")
    files = [_relative(item) for item in manifest.get("root_files", [])]
    groups = manifest.get("runtime_groups")
    if not isinstance(groups, list) or not groups:
        raise SuitePackageError("runtime_groups must be a non-empty array")
    for group in groups:
        if not isinstance(group, dict):
            raise SuitePackageError("runtime group must be an object")
        group_root = _relative(group.get("root"))
        group_manifest_path = _relative(group.get("manifest"))
        nested = _read(project_root / group_manifest_path)
        package_files = nested.get("package_files")
        if not isinstance(package_files, list) or not package_files:
            raise SuitePackageError(
                f"nested manifest has no package_files: {group_manifest_path}"
            )
        files.extend(
            f"{group_root}/{_relative(item)}" for item in package_files
        )
    if len(files) != len(set(files)):
        raise SuitePackageError("suite package contains duplicate paths")
    forbidden = [
        item
        for item in files
        if any(
            token in {part.casefold() for part in PurePosixPath(item).parts}
            for token in {"runs", ".planning", "tests", "fixtures", "__pycache__"}
        )
        or PurePosixPath(item).suffix.casefold() in {".pyc", ".tmp"}
    ]
    if forbidden:
        raise SuitePackageError(f"forbidden runtime paths: {forbidden}")
    missing = [item for item in files if not (project_root / item).is_file()]
    if missing:
        raise SuitePackageError(f"suite files missing: {missing}")
    _validate_distribution_contracts(project_root, manifest, files)
    return files


def _archive_prefix(project_root: Path) -> str:
    plugin = _read(project_root.resolve() / "plugin.json")
    name = plugin.get("name")
    if not isinstance(name, str) or not PLUGIN_NAME_PATTERN.fullmatch(name):
        raise SuitePackageError("plugin.json name must be lower kebab-case")
    return name


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(project_root: Path) -> dict[str, Any]:
    files = resolve_package_files(project_root)
    return {
        "valid": True,
        "project_root": _display_path(project_root),
        "file_count": len(files),
        "package_files": files,
    }


def install(project_root: Path, destination: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    destination = destination.resolve()
    files = resolve_package_files(project_root)
    if destination.exists():
        raise SuitePackageError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    try:
        for item in files:
            source = project_root / item
            target = destination / item
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    mismatches = [
        item
        for item in files
        if _hash(project_root / item) != _hash(destination / item)
    ]
    if mismatches:
        raise SuitePackageError(f"byte verification failed: {mismatches}")
    return {
        "valid": True,
        "destination": _display_path(destination),
        "file_count": len(files),
        "byte_identical": True,
    }


def build_archive(project_root: Path, destination: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    destination = destination.resolve()
    files = resolve_package_files(project_root)
    if destination.exists():
        raise SuitePackageError(f"archive already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = _archive_prefix(project_root)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for item in files:
            data = (project_root / item).read_bytes()
            info = zipfile.ZipInfo(
                f"{prefix}/{item}", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(destination, "r") as archive:
        names = archive.namelist()
        expected = [f"{prefix}/{item}" for item in files]
        if names != expected:
            raise SuitePackageError("archive file order or membership differs")
        mismatches = [
            item
            for item in files
            if hashlib.sha256(archive.read(f"{prefix}/{item}")).hexdigest()
            != _hash(project_root / item)
        ]
    if mismatches:
        raise SuitePackageError(f"archive byte verification failed: {mismatches}")
    return {
        "valid": True,
        "archive": _display_path(destination),
        "file_count": len(files),
        "byte_identical": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "install", "archive"])
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--destination")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate(Path(args.project_root))
        elif args.command == "install":
            if not args.destination:
                raise SuitePackageError("install requires --destination")
            result = install(Path(args.project_root), Path(args.destination))
        else:
            if not args.destination:
                raise SuitePackageError("archive requires --destination")
            result = build_archive(Path(args.project_root), Path(args.destination))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, SuitePackageError) as exc:
        print(f"package_flight_search_harness: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
