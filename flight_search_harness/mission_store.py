"""Atomic persistence for the protocol 3 Search Mission execution cursor."""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import HarnessProtocolError
from .mission_contracts import (
    LEASE_VERSION,
    MISSION_VERSION,
    item_id,
    receipt_id,
    validate_receipt,
    validate_work_item,
)
from .run_store import RunBusyError, RunStore, read_json, write_json_atomic
from .worker_projection import digest, project_item


MISSION_STAGING_FILE = ".initializing-mission.json"


class MissionStore:
    """Own the subordinate work cursor, Lease, Receipt, and admission artifacts."""

    def __init__(self, run_store: RunStore):
        self.runs = run_store

    def mission_directory(self, run_id: str) -> Path:
        return self.runs.run_directory(run_id) / "mission"

    def mission_path(self, run_id: str) -> Path:
        return self.mission_directory(run_id) / "mission.json"

    def pending_effect_path(self, run_id: str) -> Path:
        return self.mission_directory(run_id) / "pending-effect.json"

    def create(self, run_manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run_manifest["run_id"])
        root = self.mission_directory(run_id)
        staging_path = root / MISSION_STAGING_FILE
        if root.exists():
            if self.mission_path(run_id).is_file():
                raise HarnessProtocolError(f"mission already exists: {run_id}")
            self._remove_staging_temps(root)
            if staging_path.is_file():
                staging = read_json(staging_path)
                if (
                    staging.get("staging_version") != "1.0"
                    or staging.get("mission_version") != MISSION_VERSION
                    or staging.get("run_id") != run_id
                ):
                    raise HarnessProtocolError(
                        f"initializing Search Mission identity mismatch: {run_id}"
                    )
            elif self._is_safe_partial_mission_layout(root):
                write_json_atomic(
                    staging_path,
                    {
                        "staging_version": "1.0",
                        "mission_version": MISSION_VERSION,
                        "run_id": run_id,
                    },
                )
            else:
                raise HarnessProtocolError(
                    f"existing directory is not a recoverable Search Mission: {run_id}"
                )
        else:
            root.mkdir(parents=True, exist_ok=False)
            write_json_atomic(
                staging_path,
                {
                    "staging_version": "1.0",
                    "mission_version": MISSION_VERSION,
                    "run_id": run_id,
                },
            )
        for relative in (
            "items",
            "leases",
            "receipts/raw",
            "receipts/admissions",
            "metrics/checkpoints",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        mission = {
            "mission_version": MISSION_VERSION,
            "run_id": run_id,
            "revision": 0,
            "next_lease_sequence": 1,
            "active_lease_ref": None,
            "work_items": {},
        }
        self.save(mission)
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        return mission

    @staticmethod
    def _is_safe_partial_mission_layout(root: Path) -> bool:
        """Recognize the empty directory tree created before mission.json."""

        allowed_directories = {
            "items",
            "leases",
            "receipts",
            "receipts/raw",
            "receipts/admissions",
            "metrics",
            "metrics/checkpoints",
        }
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if not path.is_dir() or relative not in allowed_directories:
                return False
        return True

    @staticmethod
    def _remove_staging_temps(root: Path) -> None:
        """Remove only atomic-write residue for the Mission staging marker."""

        pattern = re.compile(
            r"^\.\.initializing-mission\.json\.[A-Za-z0-9_-]+\.tmp$"
        )
        for path in root.iterdir():
            if pattern.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_file():
                raise HarnessProtocolError(
                    f"unsafe Mission staging residue is not a regular file: {path.name}"
                )
            try:
                path.unlink()
            except OSError as exc:
                raise HarnessProtocolError(
                    f"cannot remove Mission staging residue {path.name}: {exc}"
                ) from exc

    def load(self, run_id: str) -> dict[str, Any]:
        return self._validate(read_json(self.mission_path(run_id)), run_id)

    def save(self, mission: dict[str, Any]) -> None:
        value = self._validate(copy.deepcopy(mission))
        write_json_atomic(self.mission_path(value["run_id"]), value)

    def _validate(
        self, raw: Any, expected_run_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise HarnessProtocolError("Search Mission cursor must be an object")
        version = raw.get("mission_version")
        if version != MISSION_VERSION:
            raise HarnessProtocolError(
                f"legacy Search Mission protocol {version!r} cannot be resumed; "
                "start a new protocol 3 Run"
            )
        result = copy.deepcopy(raw)
        allowed = {
            "mission_version",
            "run_id",
            "revision",
            "next_lease_sequence",
            "active_lease_ref",
            "work_items",
        }
        extras = sorted(set(result) - allowed)
        if extras:
            raise HarnessProtocolError(
                f"protocol 3 Search Mission has unsupported fields: {extras}"
            )
        run_id = str(result.get("run_id") or "")
        if not run_id or (expected_run_id is not None and run_id != expected_run_id):
            raise HarnessProtocolError("Search Mission Run identity mismatch")
        for field in ("revision", "next_lease_sequence"):
            value = result.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessProtocolError(f"Search Mission {field} must be non-negative")
        if result["next_lease_sequence"] < 1:
            raise HarnessProtocolError("next_lease_sequence must be at least one")
        active_ref = result.get("active_lease_ref")
        if active_ref is not None and (
            not isinstance(active_ref, str) or not active_ref.startswith("mission/leases/")
        ):
            raise HarnessProtocolError("active_lease_ref must be a Mission Lease reference")
        work_items = result.get("work_items")
        if not isinstance(work_items, dict):
            raise HarnessProtocolError("Search Mission work_items must be an object")
        for identifier, index in work_items.items():
            item_id(identifier)
            if not isinstance(index, dict):
                raise HarnessProtocolError("Search Mission Work Item index must be an object")
            if not isinstance(index.get("item_ref"), str) or not isinstance(
                index.get("item_hash"), str
            ):
                raise HarnessProtocolError("Work Item index requires item_ref and item_hash")
            count = index.get("attempt_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise HarnessProtocolError("Work Item attempt_count must be at least one")
        return result

    @staticmethod
    def _token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _resolve_ref(self, run_id: str, reference: str, owner: str) -> Path:
        run_root = self.runs.run_directory(run_id).resolve()
        target = (run_root / reference).resolve()
        owner_root = (run_root / owner).resolve()
        if target != owner_root and owner_root not in target.parents:
            raise HarnessProtocolError(f"artifact reference escapes {owner}")
        return target

    def active_lease(self, mission: dict[str, Any]) -> dict[str, Any] | None:
        mission = self._validate(mission)
        reference = mission.get("active_lease_ref")
        if reference is None:
            return None
        lease = self._validate_lease(
            read_json(self._resolve_ref(mission["run_id"], reference, "mission/leases")),
            mission,
        )
        # A crash may happen after admission indexing but before the one Lease
        # update. Reconcile only decisions for the exact Work Attempt.
        remaining = []
        changed = False
        for attempt in lease["items"]:
            index = mission["work_items"].get(attempt["item_id"], {})
            decided = (
                index.get("last_decision_attempt") == attempt["attempt_number"]
                and isinstance(index.get("last_admission_ref"), str)
            )
            if decided:
                changed = True
            else:
                remaining.append(attempt)
        if changed:
            lease["items"] = remaining
            self._write_lease(mission, lease)
        return lease

    def _validate_lease(
        self, raw: Any, mission: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("lease_version") != LEASE_VERSION:
            raise HarnessProtocolError(f"lease_version must be {LEASE_VERSION}")
        lease = copy.deepcopy(raw)
        required = {"lease_version", "lease_id", "run_id", "mission_revision", "items", "claim"}
        if set(lease) != required:
            raise HarnessProtocolError("protocol 3 Lease contains unsupported or missing fields")
        if mission is not None and lease.get("run_id") != mission.get("run_id"):
            raise HarnessProtocolError("Lease Run identity mismatch")
        revision = lease.get("mission_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise HarnessProtocolError("Lease mission_revision must be non-negative")
        if mission is not None and revision != mission.get("revision"):
            raise HarnessProtocolError("Lease revision does not match Search Mission cursor")
        if not isinstance(lease.get("lease_id"), str) or not lease["lease_id"]:
            raise HarnessProtocolError("Lease requires lease_id")
        items = lease.get("items")
        if not isinstance(items, list):
            raise HarnessProtocolError("Lease items must be an array")
        seen: set[tuple[str, int]] = set()
        for attempt in items:
            if not isinstance(attempt, dict):
                raise HarnessProtocolError("Lease Work Attempt must be an object")
            identifier = item_id(attempt.get("item_id"))
            number = attempt.get("attempt_number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise HarnessProtocolError("Lease attempt_number must be at least one")
            identity = (identifier, number)
            if identity in seen:
                raise HarnessProtocolError("Lease contains duplicate Work Attempts")
            seen.add(identity)
        claim = lease.get("claim")
        if claim is not None:
            if not isinstance(claim, dict) or set(claim) != {
                "claim_id",
                "worker_instance_id",
                "expires_at",
            }:
                raise HarnessProtocolError("Lease claim is invalid")
            for field in ("claim_id", "worker_instance_id", "expires_at"):
                if not isinstance(claim.get(field), str) or not claim[field]:
                    raise HarnessProtocolError(f"Lease claim {field} is required")
        return lease

    def _lease_path(self, mission: dict[str, Any], lease_id: str) -> Path:
        return self.mission_directory(mission["run_id"]) / "leases" / f"{lease_id}.json"

    def _write_lease(self, mission: dict[str, Any], lease: dict[str, Any]) -> None:
        lease = self._validate_lease(lease, mission)
        write_json_atomic(self._lease_path(mission, lease["lease_id"]), lease)

    def publish_lease(
        self, mission: dict[str, Any], raw_items: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mission = self._validate(mission)
        active = self.active_lease(mission)
        if active is not None:
            if active["items"]:
                return mission, active
            mission = self.close_lease(mission)
        items = [validate_work_item(item) for item in raw_items]
        if not items:
            raise HarnessProtocolError("Mission Lease requires at least one Work Item")
        if len({item["item_id"] for item in items}) != len(items):
            raise HarnessProtocolError("Mission Lease contains duplicate Work Item IDs")
        sequence = int(mission["next_lease_sequence"])
        mission["next_lease_sequence"] = sequence + 1
        lease_id = f"lease-{sequence:06d}"
        attempts = []
        for item in items:
            identifier = item["item_id"]
            target = self.mission_directory(mission["run_id"]) / "items" / f"{self._token(identifier)}.json"
            relative = target.relative_to(self.runs.run_directory(mission["run_id"])).as_posix()
            if target.exists():
                existing = read_json(target)
                if existing != item:
                    raise HarnessProtocolError(
                        f"Work Item {identifier} was replayed with different content"
                    )
            else:
                write_json_atomic(target, item)
            previous = mission["work_items"].get(identifier)
            if isinstance(previous, dict) and previous.get("last_decision") == "committed":
                continue
            attempt_number = int((previous or {}).get("attempt_count", 0)) + 1
            index = {
                "item_ref": relative,
                "item_hash": digest(item),
                "attempt_count": attempt_number,
            }
            if isinstance(previous, dict) and previous.get("last_error"):
                index["last_error"] = str(previous["last_error"])
            mission["work_items"][identifier] = index
            projected = project_item(item, full_item_ref=relative, full_item_hash=digest(item))
            projected["attempt_number"] = attempt_number
            if index.get("last_error"):
                projected["last_error"] = index["last_error"]
            attempts.append(projected)
        if not attempts:
            raise HarnessProtocolError("Mission Lease has no unfinished Work Items")
        lease = {
            "lease_version": LEASE_VERSION,
            "lease_id": lease_id,
            "run_id": mission["run_id"],
            "mission_revision": mission["revision"],
            "items": attempts,
            "claim": None,
        }
        self._write_lease(mission, lease)
        mission["active_lease_ref"] = self._lease_path(mission, lease_id).relative_to(
            self.runs.run_directory(mission["run_id"])
        ).as_posix()
        self.save(mission)
        return mission, lease

    def claim_lease(
        self,
        mission: dict[str, Any],
        worker_instance_id: str,
        *,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None or not lease["items"]:
            raise HarnessProtocolError("Search Mission has no active Lease to claim")
        now = datetime.now(timezone.utc)
        claim = lease.get("claim")
        expires = _parse_time(claim.get("expires_at")) if isinstance(claim, dict) else None
        expired = expires is not None and expires <= now
        if isinstance(claim, dict) and not expired:
            if claim.get("worker_instance_id") != worker_instance_id:
                raise RunBusyError(
                    "active Search Mission Lease is owned by "
                    f"{claim.get('worker_instance_id')} until {claim.get('expires_at')}"
                )
            # Re-entering the same live claim is safe and preserves its fence.
            return lease
        lease["claim"] = {
            "claim_id": str(uuid.uuid4()),
            "worker_instance_id": str(worker_instance_id),
            "expires_at": (
                now + timedelta(seconds=max(1.0, float(ttl_seconds)))
            ).isoformat(timespec="microseconds"),
        }
        self._write_lease(mission, lease)
        return lease

    def heartbeat_lease(
        self,
        mission: dict[str, Any],
        worker_instance_id: str,
        claim_id: str,
        *,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None or not isinstance(lease.get("claim"), dict):
            raise HarnessProtocolError("Search Mission Lease has no active claim")
        claim = lease["claim"]
        if (
            claim.get("worker_instance_id") != worker_instance_id
            or claim.get("claim_id") != claim_id
        ):
            raise HarnessProtocolError("Search Mission Lease heartbeat claim mismatch")
        now = datetime.now(timezone.utc)
        expires = _parse_time(claim.get("expires_at"))
        if expires is None or expires <= now:
            raise HarnessProtocolError("Search Mission Lease heartbeat cannot revive an expired claim")
        claim["expires_at"] = (
            now + timedelta(seconds=max(1.0, float(ttl_seconds)))
        ).isoformat(timespec="microseconds")
        self._write_lease(mission, lease)
        return lease

    def release_lease(
        self,
        mission: dict[str, Any],
        worker_instance_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None:
            raise HarnessProtocolError("Search Mission has no active Lease")
        claim = lease.get("claim")
        if not isinstance(claim, dict) or (
            claim.get("worker_instance_id") != worker_instance_id
            or claim.get("claim_id") != claim_id
        ):
            raise HarnessProtocolError("Search Mission Lease release claim mismatch")
        lease["claim"] = None
        self._write_lease(mission, lease)
        return lease

    def write_raw_receipt(
        self, mission: dict[str, Any], raw_receipt: dict[str, Any]
    ) -> tuple[dict[str, Any], str, bool]:
        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None:
            raise HarnessProtocolError("Receipt requires an active Search Mission Lease")
        receipt = validate_receipt(raw_receipt, lease_id=lease["lease_id"])
        matching = next(
            (
                value
                for value in lease["items"]
                if value.get("item_id") == receipt["item_id"]
                and value.get("attempt_number") == receipt["attempt_number"]
            ),
            None,
        )
        if matching is None:
            raise HarnessProtocolError("Receipt does not target a pending Work Attempt")
        target = self.mission_directory(mission["run_id"]) / "receipts" / "raw" / f"{self._token(receipt['receipt_id'])}.json"
        relative = target.relative_to(self.runs.run_directory(mission["run_id"])).as_posix()
        if target.exists():
            existing = read_json(target)
            if existing != receipt:
                raise HarnessProtocolError("Receipt identity was replayed with different content")
            return receipt, relative, False
        write_json_atomic(target, receipt)
        return receipt, relative, True

    def apply_admissions(
        self,
        mission: dict[str, Any],
        receipts: dict[str, dict[str, Any]],
        receipt_refs: dict[str, str],
        decisions: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
        """Persist decisions, then update the Lease once for the whole batch."""

        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None:
            raise HarnessProtocolError("admission requires an active Search Mission Lease")
        newly_committed: set[str] = set()
        processed: set[tuple[str, int]] = set()
        for identifier, receipt in receipts.items():
            decision = decisions.get(identifier)
            if not isinstance(decision, dict):
                raise HarnessProtocolError(
                    f"Product Kernel omitted Receipt decision for {identifier}"
                )
            product_status = decision.get("status")
            if product_status == "committed":
                normalized = "committed"
            elif product_status == "correction_required":
                normalized = "retry_required"
            elif product_status == "blocked":
                normalized = "blocked"
            else:
                raise HarnessProtocolError(
                    f"unsupported Product Kernel Receipt decision: {decision}"
                )
            admission = {
                "admission_version": "1.0",
                "receipt_id": receipt["receipt_id"],
                "receipt_ref": receipt_refs[identifier],
                "item_id": identifier,
                "attempt_number": receipt["attempt_number"],
                "decision": normalized,
            }
            if normalized != "committed":
                admission["error"] = str(
                    decision.get("error") or "Product Kernel did not admit the Receipt"
                )
            target = self.mission_directory(mission["run_id"]) / "receipts" / "admissions" / f"{self._token(receipt['receipt_id'])}.json"
            relative = target.relative_to(self.runs.run_directory(mission["run_id"])).as_posix()
            existed = target.exists()
            if existed:
                if read_json(target) != admission:
                    raise HarnessProtocolError("Receipt admission identity collision")
            else:
                write_json_atomic(target, admission)
            index = mission["work_items"].get(identifier)
            if not isinstance(index, dict):
                raise HarnessProtocolError(f"unknown Search Mission Work Item {identifier}")
            index["last_admission_ref"] = relative
            index["last_decision"] = normalized
            index["last_decision_attempt"] = receipt["attempt_number"]
            if normalized == "retry_required":
                index["last_error"] = admission["error"]
            else:
                index.pop("last_error", None)
            if normalized == "committed" and not existed:
                newly_committed.add(identifier)
            processed.add((identifier, receipt["attempt_number"]))
        # Persist the recovery index before removing attempts from the Lease.
        self.save(mission)
        lease["items"] = [
            attempt
            for attempt in lease["items"]
            if (attempt["item_id"], attempt["attempt_number"]) not in processed
        ]
        self._write_lease(mission, lease)
        return mission, lease, newly_committed

    def record_metrics(
        self,
        mission: dict[str, Any],
        metrics: dict[str, Any] | None,
        *,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """Write replay-safe optional metrics without mutating Mission state."""

        mission = self._validate(mission)
        if not metrics:
            return mission
        if not checkpoint_id:
            raise HarnessProtocolError("metrics sidecar requires checkpoint_id")
        target = self.mission_directory(mission["run_id"]) / "metrics" / "checkpoints" / f"{self._token(checkpoint_id)}.json"
        value = {
            "metrics_version": "1.0",
            "checkpoint_id": checkpoint_id,
            "metrics": copy.deepcopy(metrics),
        }
        if target.exists():
            if read_json(target) != value:
                raise HarnessProtocolError("Mission metrics checkpoint identity collision")
            return mission
        write_json_atomic(target, value)
        return mission

    def close_lease(self, mission: dict[str, Any]) -> dict[str, Any]:
        mission = self._validate(mission)
        lease = self.active_lease(mission)
        if lease is None:
            return mission
        if lease["items"]:
            raise HarnessProtocolError("cannot close Search Mission Lease with unfinished Work Attempts")
        lease["claim"] = None
        self._write_lease(mission, lease)
        mission["active_lease_ref"] = None
        mission["revision"] = int(mission["revision"]) + 1
        self.save(mission)
        return mission

    def read_item(self, mission: dict[str, Any], identifier: str) -> dict[str, Any]:
        mission = self._validate(mission)
        index = mission["work_items"].get(item_id(identifier))
        if not isinstance(index, dict):
            raise HarnessProtocolError(f"unknown Search Mission Work Item {identifier}")
        value = read_json(
            self._resolve_ref(mission["run_id"], index["item_ref"], "mission/items")
        )
        if digest(value) != index.get("item_hash"):
            raise HarnessProtocolError(
                f"Search Mission Work Item integrity mismatch: {identifier}"
            )
        return value

    def stage_effect(
        self,
        mission: dict[str, Any],
        effect: dict[str, Any],
        *,
        mission_revision: int | None = None,
    ) -> None:
        """Persist the exact Product effect until Outcome materialization commits."""

        mission = self._validate(mission)
        if not isinstance(effect, dict) or not isinstance(effect.get("kind"), str):
            raise HarnessProtocolError("pending Product effect must be an object")
        revision = mission["revision"] if mission_revision is None else mission_revision
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise HarnessProtocolError("pending Product effect revision is invalid")
        write_json_atomic(
            self.pending_effect_path(mission["run_id"]),
            {
                "pending_effect_version": "1.0",
                "run_id": mission["run_id"],
                "mission_revision": revision,
                "effect": copy.deepcopy(effect),
            },
        )

    def pending_effect(self, mission: dict[str, Any]) -> dict[str, Any] | None:
        mission = self._validate(mission)
        target = self.pending_effect_path(mission["run_id"])
        if not target.is_file():
            return None
        value = read_json(target)
        if value.get("pending_effect_version") != "1.0" or value.get(
            "run_id"
        ) != mission.get("run_id"):
            raise HarnessProtocolError("pending Product effect identity mismatch")
        if value.get("mission_revision") != mission.get("revision"):
            raise HarnessProtocolError("pending Product effect revision mismatch")
        effect = value.get("effect")
        if effect is None:
            return None
        if not isinstance(effect, dict) or not isinstance(effect.get("kind"), str):
            raise HarnessProtocolError("pending Product effect is invalid")
        return copy.deepcopy(effect)

    def clear_pending_effect(self, mission: dict[str, Any]) -> None:
        mission = self._validate(mission)
        write_json_atomic(
            self.pending_effect_path(mission["run_id"]),
            {
                "pending_effect_version": "1.0",
                "run_id": mission["run_id"],
                "mission_revision": mission["revision"],
                "effect": None,
            },
        )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
