"""Resolve the matching Flight Search Harness runtime for this Skill."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


HARNESS_VERSION = "1.1.0"
REPOSITORY = "flyblock/flight-search-harness"
ARCHIVE_URL = (
    f"https://github.com/{REPOSITORY}/releases/download/v{HARNESS_VERSION}/"
    f"flight-search-harness-v{HARNESS_VERSION}.zip"
)
REQUEST_SCHEMAS = {
    "low-cost-one-way-travel-flight-search": "flight_search_harness/schemas/requests/one-way-request.schema.json",
    "low-cost-random-loop-flight-search": "flight_search_harness/schemas/requests/random-loop-request.schema.json",
    "low-cost-round-trip-flight-search": "flight_search_harness/schemas/requests/round-trip-request.schema.json",
}
INSTALL_LOCK_STALE_SECONDS = 1800


class HarnessResolutionError(RuntimeError):
    pass


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        name = member.filename
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        mode = (member.external_attr >> 16) & 0o170000
        if (
            "\\" in name
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in posix.parts)
            or mode == 0o120000
        ):
            raise HarnessResolutionError(f"unsafe archive member: {name!r}")
        target = (root / Path(*posix.parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HarnessResolutionError(
                f"archive member escapes destination: {name!r}"
            ) from exc
    archive.extractall(root)


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HarnessResolutionError(f"{field} must be a non-empty relative path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise HarnessResolutionError(f"unsafe {field}: {value!r}")
    return posix.as_posix()


def _package_files(root: Path) -> list[str]:
    try:
        suite = json.loads(
            (root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json").read_text(encoding="utf-8")
        )
        plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessResolutionError(f"cannot validate Harness manifests: {exc}") from exc
    if (
        suite.get("package_id") != "flight-search-harness"
        or suite.get("version") != HARNESS_VERSION
        or plugin.get("name") != suite.get("package_id")
        or plugin.get("version") != HARNESS_VERSION
    ):
        raise HarnessResolutionError("downloaded Plugin identity or version is invalid")
    files = [
        _safe_relative(value, f"root_files[{index}]")
        for index, value in enumerate(suite.get("root_files", []))
    ]
    groups = suite.get("runtime_groups")
    if not isinstance(groups, list) or not groups:
        raise HarnessResolutionError("downloaded Plugin has no runtime_groups")
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise HarnessResolutionError(f"runtime_groups[{index}] must be an object")
        group_root = _safe_relative(group.get("root"), f"runtime_groups[{index}].root")
        manifest_ref = _safe_relative(
            group.get("manifest"), f"runtime_groups[{index}].manifest"
        )
        try:
            runtime = json.loads((root / manifest_ref).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessResolutionError(
                f"cannot validate runtime manifest {manifest_ref}: {exc}"
            ) from exc
        package_files = runtime.get("package_files")
        if not isinstance(package_files, list) or not package_files:
            raise HarnessResolutionError(
                f"runtime manifest has no package_files: {manifest_ref}"
            )
        files.extend(
            f"{group_root}/{_safe_relative(value, f'{manifest_ref}.package_files[{item_index}]')}"
            for item_index, value in enumerate(package_files)
        )
    unique = list(dict.fromkeys(files))
    missing = [relative for relative in unique if not (root / relative).is_file()]
    if missing:
        raise HarnessResolutionError(
            "downloaded Plugin manifest references missing files: "
            + ", ".join(missing)
        )
    return unique


def _install_manifest_files(source: Path, destination: Path) -> None:
    for relative in _package_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def _is_matching_harness(root: Path) -> bool:
    manifest_path = root / "FLIGHT-SEARCH-HARNESS-MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("version") != HARNESS_VERSION:
        return False
    try:
        _package_files(root)
    except HarnessResolutionError:
        return False
    return True


def _bundled_harness() -> Path | None:
    for candidate in Path(__file__).resolve().parents:
        if _is_matching_harness(candidate):
            return candidate
    return None


def _cache_destination() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_data) if local_data else Path.home() / ".local" / "share"
    return base / "flight-search-harness" / "runtimes" / HARNESS_VERSION


def _acquire_install_lock(lock_path: Path) -> str:
    token = uuid.uuid4().hex
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                snapshot = lock_path.stat()
                age = time.time() - snapshot.st_mtime
                current = lock_path.stat()
            except FileNotFoundError:
                continue
            if (
                age > INSTALL_LOCK_STALE_SECONDS
                and (snapshot.st_ino, snapshot.st_mtime_ns, snapshot.st_size)
                == (current.st_ino, current.st_mtime_ns, current.st_size)
            ):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise HarnessResolutionError(
                "another Harness installation is in progress; retry shortly"
            ) from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {"token": token, "pid": os.getpid(), "created_at": time.time()},
                    stream,
                    separators=(",", ":"),
                )
                stream.write("\n")
            return token
    raise HarnessResolutionError("could not acquire the Harness installation lock")


def _release_install_lock(lock_path: Path, token: str) -> None:
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if value.get("token") != token:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _download_harness(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{HARNESS_VERSION}.install.lock"
    try:
        lock_token = _acquire_install_lock(lock_path)
    except HarnessResolutionError:
        if _is_matching_harness(destination):
            return destination
        raise
    try:
        with tempfile.TemporaryDirectory(
            prefix="flight-search-harness-", dir=destination.parent
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            archive_path = temporary_root / "release.zip"
            request = urllib.request.Request(
                ARCHIVE_URL,
                headers={"User-Agent": "flight-search-harness-bootstrap"},
            )
            with urllib.request.urlopen(request) as response, archive_path.open(
                "wb"
            ) as stream:
                shutil.copyfileobj(response, stream)

            extracted_root = temporary_root / "source"
            with zipfile.ZipFile(archive_path) as archive:
                _safe_extract(archive, extracted_root)
            manifests = list(extracted_root.glob("*/FLIGHT-SEARCH-HARNESS-MANIFEST.json"))
            if len(manifests) != 1:
                raise HarnessResolutionError(
                    "downloaded archive has no unique Harness root"
                )
            source_root = manifests[0].parent
            _package_files(source_root)
            staged_runtime = temporary_root / "runtime"
            _install_manifest_files(source_root, staged_runtime)
            if not _is_matching_harness(staged_runtime):
                raise HarnessResolutionError(
                    "staged Harness failed manifest-closure validation"
                )
            backup = destination.parent / f".{destination.name}.invalid-{uuid.uuid4().hex}"
            if destination.exists():
                destination.replace(backup)
            staged_runtime.replace(destination)
            if backup.exists():
                shutil.rmtree(backup)
        return destination
    finally:
        _release_install_lock(lock_path, lock_token)


def _display_ref(path: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()
    except ValueError:
        return str(path.resolve())


def main() -> int:
    skill_name = Path(__file__).resolve().parents[1].name
    request_schema = REQUEST_SCHEMAS.get(skill_name)
    if request_schema is None:
        print(f"resolve_harness: unsupported Skill directory: {skill_name}", file=sys.stderr)
        return 2
    try:
        harness_root = _bundled_harness()
        source = "bundled"
        if harness_root is None:
            harness_root = _cache_destination()
            source = "cached"
            if not _is_matching_harness(harness_root):
                harness_root = _download_harness(harness_root)
                source = "downloaded"
        schema_path = harness_root / request_schema
        if not schema_path.is_file():
            raise HarnessResolutionError("matching public request Schema is missing")
        print(
            json.dumps(
                {
                    "harness_version": HARNESS_VERSION,
                    "source": source,
                    "harness_root_ref": _display_ref(harness_root),
                    "launcher_ref": _display_ref(
                        harness_root / "scripts" / "run_flight_harness.py"
                    ),
                    "mission_runtime_ref": _display_ref(
                        harness_root / "docs" / "agent" / "mission-runtime.md"
                    ),
                    "request_schema_ref": _display_ref(schema_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (HarnessResolutionError, OSError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        print(f"resolve_harness: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
