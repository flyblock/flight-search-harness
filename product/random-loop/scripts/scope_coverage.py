#!/usr/bin/env python3
"""Persisted query-scope partitioning and coverage accounting for random loops."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any


COVERAGE_VERSION = "1.0"
SCOPE_FIELDS = ("origins", "destinations", "earliest_departure", "latest_departure")
PARTITION_DISPOSITIONS = {"retry_current", "advance_source"}


class ScopeCoverageError(ValueError):
    """Raised when a reported scope cannot be reconciled with its request."""


def _datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ScopeCoverageError(f"{field} must be an offset-aware date-time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScopeCoverageError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScopeCoverageError(f"{field} must include a UTC offset")
    return parsed


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _codes(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ScopeCoverageError(f"{field} must be a non-empty array")
    result: list[str] = []
    for raw in value:
        code = str(raw).strip().upper()
        if not code:
            raise ScopeCoverageError(f"{field} cannot contain an empty value")
        if code not in result:
            result.append(code)
    return result


def request_scope(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "origins": _codes(request.get("origins"), "request.origins"),
        "destinations": _codes(request.get("destinations"), "request.destinations"),
        "earliest_departure": _datetime(
            request.get("earliest_departure"), "request.earliest_departure"
        ).isoformat(),
        "latest_departure": _datetime(
            request.get("latest_departure"), "request.latest_departure"
        ).isoformat(),
    }


def normalize_scope(
    raw: Any, requested: dict[str, Any], *, field: str = "submitted_scope"
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScopeCoverageError(f"{field} must be an object")
    scope = {
        "origins": _codes(raw.get("origins"), f"{field}.origins"),
        "destinations": _codes(raw.get("destinations"), f"{field}.destinations"),
        "earliest_departure": _datetime(
            raw.get("earliest_departure"), f"{field}.earliest_departure"
        ).isoformat(),
        "latest_departure": _datetime(
            raw.get("latest_departure"), f"{field}.latest_departure"
        ).isoformat(),
    }
    if not set(scope["origins"]) <= set(requested["origins"]):
        raise ScopeCoverageError(f"{field}.origins exceeds the requested scope")
    if not set(scope["destinations"]) <= set(requested["destinations"]):
        raise ScopeCoverageError(f"{field}.destinations exceeds the requested scope")
    earliest = _utc(_datetime(scope["earliest_departure"], f"{field}.earliest_departure"))
    latest = _utc(_datetime(scope["latest_departure"], f"{field}.latest_departure"))
    request_earliest = _utc(
        _datetime(requested["earliest_departure"], "request.earliest_departure")
    )
    request_latest = _utc(
        _datetime(requested["latest_departure"], "request.latest_departure")
    )
    if latest < earliest:
        raise ScopeCoverageError(f"{field} has an inverted time window")
    if earliest < request_earliest or latest > request_latest:
        raise ScopeCoverageError(f"{field} time window exceeds the requested scope")
    return scope


def scope_is_exact(scope: dict[str, Any], requested: dict[str, Any]) -> bool:
    return (
        set(scope["origins"]) == set(requested["origins"])
        and set(scope["destinations"]) == set(requested["destinations"])
        and _utc(_datetime(scope["earliest_departure"], "scope.earliest_departure"))
        == _utc(_datetime(requested["earliest_departure"], "request.earliest_departure"))
        and _utc(_datetime(scope["latest_departure"], "scope.latest_departure"))
        == _utc(_datetime(requested["latest_departure"], "request.latest_departure"))
    )


def _validate_exact_partition(
    requested: dict[str, Any], covered_scopes: list[dict[str, Any]]
) -> None:
    if not covered_scopes:
        raise ScopeCoverageError("incomplete coverage requires at least one scope partition")
    request_start = _utc(
        _datetime(requested["earliest_departure"], "request.earliest_departure")
    )
    request_end = _utc(
        _datetime(requested["latest_departure"], "request.latest_departure")
    )
    for origin in requested["origins"]:
        for destination in requested["destinations"]:
            intervals = sorted(
                (
                    _utc(_datetime(scope["earliest_departure"], "partition.earliest_departure")),
                    _utc(_datetime(scope["latest_departure"], "partition.latest_departure")),
                )
                for scope in covered_scopes
                if origin in scope["origins"] and destination in scope["destinations"]
            )
            if not intervals:
                raise ScopeCoverageError(
                    f"scope partitions omit {origin}->{destination}"
                )
            cursor = request_start
            for start, end in intervals:
                if start > cursor:
                    raise ScopeCoverageError(
                        f"scope partitions leave a time gap for {origin}->{destination}"
                    )
                if start < cursor and cursor != request_start:
                    raise ScopeCoverageError(
                        f"scope partitions overlap for {origin}->{destination}"
                    )
                if end > cursor:
                    cursor = end
            if cursor != request_end:
                raise ScopeCoverageError(
                    f"scope partitions do not cover the full time window for {origin}->{destination}"
                )


def normalize_observation(
    request: dict[str, Any], group: dict[str, Any]
) -> dict[str, Any]:
    requested = request_scope(request)
    kind = str(group.get("observation_kind") or "").strip()
    default_complete = kind in {"payable", "no_market_result"}
    complete = group.get("coverage_complete", default_complete)
    if not isinstance(complete, bool):
        raise ScopeCoverageError("coverage_complete must be boolean")
    if kind in {"source_unavailable", "scope_unsupported", "scope_incomplete", "research_only"} and complete:
        raise ScopeCoverageError(f"{kind} cannot claim complete scope coverage")
    submitted = normalize_scope(
        group.get("submitted_scope", requested), requested, field="submitted_scope"
    )
    raw_partitions = group.get("scope_partitions", [])
    if not isinstance(raw_partitions, list):
        raise ScopeCoverageError("scope_partitions must be an array")
    partitions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_partitions):
        if not isinstance(raw, dict):
            raise ScopeCoverageError(f"scope_partitions[{index}] must be an object")
        disposition = str(raw.get("source_disposition") or "").strip()
        if disposition not in PARTITION_DISPOSITIONS:
            raise ScopeCoverageError(
                f"scope_partitions[{index}].source_disposition must be retry_current or advance_source"
            )
        scope_raw = raw.get("scope")
        scope = normalize_scope(
            scope_raw, requested, field=f"scope_partitions[{index}].scope"
        )
        partitions.append(
            {
                "scope": scope,
                "source_disposition": disposition,
                "reason": str(raw.get("reason") or "").strip() or None,
            }
        )
    if complete:
        if not scope_is_exact(submitted, requested):
            raise ScopeCoverageError(
                "coverage_complete=true requires submitted_scope to exactly match the request"
            )
        if partitions:
            raise ScopeCoverageError(
                "coverage_complete=true cannot include remaining scope_partitions"
            )
    elif partitions:
        covered = [item["scope"] for item in partitions]
        if kind in {"payable", "no_market_result"}:
            covered.insert(0, submitted)
        _validate_exact_partition(requested, covered)
    elif kind in {"payable", "no_market_result", "scope_incomplete", "research_only"}:
        raise ScopeCoverageError(
            f"{kind} with incomplete coverage requires exact scope_partitions"
        )
    acquisition = group.get("acquisition")
    if acquisition is None:
        acquisition = {"method": "legacy_unspecified"}
    if not isinstance(acquisition, dict):
        raise ScopeCoverageError("acquisition must be an object")
    method = str(acquisition.get("method") or "").strip()
    if method not in {
        "browser",
        "legacy_unspecified",
    }:
        raise ScopeCoverageError("acquisition.method is unsupported")
    normalized_acquisition = copy.deepcopy(acquisition)
    normalized_acquisition["method"] = method
    return {
        "requested_scope": requested,
        "submitted_scope": submitted,
        "coverage_complete": complete,
        "scope_partitions": partitions,
        "acquisition": normalized_acquisition,
    }


def validate_offer_in_submitted_scope(
    offer: dict[str, Any], report: dict[str, Any]
) -> None:
    scope = report["submitted_scope"]
    origin = str(offer.get("origin") or "").strip().upper()
    destination = str(offer.get("destination") or "").strip().upper()
    if origin not in scope["origins"]:
        raise ScopeCoverageError(f"offer origin {origin or '<missing>'} is outside submitted_scope")
    if "ANYWHERE" not in scope["destinations"] and destination not in scope["destinations"]:
        raise ScopeCoverageError(
            f"offer destination {destination or '<missing>'} is outside submitted_scope"
        )
    departure = _utc(_datetime(offer.get("departure"), "offer.departure"))
    earliest = _utc(
        _datetime(scope["earliest_departure"], "submitted_scope.earliest_departure")
    )
    latest = _utc(
        _datetime(scope["latest_departure"], "submitted_scope.latest_departure")
    )
    if departure < earliest or departure > latest:
        raise ScopeCoverageError("offer departure is outside submitted_scope")


def new_ledger() -> dict[str, Any]:
    return {
        "coverage_version": COVERAGE_VERSION,
        "next_partition_sequence": 1,
        "requests": {},
    }


def normalize_ledger(raw: Any) -> dict[str, Any]:
    if raw is None:
        return new_ledger()
    if not isinstance(raw, dict) or raw.get("coverage_version") != COVERAGE_VERSION:
        raise ScopeCoverageError("scope_coverage has an unsupported version")
    if not isinstance(raw.get("requests"), dict):
        raise ScopeCoverageError("scope_coverage.requests must be an object")
    ledger = copy.deepcopy(raw)
    sequence = ledger.get("next_partition_sequence", 1)
    if not isinstance(sequence, int) or sequence < 1:
        raise ScopeCoverageError("scope_coverage.next_partition_sequence must be positive")
    return ledger


def _refresh_request_status(record: dict[str, Any]) -> str:
    leaves = [
        partition
        for partition in record["partitions"].values()
        if partition.get("status") != "partitioned"
    ]
    if leaves and all(partition.get("status") == "complete" for partition in leaves):
        status = "complete"
    elif any(partition.get("status") == "pending" for partition in leaves):
        status = "pending"
    else:
        status = "unresolved"
    record["status"] = status
    return status


def register_request(ledger: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    logical_id = str(request["request_id"])
    records = ledger["requests"]
    existing = records.get(logical_id)
    if existing is not None:
        if existing.get("base_scope") != request_scope(request):
            raise ScopeCoverageError(
                f"logical request {logical_id} was rebuilt with a different scope"
            )
        return existing
    scope = request_scope(request)
    record = {
        "logical_request_id": logical_id,
        "parent_id": str(request["parent_id"]),
        "request_kind": str(request.get("request_kind") or ""),
        "base_scope": scope,
        "status": "pending",
        "partitions": {
            logical_id: {
                "partition_id": logical_id,
                "scope": copy.deepcopy(scope),
                "starting_source_index": 0,
                "source_attempt_count": 0,
                "priority": 0,
                "status": "pending",
                "resolution": None,
            }
        },
    }
    records[logical_id] = record
    return record


def materialize_requests(
    ledger: dict[str, Any], request: dict[str, Any]
) -> list[dict[str, Any]]:
    record = register_request(ledger, request)
    result: list[dict[str, Any]] = []
    pending = sorted(
        (
            item
            for item in record["partitions"].values()
            if item.get("status") == "pending"
        ),
        key=lambda item: (int(item.get("priority", 0)), str(item["partition_id"])),
    )
    for index, partition in enumerate(pending, 1):
        materialized = copy.deepcopy(request)
        scope = partition["scope"]
        materialized.update(copy.deepcopy(scope))
        materialized["logical_request_id"] = record["logical_request_id"]
        materialized["coverage_partition_id"] = partition["partition_id"]
        materialized["coverage_partition_sequence"] = index
        materialized["starting_source_index"] = int(
            partition.get("starting_source_index", 0)
        )
        materialized["request_id"] = partition["partition_id"]
        result.append(materialized)
    return result


def record_outcome(
    ledger: dict[str, Any],
    request: dict[str, Any],
    report: dict[str, Any],
    *,
    outcome: str,
    reason: str,
    source_index: int,
    source_count: int,
    next_source_index: int | None = None,
) -> dict[str, Any]:
    logical_id = str(request.get("logical_request_id") or request["request_id"])
    record = ledger["requests"].get(logical_id)
    if not isinstance(record, dict):
        raise ScopeCoverageError(f"unknown logical coverage request: {logical_id}")
    partition_id = str(request.get("coverage_partition_id") or request["request_id"])
    partition = record["partitions"].get(partition_id)
    if not isinstance(partition, dict):
        raise ScopeCoverageError(f"unknown coverage partition: {partition_id}")
    partition["source_attempt_count"] = int(
        partition.get("source_attempt_count", 0)
    ) + 1
    new_partition_ids: list[str] = []
    if report["scope_partitions"]:
        partition["status"] = "partitioned"
        partition["resolution"] = {
            "outcome": "partitioned",
            "reason": reason,
            "submitted_scope": copy.deepcopy(report["submitted_scope"]),
            "coverage_complete": False,
            "acquisition": copy.deepcopy(report["acquisition"]),
        }
        for priority, definition in enumerate(
            sorted(
                report["scope_partitions"],
                key=lambda item: 0
                if item["source_disposition"] == "retry_current"
                else 1,
            )
        ):
            sequence = int(ledger["next_partition_sequence"])
            ledger["next_partition_sequence"] = sequence + 1
            child_id = f"{logical_id}::scope-{sequence:04d}"
            starting_source = (
                source_index
                if definition["source_disposition"] == "retry_current"
                else source_index + 1
            )
            child_status = "pending" if starting_source < source_count else "unresolved"
            record["partitions"][child_id] = {
                "partition_id": child_id,
                "parent_partition_id": partition_id,
                "scope": copy.deepcopy(definition["scope"]),
                "starting_source_index": starting_source,
                "source_attempt_count": 0,
                "priority": priority,
                "status": child_status,
                "resolution": None
                if child_status == "pending"
                else {
                    "outcome": "unresolved",
                    "reason": "partition_has_no_remaining_provider",
                },
                "source_disposition": definition["source_disposition"],
            }
            new_partition_ids.append(child_id)
    elif outcome in {"satisfied", "exhausted"} and report["coverage_complete"]:
        partition["status"] = "complete"
        partition["resolution"] = {
            "outcome": outcome,
            "reason": reason,
            "submitted_scope": copy.deepcopy(report["submitted_scope"]),
            "coverage_complete": True,
            "acquisition": copy.deepcopy(report["acquisition"]),
        }
    elif outcome == "unresolved":
        partition["status"] = "unresolved"
        partition["resolution"] = {
            "outcome": outcome,
            "reason": reason,
            "submitted_scope": copy.deepcopy(report["submitted_scope"]),
            "coverage_complete": False,
            "acquisition": copy.deepcopy(report["acquisition"]),
        }
    else:
        if outcome == "fallback":
            fallback_index = (
                source_index + 1
                if next_source_index is None
                else next_source_index
            )
            if fallback_index <= source_index or fallback_index >= source_count:
                raise ScopeCoverageError("fallback has no valid next Provider")
            partition["starting_source_index"] = fallback_index
        partition["status"] = "pending"
        partition["last_observation"] = {
            "outcome": outcome,
            "reason": reason,
            "submitted_scope": copy.deepcopy(report["submitted_scope"]),
            "coverage_complete": bool(report["coverage_complete"]),
            "acquisition": copy.deepcopy(report["acquisition"]),
        }
    return {
        "logical_request_id": logical_id,
        "partition_id": partition_id,
        "new_partition_ids": new_partition_ids,
        "logical_status": _refresh_request_status(record),
    }


def audit(ledger: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_ledger(ledger)
    records = list(normalized["requests"].values())
    pending: list[str] = []
    unresolved: list[str] = []
    complete: list[str] = []
    for record in records:
        status = _refresh_request_status(record)
        logical_id = str(record["logical_request_id"])
        if status == "complete":
            complete.append(logical_id)
        elif status == "unresolved":
            unresolved.append(logical_id)
        else:
            pending.append(logical_id)
    return {
        "coverage_version": COVERAGE_VERSION,
        "complete": bool(records) and not pending and not unresolved,
        "logical_request_count": len(records),
        "complete_request_ids": complete,
        "pending_request_ids": pending,
        "unresolved_request_ids": unresolved,
    }
