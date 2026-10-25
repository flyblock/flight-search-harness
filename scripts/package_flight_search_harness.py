"""Package manifest-listed sources; validate paths, syntax, and copied bytes."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import zipfile


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
    files = [_relative(item) for item in manifest.get("root_files", [])]
    for group in manifest.get("runtime_groups", []):
        prefix = _relative(group["root"])
        manifest_ref = _relative(group["manifest"])
        if manifest_ref != prefix + "/PRODUCT-MANIFEST.json":
            raise SuitePackageError("nested manifest must belong to its declared root")
        nested = _read(_source(root, manifest_ref))
        files.extend(prefix + "/" + _relative(item) for item in nested["package_files"])
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


def install(project_root: Path, destination: Path) -> dict:
    root, destination = project_root.resolve(), destination.resolve()
    files = resolve_package_files(root)
    if destination.exists():
        raise SuitePackageError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    for item in files:
        target = destination / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_source(root, item), target)
        if target.read_bytes() != _source(root, item).read_bytes():
            raise SuitePackageError(f"installed bytes differ: {item}")
    return {"valid": True, "destination": str(destination), "file_count": len(files), "byte_identical": True}


def build_archive(project_root: Path, destination: Path) -> dict:
    root, destination = project_root.resolve(), destination.resolve()
    files = resolve_package_files(root)
    prefix = _read(root / "plugin.json").get("name")
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", prefix):
        raise SuitePackageError("plugin name must be lower kebab-case")
    if destination.exists():
        raise SuitePackageError(f"archive already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            info = zipfile.ZipInfo(prefix + "/" + item, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _source(root, item).read_bytes())
    with zipfile.ZipFile(destination) as archive:
        expected = [prefix + "/" + item for item in files]
        if archive.namelist() != expected:
            raise SuitePackageError("archive membership differs from manifest")
        for item in files:
            if archive.read(prefix + "/" + item) != _source(root, item).read_bytes():
                raise SuitePackageError(f"archive bytes differ: {item}")
    return {"valid": True, "archive": str(destination), "file_count": len(files), "byte_identical": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "install", "archive"])
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--destination")
    args = parser.parse_args(argv)
    try:
        root = Path(args.project_root).resolve()
        if args.command == "validate":
            result = validate(root)
        else:
            if args.destination:
                destination = Path(args.destination)
            else:
                sys.path.insert(0, str(root))
                from flight_search_harness.suite_layout import SuiteLayout
                manifest = _read(root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json")
                name = f"{manifest['package_id']}-{manifest['version']}"
                if args.command == "archive":
                    name += ".zip"
                destination = SuiteLayout.discover(root=root).runtime_data_root() / "releases" / name
            result = (install if args.command == "install" else build_archive)(root, destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
