"""Compact Search Mission Interfaces and protocol 3 validators.

The Run owns lifecycle state. This module only describes the execution cursor
exchanged by the Harness, a Worker, and one Product Kernel.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from .contracts import HarnessProtocolError


MISSION_VERSION = "3.0"
LEASE_VERSION = "3.0"
RECEIPT_VERSION = "2.0"
WORK_ITEM_VERSION = "3.0"
WORK_ITEM_KINDS = {"semantic_judgment", "browser_research", "choice"}
RECEIPT_STATUSES = {"complete", "blocked"}
RECEIPT_EXECUTION_ORIGINS = {
    "provider_query",
    "evidence_replay",
    "non_provider",
}
ITEM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,191}$")


class ProductKernel(Protocol):
    """One deep product Interface over initialization and transitions."""

    product_id: str
    journey_shape: str

    def start(
        self, request: dict[str, Any], product_directory: Path
    ) -> dict[str, Any]:
        """Initialize product state and return the first FlowEffect."""

    def advance(
        self,
        product_directory: Path,
        inbound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Admit Receipts or inspect current state for the next effect."""

    def reopen_expired_result(self, product_directory: Path) -> bool:
        """Reopen a completed Run when its Product supports price refresh."""


class SearchWorker(Protocol):
    """External seam for browser-capable Worker implementations."""

    worker_instance_id: str

    def run(
        self,
        lease: dict[str, Any],
        checkpoint: Callable[[dict[str, Any]], dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute leased Work Attempts and checkpoint their Receipts."""


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def item_id(value: Any) -> str:
    raw = text(value, "item_id")
    if ITEM_ID_PATTERN.fullmatch(raw) is None:
        raise HarnessProtocolError(f"invalid item_id: {raw!r}")
    return raw


def need_work(items: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise HarnessProtocolError("NeedWork requires at least one item")
    return {
        "effect_version": MISSION_VERSION,
        "kind": "need_work",
        "reason": text(reason, "reason"),
        "items": [validate_work_item(item) for item in items],
    }


def blocked(reason: str, action_required: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "effect_version": MISSION_VERSION,
        "kind": "blocked",
        "reason": text(reason, "reason"),
        "action_required": copy.deepcopy(action_required),
    }


def draft(reason: str, action_required: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "effect_version": MISSION_VERSION,
        "kind": "draft",
        "reason": text(reason, "reason"),
        "action_required": copy.deepcopy(action_required),
    }


def complete(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise HarnessProtocolError("Complete requires an object result")
    return {
        "effect_version": MISSION_VERSION,
        "kind": "complete",
        "result": copy.deepcopy(result),
    }


def validate_work_item(raw: Any) -> dict[str, Any]:
    """Normalize a Product Kernel Work Item to the protocol 3 contract."""

    if not isinstance(raw, dict):
        raise HarnessProtocolError("work item must be an object")
    dependencies = raw.get("depends_on", [])
    if dependencies not in (None, []):
        raise HarnessProtocolError("protocol 3 Work Items do not support depends_on")
    kind = raw.get("kind")
    if kind not in WORK_ITEM_KINDS:
        raise HarnessProtocolError(f"unsupported work item kind: {kind}")
    if not isinstance(raw.get("payload"), dict):
        raise HarnessProtocolError("work item payload must be an object")
    return {
        "item_version": WORK_ITEM_VERSION,
        "item_id": item_id(raw.get("item_id")),
        "kind": kind,
        "payload": copy.deepcopy(raw["payload"]),
    }


def receipt_id(lease_id: str, identifier: str, attempt_number: int) -> str:
    """Return the Harness-owned deterministic identity for one Receipt."""

    basis = f"{lease_id}\0{identifier}\0{attempt_number}".encode("utf-8")
    return f"receipt-{hashlib.sha256(basis).hexdigest()[:32]}"


def validate_receipt(raw: Any, *, lease_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HarnessProtocolError("receipt must be an object")
    version = raw.get("receipt_version", RECEIPT_VERSION)
    if version != RECEIPT_VERSION:
        raise HarnessProtocolError(
            f"receipt_version must be {RECEIPT_VERSION}; legacy Receipts cannot be resumed"
        )
    identifier = item_id(raw.get("item_id"))
    attempt_number = raw.get("attempt_number")
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise HarnessProtocolError("receipt attempt_number must be at least one")
    status = raw.get("status")
    if status not in RECEIPT_STATUSES:
        raise HarnessProtocolError(f"unsupported receipt status: {status}")
    payload = raw.get("payload")
    if status == "complete" and not isinstance(payload, dict):
        raise HarnessProtocolError("complete receipt payload must be an object")
    if payload is not None and not isinstance(payload, dict):
        raise HarnessProtocolError("receipt payload must be an object or null")
    result = {
        "receipt_version": RECEIPT_VERSION,
        "item_id": identifier,
        "attempt_number": attempt_number,
        "status": status,
        "payload": copy.deepcopy(payload),
    }
    execution_origin = raw.get("execution_origin")
    if execution_origin is not None:
        if execution_origin not in RECEIPT_EXECUTION_ORIGINS:
            raise HarnessProtocolError(
                f"unsupported receipt execution_origin: {execution_origin}"
            )
        result["execution_origin"] = execution_origin
    if status == "blocked":
        result["reason"] = text(raw.get("reason"), "reason")
    if lease_id is not None:
        derived = receipt_id(lease_id, identifier, attempt_number)
        supplied = raw.get("receipt_id")
        if supplied is not None and supplied != derived:
            raise HarnessProtocolError("receipt_id does not match its Work Attempt")
        result["receipt_id"] = derived
    return result


def validate_receipt_batch(
    raw: Any, *, default_worker_instance_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HarnessProtocolError("receipt batch must be an object")
    lease_identifier = text(raw.get("lease_id"), "lease_id")
    revision = raw.get("mission_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HarnessProtocolError("receipt batch mission_revision must be non-negative")
    claim_id = text(raw.get("claim_id"), "claim_id")
    worker_instance_id = raw.get("worker_instance_id", default_worker_instance_id)
    worker_instance_id = text(worker_instance_id, "worker_instance_id")
    raw_receipts = raw.get("receipts")
    if not isinstance(raw_receipts, list) or not raw_receipts:
        raise HarnessProtocolError("Mission checkpoint requires receipts")
    receipts = [
        validate_receipt(value, lease_id=lease_identifier)
        for value in raw_receipts
    ]
    identities = [
        (receipt["item_id"], receipt["attempt_number"])
        for receipt in receipts
    ]
    if len(set(identities)) != len(identities):
        raise HarnessProtocolError("Receipt batch contains a duplicate Work Attempt")
    metrics = raw.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise HarnessProtocolError("Mission metrics must be an object")
    return {
        "lease_id": lease_identifier,
        "mission_revision": revision,
        "claim_id": claim_id,
        "worker_instance_id": worker_instance_id,
        "receipts": receipts,
        "metrics": copy.deepcopy(metrics),
    }
