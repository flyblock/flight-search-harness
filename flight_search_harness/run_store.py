"""Filesystem Run Store with one lifecycle authority and optional sidecars."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import HARNESS_VERSION, HarnessProtocolError


RUN_VERSION = "3.0"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
RUN_STATUSES = {"initializing", "running", "draft", "blocked", "failed", "completed"}
RUN_STAGING_FILE = ".initializing-run.json"


class RunBusyError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessProtocolError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessProtocolError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class RunStore:
    """Own the Run lifecycle without reading Product Session internals."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _validated_run_id(self, run_id: str) -> str:
        if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise HarnessProtocolError(f"invalid run_id: {run_id!r}")
        return run_id

    def run_directory(self, run_id: str) -> Path:
        return self.root / self._validated_run_id(run_id)

    def product_directory(self, run_id: str) -> Path:
        return self.run_directory(run_id) / "product"

    def new_run_id(self) -> str:
        return self._validated_run_id(
            f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        )

    def create(
        self,
        request: dict[str, Any],
        product_id: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        chosen_id = self._validated_run_id(run_id) if run_id else self.new_run_id()
        directory = self.run_directory(chosen_id)
        staging_path = directory / RUN_STAGING_FILE
        staging: dict[str, Any]
        if directory.exists():
            if (directory / "run.json").exists():
                raise HarnessProtocolError(f"run already exists: {chosen_id}")
            self._remove_staging_temps(directory)
            if staging_path.is_file():
                staging = read_json(staging_path)
                if (
                    staging.get("staging_version") != "1.0"
                    or staging.get("run_version") != RUN_VERSION
                    or staging.get("run_id") != chosen_id
                    or staging.get("product_id") != product_id
                    or staging.get("request") != request
                    or not isinstance(staging.get("created_at"), str)
                ):
                    raise HarnessProtocolError(
                        f"initializing Run identity mismatch: {chosen_id}"
                    )
            elif self._is_safe_partial_run_layout(directory):
                staging = self._run_staging_value(chosen_id, request, product_id)
                write_json_atomic(staging_path, staging)
            else:
                raise HarnessProtocolError(
                    f"existing directory is not a recoverable initializing Run: {chosen_id}"
                )
        else:
            directory.mkdir(parents=True, exist_ok=False)
            staging = self._run_staging_value(chosen_id, request, product_id)
            write_json_atomic(staging_path, staging)
        for relative in ("product", "browser", "result"):
            (directory / relative).mkdir(parents=True, exist_ok=True)
        now = str(staging["created_at"])
        manifest = {
            "run_version": RUN_VERSION,
            "harness_version": HARNESS_VERSION,
            "run_id": chosen_id,
            "product_id": product_id,
            "journey_shape": request["journey_shape"],
            "status": "initializing",
            "request": copy.deepcopy(request),
            "created_at": now,
            "updated_at": now,
            "last_outcome": None,
            "mission_ref": "mission/mission.json",
            "product_session_ref": "product/session.json",
        }
        self.save_manifest(manifest)
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            # A completed run.json is authoritative; a stale private staging
            # marker is harmless and never selected over it.
            pass
        try:
            self.append_event(manifest, "run_created", product_id=product_id)
        except Exception:
            pass
        return manifest

    @staticmethod
    def _run_staging_value(
        run_id: str,
        request: dict[str, Any],
        product_id: str,
    ) -> dict[str, Any]:
        return {
            "staging_version": "1.0",
            "run_version": RUN_VERSION,
            "run_id": run_id,
            "product_id": product_id,
            "request": copy.deepcopy(request),
            "created_at": utc_now(),
        }

    @staticmethod
    def _is_safe_partial_run_layout(directory: Path) -> bool:
        """Recognize only the empty layout created before run.json existed."""

        allowed = {"product", "browser", "result", ".run.lock"}
        entries = list(directory.iterdir())
        if any(path.name not in allowed for path in entries):
            return False
        for name in ("product", "browser", "result"):
            path = directory / name
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                return False
        lock_path = directory / ".run.lock"
        return not lock_path.exists() or lock_path.is_file()

    @staticmethod
    def _remove_staging_temps(directory: Path) -> None:
        """Remove only atomic-write residue for this Store-owned staging file."""

        pattern = re.compile(r"^\.\.initializing-run\.json\.[A-Za-z0-9_-]+\.tmp$")
        for path in directory.iterdir():
            if pattern.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_file():
                raise HarnessProtocolError(
                    f"unsafe Run staging residue is not a regular file: {path.name}"
                )
            try:
                path.unlink()
            except OSError as exc:
                raise HarnessProtocolError(
                    f"cannot remove Run staging residue {path.name}: {exc}"
                ) from exc

    def recover_initializing(self, run_id: str) -> dict[str, Any]:
        """Complete a staged Run manifest without guessing caller intent."""

        run_id = self._validated_run_id(run_id)
        manifest_path = self.run_directory(run_id) / "run.json"
        if manifest_path.is_file():
            return self.load_manifest(run_id)
        staging_path = self.run_directory(run_id) / RUN_STAGING_FILE
        if not staging_path.is_file():
            raise HarnessProtocolError(f"Run manifest does not exist: {run_id}")
        staging = read_json(staging_path)
        request = staging.get("request")
        product_id = staging.get("product_id")
        if not isinstance(request, dict) or not isinstance(product_id, str):
            raise HarnessProtocolError(f"initializing Run staging is invalid: {run_id}")
        return self.create(request, product_id, run_id=run_id)

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        manifest = read_json(self.run_directory(run_id) / "run.json")
        return self._validate_manifest(manifest, expected_run_id=run_id)

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        value = self._validate_manifest(copy.deepcopy(manifest))
        value["updated_at"] = utc_now()
        write_json_atomic(self.run_directory(value["run_id"]) / "run.json", value)
        # Keep the caller synchronized because lifecycle code often saves and
        # then appends an optional event with the same object.
        manifest.clear()
        manifest.update(copy.deepcopy(value))

    def _validate_manifest(
        self,
        manifest: dict[str, Any],
        *,
        expected_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            raise HarnessProtocolError("Run manifest must be an object")
        version = manifest.get("run_version")
        if version != RUN_VERSION:
            raise HarnessProtocolError(
                f"legacy Run protocol {version!r} cannot be resumed; start a new protocol 3 Run"
            )
        if manifest.get("harness_version") != HARNESS_VERSION:
            raise HarnessProtocolError(
                f"Run manifest harness_version must be {HARNESS_VERSION}"
            )
        run_id = self._validated_run_id(str(manifest.get("run_id", "")))
        if expected_run_id is not None and run_id != expected_run_id:
            raise HarnessProtocolError("Run manifest identity mismatch")
        if not isinstance(manifest.get("product_id"), str) or not manifest["product_id"]:
            raise HarnessProtocolError("Run manifest product_id is required")
        if manifest.get("journey_shape") not in {"random_loop", "one_way", "round_trip"}:
            raise HarnessProtocolError("Run manifest journey_shape is unsupported")
        if manifest.get("status") not in RUN_STATUSES:
            raise HarnessProtocolError("Run manifest status is unsupported")
        if not isinstance(manifest.get("request"), dict):
            raise HarnessProtocolError("Run manifest request must be an object")
        if not isinstance(manifest.get("created_at"), str) or not isinstance(
            manifest.get("updated_at"), str
        ):
            raise HarnessProtocolError("Run manifest timestamps are required")
        if manifest.get("last_outcome") is not None and not isinstance(
            manifest.get("last_outcome"), dict
        ):
            raise HarnessProtocolError("Run last_outcome must be an object or null")
        canonical_references = {
            "mission_ref": "mission/mission.json",
            "product_session_ref": "product/session.json",
        }
        for reference_field, expected_reference in canonical_references.items():
            if manifest.get(reference_field) != expected_reference:
                raise HarnessProtocolError(
                    f"Run manifest {reference_field} must be {expected_reference}"
                )
        optional_references = {"result_ref": "product/final.json"}
        for reference_field, expected_reference in optional_references.items():
            if (
                reference_field in manifest
                and manifest.get(reference_field) != expected_reference
            ):
                raise HarnessProtocolError(
                    f"Run manifest {reference_field} must be {expected_reference}"
                )
        allowed = {
            "run_version",
            "harness_version",
            "run_id",
            "product_id",
            "journey_shape",
            "status",
            "request",
            "created_at",
            "updated_at",
            "last_outcome",
            "mission_ref",
            "product_session_ref",
            "result_ref",
        }
        extras = sorted(set(manifest) - allowed)
        if extras:
            raise HarnessProtocolError(f"protocol 3 Run has unsupported fields: {extras}")
        return manifest

    def append_event(
        self, manifest: dict[str, Any], event_type: str, **details: Any
    ) -> dict[str, Any]:
        """Append non-authoritative observability without rewriting Run state."""

        target = self.run_directory(manifest["run_id"]) / "events.jsonl"
        sequence = 1
        if target.exists():
            events, torn_tail = self._read_event_records(target)
            if torn_tail:
                with target.open("w", encoding="utf-8", newline="\n") as stream:
                    for event in events:
                        stream.write(
                            json.dumps(
                                event,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
            if events:
                try:
                    sequence = int(events[-1]["sequence"]) + 1
                except (KeyError, TypeError, ValueError) as exc:
                    raise HarnessProtocolError(
                        f"events.jsonl has an invalid final sequence: {exc}"
                    ) from exc
        event = {
            "event_version": "1.0",
            "sequence": sequence,
            "type": event_type,
            "at": utc_now(),
            **details,
        }
        needs_delimiter = False
        if target.exists() and target.stat().st_size:
            with target.open("rb") as existing_stream:
                existing_stream.seek(-1, os.SEEK_END)
                needs_delimiter = existing_stream.read(1) not in {b"\n", b"\r"}
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            if needs_delimiter:
                stream.write("\n")
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def write_product_final_result(self, run_id: str, result: dict[str, Any]) -> str:
        """Persist the sole authoritative full result in the Product Module."""

        target = self.product_directory(run_id) / "final.json"
        if target.exists():
            existing = read_json(target)
            if existing != result:
                raise HarnessProtocolError("Product final result is immutable")
        else:
            write_json_atomic(target, result)
        return target.relative_to(self.run_directory(run_id)).as_posix()

    def read_result(self, run_id: str, reference: str) -> dict[str, Any]:
        run_root = self.run_directory(run_id).resolve()
        target = (run_root / reference).resolve()
        allowed = {(run_root / "product" / "final.json").resolve()}
        if target not in allowed:
            raise HarnessProtocolError("result reference is not an owned result artifact")
        return read_json(target)

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        target = self.run_directory(run_id) / "events.jsonl"
        if not target.exists():
            return []
        events, _ = self._read_event_records(target)
        return events

    @staticmethod
    def _read_event_records(target: Path) -> tuple[list[dict[str, Any]], bool]:
        raw = target.read_bytes()
        torn_tail = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.end < len(raw) - 3:
                raise HarnessProtocolError(
                    f"events.jsonl has invalid UTF-8 before its final record: {exc}"
                ) from exc
            text = raw[: exc.start].decode("utf-8")
            torn_tail = True
        lines = [
            line
            for line in text.splitlines()
            if line.strip()
        ]
        events: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    return events, True
                raise HarnessProtocolError(
                    f"events.jsonl has a corrupt non-final record: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise HarnessProtocolError("events.jsonl records must be objects")
            events.append(event)
        return events, torn_tail

    def metrics(self, run_id: str) -> dict[str, Any]:
        """Derive observability from sidecars without treating it as Run state."""

        manifest = self.load_manifest(run_id)
        run_directory = self.run_directory(run_id)
        events_path = run_directory / "events.jsonl"
        events = self.read_events(run_id)
        event_span_seconds = None
        if events:
            try:
                started = datetime.fromisoformat(str(events[0]["at"]))
                ended = datetime.fromisoformat(str(events[-1]["at"]))
                event_span_seconds = round((ended - started).total_seconds(), 3)
            except (KeyError, TypeError, ValueError):
                pass

        def total_bytes(paths: list[Path]) -> int:
            return sum(path.stat().st_size for path in paths if path.is_file())

        metric_files = list((run_directory / "mission/metrics/checkpoints").glob("*.json"))
        aggregate: dict[str, Any] = {"items": {}}
        for path in metric_files:
            try:
                metrics = read_json(path).get("metrics", {})
            except HarnessProtocolError:
                continue
            if not isinstance(metrics, dict):
                continue
            for key, value in metrics.items():
                if key == "items" or isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    aggregate[key] = aggregate.get(key, 0) + value
            for identifier, values in metrics.get("items", {}).items() if isinstance(metrics.get("items"), dict) else []:
                if not isinstance(values, dict):
                    continue
                item_target = aggregate["items"].setdefault(identifier, {})
                for key, value in values.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        item_target[key] = item_target.get(key, 0) + value
                    else:
                        item_target[key] = copy.deepcopy(value)
        mission_path = run_directory / "mission/mission.json"
        mission = read_json(mission_path) if mission_path.exists() else None
        leases = list((run_directory / "mission/leases").glob("lease-*.json"))
        items = list((run_directory / "mission/items").glob("*.json"))
        raw_receipts = list((run_directory / "mission/receipts/raw").glob("*.json"))
        session_path = run_directory / str(manifest["product_session_ref"])
        full_result_path = run_directory / "product/final.json"
        return {
            "metrics_version": "2.0",
            "run_id": run_id,
            "status": manifest["status"],
            "journey_shape": manifest["journey_shape"],
            "event_count": len(events),
            "event_bytes": events_path.stat().st_size if events_path.exists() else 0,
            "event_span_seconds": event_span_seconds,
            "session_bytes": session_path.stat().st_size if session_path.exists() else 0,
            "full_result_bytes": full_result_path.stat().st_size if full_result_path.exists() else 0,
            "mission_version": mission.get("mission_version") if mission else None,
            "mission_revision": mission.get("revision") if mission else None,
            "mission_has_active_lease": bool(mission and mission.get("active_lease_ref")),
            "mission_lease_files": len(leases),
            "mission_item_files": len(items),
            "mission_raw_receipt_files": len(raw_receipts),
            "metrics_checkpoint_files": len(metric_files),
            "mission_metrics": aggregate,
        }

    @contextmanager
    def run_lock(self, run_id: str):
        """Hold one OS advisory lock for all transitions in a Run."""

        target = self.run_directory(run_id) / ".run.lock"
        target.parent.mkdir(parents=True, exist_ok=True)
        stream = target.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RunBusyError(f"Run is already active: {run_id}") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RunBusyError(f"Run is already active: {run_id}") from exc
            yield
        finally:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            stream.close()
