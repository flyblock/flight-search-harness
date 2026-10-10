"""Manifest-driven discovery of Flight Search Harness runtime roots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SUITE_MANIFEST_NAME = "FLIGHT-SEARCH-HARNESS-MANIFEST.json"
RUNTIME_MANIFEST_NAME = "PRODUCT-MANIFEST.json"
SUITE_PACKAGE_ID = "flight-search-harness"


class SuiteLayoutError(RuntimeError):
    """Raised when a Flight Search Harness Plugin layout is invalid."""


def _display_path(path: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()
    except ValueError:
        return path.name


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SuiteLayoutError(f"{label} is missing: {_display_path(path)}") from exc
    except OSError as exc:
        raise SuiteLayoutError(
            f"cannot read {label}: {_display_path(path)}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SuiteLayoutError(
            f"{label} is not valid JSON: {_display_path(path)}: "
            f"line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(value, dict):
        raise SuiteLayoutError(
            f"{label} must contain a JSON object: {_display_path(path)}"
        )
    return value


def _resolve_relative_path(root: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SuiteLayoutError(f"{field} must be a non-empty relative path")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        "\\" in value
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise SuiteLayoutError(f"{field} is not a safe relative path: {value!r}")
    resolved = (root / Path(*posix_path.parts)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SuiteLayoutError(f"{field} escapes the suite root: {value!r}") from exc
    return resolved


def _runtime_roots(root: Path) -> dict[str, Path]:
    manifest_path = root / SUITE_MANIFEST_NAME
    manifest = _load_json_object(manifest_path, label="suite manifest")
    if manifest.get("package_id") != SUITE_PACKAGE_ID:
        raise SuiteLayoutError(
            "suite manifest package_id must be "
            f"{SUITE_PACKAGE_ID!r}: {_display_path(manifest_path)}"
        )
    groups = manifest.get("runtime_groups")
    if not isinstance(groups, list) or not groups:
        raise SuiteLayoutError("suite manifest runtime_groups must be a non-empty array")

    runtime_roots: dict[str, Path] = {}
    for index, group in enumerate(groups):
        field_prefix = f"runtime_groups[{index}]"
        if not isinstance(group, dict):
            raise SuiteLayoutError(f"{field_prefix} must be an object")
        runtime_root = _resolve_relative_path(
            root, group.get("root"), field=f"{field_prefix}.root"
        )
        runtime_manifest_path = _resolve_relative_path(
            root, group.get("manifest"), field=f"{field_prefix}.manifest"
        )
        try:
            runtime_manifest_path.relative_to(runtime_root)
        except ValueError as exc:
            raise SuiteLayoutError(
                f"{field_prefix}.manifest must be inside {field_prefix}.root"
            ) from exc
        if runtime_manifest_path.name != RUNTIME_MANIFEST_NAME:
            raise SuiteLayoutError(
                f"{field_prefix}.manifest must name {RUNTIME_MANIFEST_NAME}"
            )
        runtime_manifest = _load_json_object(
            runtime_manifest_path,
            label=f"runtime manifest for {field_prefix}",
        )
        runtime_id = runtime_manifest.get("product_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise SuiteLayoutError(
                f"runtime manifest product_id must be a non-empty string: "
                f"{_display_path(runtime_manifest_path)}"
            )
        if runtime_id in runtime_roots:
            raise SuiteLayoutError(f"duplicate runtime product_id: {runtime_id!r}")
        runtime_roots[runtime_id] = runtime_root
    return runtime_roots


@dataclass(frozen=True, slots=True)
class SuiteLayout:
    """Resolved root and runtime mapping for one Flight Search Harness Plugin."""

    root: Path

    @classmethod
    def discover(
        cls,
        *,
        root: str | Path | None = None,
        anchor: str | Path | None = None,
    ) -> "SuiteLayout":
        """Discover and validate a suite from an explicit root or an anchor."""

        if root is not None:
            candidate = Path(root).resolve()
            if not candidate.is_dir():
                raise SuiteLayoutError(
                    f"suite root is not a directory: {_display_path(candidate)}"
                )
            _runtime_roots(candidate)
            return cls(candidate)

        anchor_path = Path(anchor).resolve() if anchor is not None else Path(__file__).resolve()
        start = anchor_path if anchor_path.is_dir() else anchor_path.parent
        for candidate in (start, *start.parents):
            if (candidate / SUITE_MANIFEST_NAME).is_file():
                _runtime_roots(candidate)
                return cls(candidate)
        raise SuiteLayoutError(
            f"could not find {SUITE_MANIFEST_NAME} from anchor: "
            f"{_display_path(anchor_path)}"
        )

    def runtime_root(self, runtime_id: str) -> Path:
        """Return the root declared by the runtime's nested manifest identity."""

        runtime_roots = _runtime_roots(self.root)
        try:
            return runtime_roots[runtime_id]
        except KeyError as exc:
            available = ", ".join(sorted(runtime_roots))
            raise SuiteLayoutError(
                f"unknown runtime_id {runtime_id!r}; available runtime IDs: {available}"
            ) from exc
