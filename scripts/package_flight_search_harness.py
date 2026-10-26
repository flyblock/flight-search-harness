"""Validate manifest-listed Plugin files, paths, and syntax."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys


class SuitePackageError(ValueError):
    pass


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SuitePackageError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuitePackageError(f"JSON document must be an object: {path}")
    return value


def _relative(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SuitePackageError("package path must be a nonempty relative POSIX path")
    if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise SuitePackageError(f"unsafe package path: {value}")
    return value


def _source(root: Path, relative: str) -> Path:
    path = (root / _relative(relative)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SuitePackageError(f"missing or escaping package file: {relative}")
    return path


def resolve_package_files(project_root: Path) -> list[str]:
    root = project_root.resolve()
    manifest = _read(root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json")
    if manifest.get("installable") is not True:
        raise SuitePackageError("suite manifest is not installable")
    files = []
    for field in ("root_files", "runtime_files"):
        declared = manifest.get(field)
        if not isinstance(declared, list) or not declared:
            raise SuitePackageError(f"suite manifest {field} must be a non-empty array")
        files.extend(_relative(item) for item in declared)
    journeys = manifest.get("journeys")
    if not isinstance(journeys, dict) or not journeys:
        raise SuitePackageError("suite manifest journeys must be a non-empty object")
    for shape, declaration in journeys.items():
        if not shape or not isinstance(declaration, dict):
            raise SuitePackageError(f"invalid journey declaration: {shape!r}")
        schema = _relative(declaration.get("request_schema_ref"))
        if schema not in files:
            raise SuitePackageError(f"Request Schema is not listed in package files: {schema}")
    if not files or len(files) != len(set(files)):
        raise SuitePackageError("package file list is empty or contains duplicates")
    for item in files:
        path = _source(root, item)
        if path.suffix == ".py":
            source = path.read_text(encoding="utf-8")
            compile(ast.parse(source, filename=item), item, "exec")
        elif path.suffix == ".json":
            _read(path)
    return files


def validate(project_root: Path) -> dict:
    files = resolve_package_files(project_root)
    return {"valid": True, "project_root": str(project_root.resolve()), "file_count": len(files), "package_files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    try:
        result = validate(Path(args.project_root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
