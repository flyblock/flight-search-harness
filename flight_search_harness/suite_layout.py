"""Manifest-driven discovery of the Flight Search Harness Plugin."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SUITE_MANIFEST_NAME = "FLIGHT-SEARCH-HARNESS-MANIFEST.json"
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


def _validate_suite(root: Path) -> None:
    manifest_path = root / SUITE_MANIFEST_NAME
    manifest = _load_json_object(manifest_path, label="suite manifest")
    if manifest.get("package_id") != SUITE_PACKAGE_ID:
        raise SuiteLayoutError(
            "suite manifest package_id must be "
            f"{SUITE_PACKAGE_ID!r}: {_display_path(manifest_path)}"
        )
    files = manifest.get("runtime_files")
    if not isinstance(files, list) or not files:
        raise SuiteLayoutError("suite manifest runtime_files must be a non-empty array")
    for index, value in enumerate(files):
        path = _resolve_relative_path(root, value, field=f"runtime_files[{index}]")
        if not path.is_file():
            raise SuiteLayoutError(f"runtime file is missing: {_display_path(path)}")
    journeys = manifest.get("journeys")
    if not isinstance(journeys, dict) or not journeys:
        raise SuiteLayoutError("suite manifest journeys must be a non-empty object")
    for shape, declaration in journeys.items():
        if not shape or not isinstance(declaration, dict):
            raise SuiteLayoutError(f"invalid journey declaration: {shape!r}")
        schema = _resolve_relative_path(
            root, declaration.get("request_schema_ref"),
            field=f"journeys.{shape}.request_schema_ref",
        )
        if not schema.is_file():
            raise SuiteLayoutError(f"Request Schema is missing: {_display_path(schema)}")


@dataclass(frozen=True, slots=True)
class SuiteLayout:
    """Resolved root for one Flight Search Harness Plugin."""

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
            _validate_suite(candidate)
            return cls(candidate)

        anchor_path = Path(anchor).resolve() if anchor is not None else Path(__file__).resolve()
        start = anchor_path if anchor_path.is_dir() else anchor_path.parent
        for candidate in (start, *start.parents):
            if (candidate / SUITE_MANIFEST_NAME).is_file():
                _validate_suite(candidate)
                return cls(candidate)
        raise SuiteLayoutError(
            f"could not find {SUITE_MANIFEST_NAME} from anchor: "
            f"{_display_path(anchor_path)}"
        )

    def runtime_data_root(self) -> Path:
        """Resolve generated data outside the authored Plugin directory."""

        manifest = _load_json_object(
            self.root / SUITE_MANIFEST_NAME, label="suite manifest"
        )
        configured = manifest.get("runtime_data_root")
        if not isinstance(configured, str) or not configured.strip():
            raise SuiteLayoutError("suite manifest runtime_data_root must be a path")
        resolved = (self.root / configured).resolve()
        if resolved == self.root or self.root in resolved.parents or resolved in self.root.parents:
            raise SuiteLayoutError("runtime_data_root must be separate from the Plugin directory")
        return resolved
