"""Resolve the matching Flight Search Harness runtime for this Skill."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


HARNESS_VERSION = "3.9.1"
PRODUCT_ID = "random_loop"


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest must be an object: {path}")
    return value


def _safe_path(root: Path, value: str) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError(f"unsafe relative path: {value!r}")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes Plugin root: {value!r}")
    return path


def _validate_plugin(root: Path) -> None:
    """Check manifests and their files without importing downloaded code."""
    suite = _read_object(root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json")
    plugin = _read_object(root / "plugin.json")
    if (suite.get("package_id") != "flight-search-harness"
            or plugin.get("name") != suite.get("package_id")
            or suite.get("version") != HARNESS_VERSION
            or plugin.get("version") != HARNESS_VERSION):
        raise ValueError(f"Skill requires Flight Search Harness Plugin {HARNESS_VERSION}")
    files = suite.get("root_files")
    groups = suite.get("runtime_groups")
    if not isinstance(files, list) or not files or not isinstance(groups, list) or not groups:
        raise ValueError("Plugin manifest must list its files and runtime groups")
    files = list(files) + ["scripts/resolve_skill_runtime.py", "scripts/run_flight_harness.py"]
    found_product = False
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("runtime group must be an object")
        prefix = group.get("root")
        group_root = _safe_path(root, prefix)
        manifest_path = _safe_path(root, group.get("manifest"))
        if manifest_path != group_root / "PRODUCT-MANIFEST.json":
            raise ValueError("runtime manifest must belong to its declared root")
        declaration = _read_object(manifest_path)
        if declaration.get("version") != HARNESS_VERSION:
            raise ValueError(f"runtime manifest version must be {HARNESS_VERSION}: {manifest_path}")
        package_files = declaration.get("package_files")
        if not isinstance(package_files, list) or not package_files:
            raise ValueError(f"runtime manifest must list its files: {manifest_path}")
        for item in package_files:
            path = _safe_path(group_root, item)
            if not path.is_file():
                raise ValueError(f"installed Plugin is incomplete: {path}")
        if declaration.get("product_id") == PRODUCT_ID:
            if (declaration.get("journey_shape") != PRODUCT_ID
                    or declaration.get("request_schema_base") != "plugin_root"):
                raise ValueError(f"invalid journey declaration: {PRODUCT_ID}")
            files.append(declaration.get("request_schema_ref"))
            found_product = True
    if not found_product:
        raise ValueError(f"missing matching journey declaration: {PRODUCT_ID}")
    for item in files:
        path = _safe_path(root, item)
        if not path.is_file():
            raise ValueError(f"installed Plugin is incomplete: {path}")


def _is_matching_plugin(root: Path) -> bool:
    try:
        _validate_plugin(root)
        return True
    except (OSError, ValueError):
        return False


def _extract(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        entries = []
        for info in archive.infolist():
            path = _safe_path(destination, info.filename[:-1] if info.is_dir() else info.filename)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"archive links and special files are not allowed: {info.filename}")
            entries.append((info, path))
        for info, path in entries:
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("archive must contain one top-level Plugin directory")
    return roots[0]


def _download_plugin(base: Path) -> Path:
    plugins = base / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    cache = plugins / HARNESS_VERSION
    if cache.resolve().parent != plugins.resolve():
        raise ValueError(f"cache path must stay inside {plugins}")
    source = os.environ.get("FLIGHT_SEARCH_HARNESS_ARCHIVE_URL") or (
        f"https://github.com/flyblock/flight-search-harness/archive/refs/tags/v{HARNESS_VERSION}.zip"
    )
    with tempfile.TemporaryDirectory(prefix=f".{HARNESS_VERSION}-", dir=plugins) as temporary:
        work = Path(temporary).resolve()
        archive_path = work / "source.zip"
        if urllib.parse.urlsplit(source).scheme in {"http", "https", "file"}:
            with urllib.request.urlopen(source, timeout=30) as response, archive_path.open("wb") as target:
                shutil.copyfileobj(response, target)
        else:
            shutil.copyfile(Path(source).expanduser(), archive_path)
        extracted = work / "extracted"
        extracted.mkdir()
        plugin_root = _extract(archive_path, extracted)
        _validate_plugin(plugin_root)
        # Another caller may have populated this version while we downloaded.
        if _is_matching_plugin(cache):
            return cache
        previous = work / "previous"
        if cache.exists():
            cache.rename(previous)
        try:
            plugin_root.rename(cache)
        except OSError:
            if previous.exists() and not cache.exists():
                previous.rename(cache)
            if not _is_matching_plugin(cache):
                raise
        print(f"Downloaded Flight Search Harness {HARNESS_VERSION} from {source} to {cache}", file=sys.stderr)
    return cache


def main() -> int:
    sys.dont_write_bytecode = True
    try:
        plugin_root = Path(__file__).resolve().parents[3]
        data_root = None
        if not _is_matching_plugin(plugin_root):
            base = Path(os.environ.get("FLIGHT_SEARCH_HARNESS_HOME") or "~/.flight-search-harness").expanduser().resolve()
            plugin_root = base / "plugins" / HARNESS_VERSION
            if not _is_matching_plugin(plugin_root):
                plugin_root = _download_plugin(base)
            data_root = base / "data"
        sys.path.insert(0, str(plugin_root))
        from scripts.resolve_skill_runtime import resolve

        value = resolve(
            __file__,
            harness_version=HARNESS_VERSION,
            product_id=PRODUCT_ID,
            plugin_root=plugin_root,
            runtime_data_root=data_root,
        )
    except (OSError, ValueError, RuntimeError, ImportError, IndexError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        hint = (
            f"Clone or download release v{HARNESS_VERSION} into <base>/plugins/{HARNESS_VERSION} "
            "and set FLIGHT_SEARCH_HARNESS_HOME to <base>, or install the full repository."
        )
        print(json.dumps({"error": str(exc), "hint": hint}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
