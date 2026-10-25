"""Resolve a Skill's shared runtime and journey declaration in a complete Plugin."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from flight_search_harness.suite_layout import SuiteLayout, SuiteLayoutError


class RuntimeResolutionError(RuntimeError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeResolutionError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeResolutionError(f"manifest must be an object: {path}")
    return value


def _root_ref(root: Path, value: Any) -> Path:
    """All manifest references here are relative to the Plugin root."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeResolutionError("manifest reference must be a relative POSIX path")
    parts = PurePosixPath(value)
    if parts.is_absolute() or PureWindowsPath(value).drive or any(p in {"", ".", ".."} for p in value.split("/")):
        raise RuntimeResolutionError(f"unsafe manifest reference: {value}")
    result = (root / value).resolve()
    if not result.is_relative_to(root):
        raise RuntimeResolutionError(f"manifest reference escapes Plugin root: {value}")
    return result


def resolve(
    entry_file: str,
    *,
    harness_version: str,
    product_id: str,
    plugin_root: Path | None = None,
    runtime_data_root: Path | None = None,
) -> dict[str, str]:
    root = plugin_root.resolve() if plugin_root is not None else Path(entry_file).resolve().parents[3]
    suite = _read_object(root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json")
    plugin = _read_object(root / "plugin.json")
    if (suite.get("package_id") != "flight-search-harness"
            or plugin.get("name") != suite.get("package_id")
            or suite.get("version") != harness_version
            or plugin.get("version") != harness_version):
        raise RuntimeResolutionError("Skill requires the matching complete Flight Search Harness Plugin")
    declaration = None
    for group in suite.get("runtime_groups", []):
        candidate = _read_object(_root_ref(root, group.get("manifest")))
        if candidate.get("product_id") == product_id:
            declaration = candidate
            break
    if (not declaration or declaration.get("journey_shape") != product_id
            or declaration.get("version") != harness_version):
        raise RuntimeResolutionError(f"missing matching journey declaration: {product_id}")
    if declaration.get("request_schema_base") != "plugin_root":
        raise RuntimeResolutionError("request_schema_ref must explicitly use plugin_root")
    required = {
        "launcher_ref": root / "scripts" / "run_flight_harness.py",
        "request_schema_ref": _root_ref(root, declaration.get("request_schema_ref")),
        "workflow_ref": root / "docs" / "search-workflow.md",
        "record_quote_template_ref": root / "examples" / "record-quote.json",
        "record_signal_template_ref": root / "examples" / "record-signal.json",
        "query_challenge_template_ref": root / "examples" / "query-challenge.json",
        "record_airport_discovery_template_ref": root / "examples" / "record-airport-discovery.json",
    }
    if missing := [str(path) for path in required.values() if not path.is_file()]:
        raise RuntimeResolutionError("installed Plugin is incomplete: " + ", ".join(missing))
    if runtime_data_root is None:
        try:
            data_root = SuiteLayout.discover(root=root).runtime_data_root()
        except SuiteLayoutError as exc:
            raise RuntimeResolutionError(str(exc)) from exc
    else:
        data_root = runtime_data_root.resolve()
        if data_root == root or data_root.is_relative_to(root) or root.is_relative_to(data_root):
            raise RuntimeResolutionError("runtime_data_root must be separate from the Plugin directory")
    return {
        "harness_version": harness_version,
        "journey_shape": product_id,
        "source": "plugin" if runtime_data_root is None else "cache",
        "harness_root_ref": str(root),
        "runtime_data_root_ref": str(data_root),
        "runs_root_ref": str(data_root / "runs"),
        **{name: str(path) for name, path in required.items()},
    }
