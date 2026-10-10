from __future__ import annotations

"""Normalize AI-extracted page observations into live evidence bundles.

This program is deliberately browser-blind: it accepts JSON already extracted
by an AI browser operator, validates it against one issued task, and writes the
bundle consumed by ``live_provider_session.py ingest``.  It never opens,
controls, or scrapes a browser and never invents prices or service identities.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ADAPTER_VERSION = "0.7.1"
VERIFIED_STATUS = "verified_observed"
TERMINAL_STATUSES = {VERIFIED_STATUS, "no_result", "rejected"}
UNKNOWN_FLIGHT_NUMBER_CODES = {
    "not_disclosed_by_source",
    "not_visible_in_search_result",
    "provider_omitted",
}


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


DISCOVERY_PURPOSES = {
    "itinerary_and_fare_rule_discovery",
    "route_discovery",
    "calendar_anchor",
    "price_indication",
}
MODES = {
    "flight",
    "rail",
    "bus",
    "coach",
    "ferry",
    "local_transit",
    "walk",
    "rideshare",
    "car",
}
MODE_ALIASES = {
    "air": "flight",
    "airfare": "flight",
    "plane": "flight",
    "train": "rail",
    "local-transit": "local_transit",
    "public_transport": "local_transit",
    "ride_share": "rideshare",
}


class EvidenceAdapterError(ValueError):
    pass


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceAdapterError(f"Cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceAdapterError(f"{field} must be a non-empty string")
    return " ".join(value.split())


def _only_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvidenceAdapterError(
            f"{field} has unsupported fields: {', '.join(unknown)}"
        )


def _datetime(value: Any, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceAdapterError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise EvidenceAdapterError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceAdapterError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise EvidenceAdapterError(f"{field} must be finite and non-negative")
    return int(number) if number.is_integer() else number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceAdapterError(f"{field} must be a non-negative integer")
    return value


def _currency(value: Any, field: str) -> str:
    currency = _text(value, field).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise EvidenceAdapterError(f"{field} must be a three-letter currency code")
    return currency


def _url(value: Any, field: str) -> str:
    raw = _text(value, field)
    if any(character.isspace() for character in raw):
        raise EvidenceAdapterError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise EvidenceAdapterError(
            f"{field} must be an absolute HTTP(S) URL"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise EvidenceAdapterError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise EvidenceAdapterError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            # SPA providers can encode the complete issued search state in
            # the fragment. Preserve it as evidence instead of treating it as
            # a cosmetic page anchor.
            parsed.fragment,
        )
    )


def _mode(value: Any, field: str) -> str:
    mode = _text(value, field).lower().replace(" ", "_")
    mode = MODE_ALIASES.get(mode, mode)
    if mode not in MODES:
        raise EvidenceAdapterError(f"{field} is not a supported travel mode")
    return mode


def _string_list(value: Any, field: str, *, upper: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise EvidenceAdapterError(f"{field} must be a string array")
    result = [" ".join(item.split()) for item in value]
    if upper:
        result = [item.upper() for item in result]
    if len(set(result)) != len(result):
        raise EvidenceAdapterError(f"{field} must contain unique values")
    return result


def _stable_id(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _normalize_task(raw: Any) -> dict[str, Any]:
    task = copy.deepcopy(_object(raw, "task"))
    if _text(task.get("status"), "task.status").lower() != "pending":
        raise EvidenceAdapterError("task.status must be pending")
    contract = _text(
        task.get("evidence_contract_version"), "task.evidence_contract_version"
    )
    if contract != "0.7.1":
        raise EvidenceAdapterError("task.evidence_contract_version must be 0.7.1")
    earliest = _datetime(task.get("earliest_departure"), "task.earliest_departure")
    latest = _datetime(task.get("latest_arrival"), "task.latest_arrival")
    if latest <= earliest:
        raise EvidenceAdapterError("task time window must be increasing")
    policies = _object(task.get("endpoint_policy"), "task.endpoint_policy")
    aliases = {
        "exact": "exact_node",
        "exact_node": "exact_node",
        "allowed": "node_pool",
        "pool": "node_pool",
        "node_pool": "node_pool",
        "discovered": "provider_authorized_anywhere",
        "provider_authorized_anywhere": "provider_authorized_anywhere",
    }
    endpoint_policy: dict[str, str] = {}
    for side in ("from", "to"):
        policy = aliases.get(str(policies.get(side, "")).lower())
        if policy is None:
            raise EvidenceAdapterError(f"task.endpoint_policy.{side} is unsupported")
        endpoint_policy[side] = policy
    modes = _string_list(task.get("suggested_modes"), "task.suggested_modes")
    modes = [_mode(item, "task.suggested_modes") for item in modes]
    origins = [str(item).strip().upper() for item in task.get("origins", []) if item]
    destinations = [
        str(item).strip().upper() for item in task.get("destinations", []) if item
    ]
    normalized = {
        "task_id": _text(task.get("task_id"), "task.task_id"),
        "evidence_contract_version": contract,
        "endpoint_policy": endpoint_policy,
        "origins": list(dict.fromkeys(origins)),
        "destinations": list(dict.fromkeys(destinations)),
        "from_node": str(task["from_node"]).strip().upper()
        if task.get("from_node")
        else None,
        "to_node": str(task["to_node"]).strip().upper()
        if task.get("to_node")
        else None,
        "earliest_departure": earliest,
        "latest_arrival": latest,
        "suggested_modes": list(dict.fromkeys(modes)),
    }
    for side in ("from", "to"):
        if endpoint_policy[side] == "exact_node" and not normalized[f"{side}_node"]:
            raise EvidenceAdapterError(
                f"task.{side}_node is required by exact_node endpoint policy"
            )
    return normalized


def _normalize_source(raw: Any, field: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    source = copy.deepcopy(fallback or {})
    if raw is not None:
        override = _object(raw, field)
        _only_keys(override, {"provider", "url", "observed_at", "expires_at"}, field)
        source.update(override)
    provider = _text(source.get("provider"), f"{field}.provider")
    url = _url(source.get("url"), f"{field}.url")
    observed_at = _datetime(source.get("observed_at"), f"{field}.observed_at")
    normalized: dict[str, Any] = {
        "provider": provider,
        "url": url,
        "observed_at": _iso(observed_at),
    }
    if source.get("expires_at") is not None:
        expires_at = _datetime(source["expires_at"], f"{field}.expires_at")
        if expires_at <= observed_at:
            raise EvidenceAdapterError(f"{field}.expires_at must be after observed_at")
        normalized["expires_at"] = _iso(expires_at)
    return normalized


def _normalize_discovery_provenance(raw: Any, field: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise EvidenceAdapterError(f"{field} must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        item_field = f"{field}[{index}]"
        value = _object(item, item_field)
        _only_keys(
            value,
            {
                "provider",
                "url",
                "observed_at",
                "purpose",
                "admission_status",
                "displayed_price",
                "notes",
            },
            item_field,
        )
        purpose = _text(value.get("purpose"), f"{item_field}.purpose").lower()
        if purpose not in DISCOVERY_PURPOSES:
            raise EvidenceAdapterError(f"{item_field}.purpose is unsupported")
        admission_status = _text(
            value.get("admission_status"), f"{item_field}.admission_status"
        ).lower()
        if admission_status != "research_only":
            raise EvidenceAdapterError(
                f"{item_field}.admission_status must be research_only"
            )
        provenance: dict[str, Any] = {
            "provider": _text(value.get("provider"), f"{item_field}.provider"),
            "url": _url(value.get("url"), f"{item_field}.url"),
            "observed_at": _iso(
                _datetime(value.get("observed_at"), f"{item_field}.observed_at")
            ),
            "purpose": purpose,
            "admission_status": "research_only",
        }
        if value.get("displayed_price") is not None:
            displayed = _object(
                value["displayed_price"], f"{item_field}.displayed_price"
            )
            _only_keys(displayed, {"amount", "currency"}, f"{item_field}.displayed_price")
            provenance["displayed_price"] = {
                "amount": _number(
                    displayed.get("amount"), f"{item_field}.displayed_price.amount"
                ),
                "currency": _currency(
                    displayed.get("currency"), f"{item_field}.displayed_price.currency"
                ),
            }
        if value.get("notes") is not None:
            provenance["notes"] = _string_list(
                value["notes"], f"{item_field}.notes"
            )
        normalized.append(provenance)
    return normalized


def _normalize_exact_money(raw: Any, field: str) -> dict[str, Any]:
    money = _object(raw, field)
    _only_keys(money, {"status", "amount", "currency"}, field)
    status = _text(money.get("status"), f"{field}.status").lower()
    if status != "observed_exact":
        raise EvidenceAdapterError(
            f"{field}.status must be observed_exact; estimates are not admissible"
        )
    return {
        "amount": _number(money.get("amount"), f"{field}.amount"),
        "currency": _currency(money.get("currency"), f"{field}.currency"),
    }


def _normalize_baggage(raw: Any, field: str, mode: str) -> dict[str, Any]:
    baggage = _object(raw, field)
    _only_keys(
        baggage,
        {"status", "included_personal_item", "mandatory_cost"},
        field,
    )
    status = _text(baggage.get("status"), f"{field}.status").lower()
    if status not in {"confirmed", "not_applicable"}:
        raise EvidenceAdapterError(
            f"{field}.status must be confirmed or not_applicable"
        )
    if mode == "flight" and status != "confirmed":
        raise EvidenceAdapterError(f"{field}.status must be confirmed for flight mode")
    personal_item = baggage.get("included_personal_item")
    if not isinstance(personal_item, bool):
        raise EvidenceAdapterError(f"{field}.included_personal_item must be boolean")
    return {
        "status": status,
        "included_personal_item": personal_item,
        "mandatory_cost": _normalize_exact_money(
            baggage.get("mandatory_cost"), f"{field}.mandatory_cost"
        ),
    }


def _normalize_flight_identity(raw: Any, field: str) -> dict[str, Any]:
    identity = _object(raw, field)
    _only_keys(
        identity,
        {
            "marketing_carrier",
            "operating_carrier",
            "flight_numbers",
            "unknown_reason",
        },
        field,
    )
    normalized: dict[str, Any] = {}
    for key in ("marketing_carrier", "operating_carrier"):
        if identity.get(key) is not None:
            normalized[key] = _text(identity[key], f"{field}.{key}")
    if not normalized:
        raise EvidenceAdapterError(
            f"{field} must include marketing_carrier or operating_carrier"
        )
    numbers = identity.get("flight_numbers")
    unknown = identity.get("unknown_reason")
    if (numbers is None) == (unknown is None):
        raise EvidenceAdapterError(
            f"{field} must include exactly one of flight_numbers or unknown_reason"
        )
    if numbers is not None:
        normalized["flight_numbers"] = _string_list(
            numbers, f"{field}.flight_numbers", upper=True
        )
        if not normalized["flight_numbers"]:
            raise EvidenceAdapterError(f"{field}.flight_numbers must not be empty")
    else:
        reason = _object(unknown, f"{field}.unknown_reason")
        _only_keys(reason, {"code", "detail"}, f"{field}.unknown_reason")
        code = _text(reason.get("code"), f"{field}.unknown_reason.code")
        if code not in UNKNOWN_FLIGHT_NUMBER_CODES:
            raise EvidenceAdapterError(
                f"{field}.unknown_reason.code is unsupported"
            )
        normalized_reason = {"code": code}
        if reason.get("detail") is not None:
            normalized_reason["detail"] = _text(
                reason["detail"], f"{field}.unknown_reason.detail"
            )
        normalized["unknown_reason"] = normalized_reason
    return normalized


def _normalize_discovered_nodes(
    raw_nodes: Any, task: dict[str, Any], default_source: dict[str, Any]
) -> list[dict[str, Any]]:
    if raw_nodes is None:
        return []
    if not isinstance(raw_nodes, list):
        raise EvidenceAdapterError("discovered_nodes must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_types = {
        "airport",
        "railway_station",
        "bus_terminal",
        "ferry_terminal",
        "city_destination",
    }
    for index, value in enumerate(raw_nodes):
        field = f"discovered_nodes[{index}]"
        node = _object(value, field)
        _only_keys(
            node,
            {
                "node_id",
                "node_type",
                "location",
                "country_or_region",
                "country_code",
                "timezone",
                "coordinates",
                "notes",
                "source",
            },
            field,
        )
        node_id = _text(node.get("node_id"), f"{field}.node_id").upper()
        if node_id in seen:
            raise EvidenceAdapterError(f"duplicate discovered node: {node_id}")
        seen.add(node_id)
        node_type = _text(node.get("node_type"), f"{field}.node_type").lower()
        if node_type not in allowed_types:
            raise EvidenceAdapterError(f"{field}.node_type is unsupported")
        source = _normalize_source(node.get("source"), f"{field}.source", default_source)
        normalized: dict[str, Any] = {
            "node_id": node_id,
            "node_type": node_type,
            "location": _text(node.get("location"), f"{field}.location"),
            "country_or_region": _text(
                node.get("country_or_region"), f"{field}.country_or_region"
            ),
            "timezone": _text(node.get("timezone"), f"{field}.timezone"),
            "source_url": source["url"],
            "observed_at": source["observed_at"],
        }
        if node.get("country_code") is not None:
            country_code = _text(
                node["country_code"], f"{field}.country_code"
            ).upper()
            if len(country_code) != 2 or not country_code.isalpha():
                raise EvidenceAdapterError(
                    f"{field}.country_code must be a two-letter code"
                )
            normalized["country_code"] = country_code
        if node.get("coordinates") is not None:
            coordinates = _object(node["coordinates"], f"{field}.coordinates")
            _only_keys(
                coordinates, {"latitude", "longitude"}, f"{field}.coordinates"
            )
            latitude = _number_signed(
                coordinates.get("latitude"), f"{field}.coordinates.latitude", -90, 90
            )
            longitude = _number_signed(
                coordinates.get("longitude"), f"{field}.coordinates.longitude", -180, 180
            )
            normalized["coordinates"] = {
                "latitude": latitude,
                "longitude": longitude,
            }
        if node.get("notes") is not None:
            normalized["notes"] = _string_list(node["notes"], f"{field}.notes")
        result.append(normalized)
    return result


def _validate_discovered_node_usage(
    discovered_nodes: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Authorize discovered endpoints and sourced atomic-flight via nodes separately.

    Exact and pool endpoint policies remain immutable.  A flight provider may,
    however, disclose an airport that is internal to one verified atomic ticket;
    that airport is graph metadata, not a newly authorized task endpoint.
    """

    if not discovered_nodes:
        return
    declared = {node["node_id"]: node for node in discovered_nodes}
    endpoint_references: set[str] = set()
    internal_flight_references: set[str] = set()
    for observation in observations:
        if observation.get("evidence_status") != VERIFIED_STATUS:
            continue
        for side in ("from", "to"):
            if task["endpoint_policy"][side] == "provider_authorized_anywhere":
                endpoint_references.add(observation[f"{side}_node"])
        ticket = observation.get("ticket_context")
        if observation.get("mode") == "flight" and isinstance(ticket, dict):
            internal_flight_references.update(ticket.get("via_nodes", []))
            ticketed_final_node = ticket.get("ticketed_final_node")
            if ticketed_final_node:
                internal_flight_references.add(str(ticketed_final_node).upper())

    for node_id, node in declared.items():
        used_as_endpoint = node_id in endpoint_references
        used_as_internal_flight_node = node_id in internal_flight_references
        if not used_as_endpoint and not used_as_internal_flight_node:
            raise EvidenceAdapterError(
                f"discovered node {node_id} is not referenced by an authorized endpoint "
                "or verified atomic flight itinerary"
            )
        if used_as_internal_flight_node and node["node_type"] != "airport":
            raise EvidenceAdapterError(
                f"atomic flight via node {node_id} must have node_type airport"
            )


def _number_signed(value: Any, field: str, minimum: float, maximum: float) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceAdapterError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise EvidenceAdapterError(f"{field} must be between {minimum} and {maximum}")
    return int(number) if number.is_integer() else number


def _endpoint_allowed(
    node_id: str, side: str, task: dict[str, Any], declared_nodes: set[str]
) -> bool:
    policy = task["endpoint_policy"][side]
    exact = task[f"{side}_node"]
    pool = task["origins" if side == "from" else "destinations"]
    pool = {item for item in pool if item != "ANYWHERE"}
    if policy == "exact_node":
        return node_id == exact
    if policy == "node_pool":
        return node_id in pool
    return node_id in pool or node_id in declared_nodes


def _normalize_itinerary(
    raw: Any, field: str, observation_mode: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    itinerary = _object(raw, field)
    _only_keys(
        itinerary,
        {"atomic_offer_id", "segments", "planned_exit_node"},
        field,
    )
    segments = itinerary.get("segments")
    if not isinstance(segments, list) or len(segments) < 2:
        raise EvidenceAdapterError(f"{field}.segments must contain at least two segments")
    normalized_segments: list[dict[str, Any]] = []
    ids: set[str] = set()
    prior: dict[str, Any] | None = None
    for index, raw_segment in enumerate(segments):
        segment_field = f"{field}.segments[{index}]"
        segment = _object(raw_segment, segment_field)
        _only_keys(
            segment,
            {
                "component_id",
                "from_node",
                "to_node",
                "mode",
                "departure",
                "arrival",
            },
            segment_field,
        )
        start = _text(segment.get("from_node"), f"{segment_field}.from_node").upper()
        end = _text(segment.get("to_node"), f"{segment_field}.to_node").upper()
        mode = _mode(segment.get("mode"), f"{segment_field}.mode")
        if observation_mode is not None and mode != observation_mode:
            raise EvidenceAdapterError(
                f"{segment_field}.mode must match the atomic observation mode"
            )
        departure = _datetime(segment.get("departure"), f"{segment_field}.departure")
        arrival = _datetime(segment.get("arrival"), f"{segment_field}.arrival")
        if arrival <= departure:
            raise EvidenceAdapterError(f"{segment_field}.arrival must be after departure")
        if prior is not None:
            if prior["to_node"] != start:
                raise EvidenceAdapterError(f"{segment_field} breaks itinerary node continuity")
            if departure < prior["arrival_value"]:
                raise EvidenceAdapterError(f"{segment_field} overlaps the previous segment")
        component_id = (
            _text(segment["component_id"], f"{segment_field}.component_id")
            if segment.get("component_id") is not None
            else _stable_id(
                "component",
                [start, end, mode, _iso(departure), _iso(arrival)],
            )
        )
        if component_id in ids:
            raise EvidenceAdapterError(f"duplicate itinerary component_id: {component_id}")
        ids.add(component_id)
        normalized_segment: dict[str, Any] = {
            "component_id": component_id,
            "from_node": start,
            "to_node": end,
            "mode": mode,
            "departure": _iso(departure),
            "arrival": _iso(arrival),
            "arrival_value": arrival,
        }
        normalized_segments.append(normalized_segment)
        prior = normalized_segment
    modes = {segment["mode"] for segment in normalized_segments}
    if len(modes) != 1:
        raise EvidenceAdapterError("one atomic itinerary cannot mix travel modes")
    via_nodes = [segment["to_node"] for segment in normalized_segments[:-1]]
    if len(set(via_nodes)) != len(via_nodes):
        raise EvidenceAdapterError("atomic itinerary via nodes must be unique")
    public_segments = []
    for segment in normalized_segments:
        public_segment = {key: value for key, value in segment.items() if key != "arrival_value"}
        public_segments.append(public_segment)
    atomic_offer_id = (
        _text(itinerary["atomic_offer_id"], f"{field}.atomic_offer_id")
        if itinerary.get("atomic_offer_id") is not None
        else _stable_id("atomic-offer", public_segments)
    )
    ticket_context = {
        "pricing_scope": "atomic_offer",
        "atomic_offer_id": atomic_offer_id,
        "component_edge_ids": [segment["component_id"] for segment in normalized_segments],
        "via_nodes": via_nodes,
        "segments": public_segments,
    }
    used_segments = normalized_segments
    planned_exit_node = None
    if itinerary.get("planned_exit_node") is not None:
        planned_exit_node = _text(
            itinerary["planned_exit_node"], f"{field}.planned_exit_node"
        ).upper()
        if planned_exit_node not in via_nodes:
            raise EvidenceAdapterError(
                f"{field}.planned_exit_node must be a ticketed connection before the final destination"
            )
        exit_index = next(
            index
            for index, segment in enumerate(normalized_segments)
            if segment["to_node"] == planned_exit_node
        )
        used_segments = normalized_segments[: exit_index + 1]
        used_component_ids = [
            segment["component_id"] for segment in used_segments
        ]
        ticket_context.update(
            {
                "use_pattern": "planned_exit_at_connection",
                "ticketed_final_node": normalized_segments[-1]["to_node"],
                "planned_exit_node": planned_exit_node,
                "used_component_edge_ids": used_component_ids,
                "unused_component_edge_ids": [
                    segment["component_id"]
                    for segment in normalized_segments[exit_index + 1 :]
                ],
                "used_via_nodes": [
                    segment["to_node"] for segment in used_segments[:-1]
                ],
            }
        )
    derived = {
        "from_node": normalized_segments[0]["from_node"],
        "to_node": used_segments[-1]["to_node"],
        "mode": normalized_segments[0]["mode"],
        "departure": normalized_segments[0]["departure"],
        "arrival": used_segments[-1]["arrival"],
        "ticket_context": ticket_context,
        "segments": public_segments,
    }
    return ticket_context, derived


def _normalize_verified_observation(
    raw: dict[str, Any],
    index: int,
    task: dict[str, Any],
    default_source: dict[str, Any],
    declared_nodes: set[str],
) -> dict[str, Any]:
    field = f"observations[{index}]"
    _only_keys(
        raw,
        {
            "observation_id",
            "evidence_status",
            "source",
            "from_node",
            "to_node",
            "mode",
            "departure",
            "arrival",
            "price",
            "baggage",
            "flight_identity",
            "operator",
            "service_id",
            "provider_offer_id",
            "itinerary",
            "independent_ticket_group",
            "entitlement_tags",
            "border_crossing_count",
            "discovery_provenance",
            "notes",
        },
        field,
    )
    source = _normalize_source(raw.get("source"), f"{field}.source", default_source)
    raw_mode = _mode(raw.get("mode"), f"{field}.mode") if raw.get("mode") is not None else None
    itinerary_result: dict[str, Any] | None = None
    if raw.get("itinerary") is not None:
        _, itinerary_result = _normalize_itinerary(
            raw["itinerary"], f"{field}.itinerary", raw_mode
        )
    mode = raw_mode or (itinerary_result or {}).get("mode")
    if mode is None:
        raise EvidenceAdapterError(f"{field}.mode is required")
    if mode not in task["suggested_modes"]:
        raise EvidenceAdapterError(f"{field}.mode is outside task suggested_modes")
    start = (
        _text(raw["from_node"], f"{field}.from_node").upper()
        if raw.get("from_node") is not None
        else (itinerary_result or {}).get("from_node")
    )
    end = (
        _text(raw["to_node"], f"{field}.to_node").upper()
        if raw.get("to_node") is not None
        else (itinerary_result or {}).get("to_node")
    )
    if not start or not end:
        raise EvidenceAdapterError(f"{field} must identify from_node and to_node")
    if itinerary_result is not None and (
        start != itinerary_result["from_node"] or end != itinerary_result["to_node"]
    ):
        raise EvidenceAdapterError(f"{field} endpoints conflict with itinerary endpoints")
    if not _endpoint_allowed(start, "from", task, declared_nodes) or not _endpoint_allowed(
        end, "to", task, declared_nodes
    ):
        raise EvidenceAdapterError(f"{field} endpoints exceed the issued task scope")
    departure = (
        _datetime(raw["departure"], f"{field}.departure")
        if raw.get("departure") is not None
        else _datetime((itinerary_result or {}).get("departure"), f"{field}.departure")
    )
    arrival = (
        _datetime(raw["arrival"], f"{field}.arrival")
        if raw.get("arrival") is not None
        else _datetime((itinerary_result or {}).get("arrival"), f"{field}.arrival")
    )
    if itinerary_result is not None and (
        _iso(departure) != itinerary_result["departure"]
        or _iso(arrival) != itinerary_result["arrival"]
    ):
        raise EvidenceAdapterError(f"{field} schedule conflicts with itinerary schedule")
    if arrival <= departure:
        raise EvidenceAdapterError(f"{field}.arrival must be after departure")
    if departure < task["earliest_departure"] or arrival > task["latest_arrival"]:
        raise EvidenceAdapterError(f"{field} schedule falls outside the issued task window")
    observed_at = _datetime(source["observed_at"], f"{field}.source.observed_at")
    if observed_at > departure:
        raise EvidenceAdapterError(f"{field}.observed_at must not be after departure")
    price = _normalize_exact_money(raw.get("price"), f"{field}.price")
    baggage = _normalize_baggage(raw.get("baggage"), f"{field}.baggage", mode)
    normalized: dict[str, Any] = {
        "observation_id": _text(raw["observation_id"], f"{field}.observation_id")
        if raw.get("observation_id") is not None
        else _stable_id("observation", [source, start, end, mode, _iso(departure), _iso(arrival), price]),
        "evidence_status": VERIFIED_STATUS,
        "provider": source["provider"],
        "url": source["url"],
        "observed_at": source["observed_at"],
        "from_node": start,
        "to_node": end,
        "mode": mode,
        "departure": _iso(departure),
        "arrival": _iso(arrival),
        "price": price,
        "baggage": baggage,
        "border_crossing_count": _integer(
            raw.get("border_crossing_count", 0), f"{field}.border_crossing_count"
        ),
        "entitlement_tags": _string_list(
            raw.get("entitlement_tags", []), f"{field}.entitlement_tags"
        ),
    }
    if source.get("expires_at"):
        normalized["expires_at"] = source["expires_at"]
    discovery_provenance = _normalize_discovery_provenance(
        raw.get("discovery_provenance"), f"{field}.discovery_provenance"
    )
    if discovery_provenance:
        normalized["discovery_provenance"] = discovery_provenance
    notes = _string_list(raw.get("notes", []), f"{field}.notes")
    if mode == "flight":
        identity = _normalize_flight_identity(
            raw.get("flight_identity"), f"{field}.flight_identity"
        )
        normalized["flight_identity"] = identity
        normalized["operator"] = identity.get("marketing_carrier") or identity["operating_carrier"]
        if identity.get("flight_numbers"):
            normalized["service_id"] = " + ".join(identity["flight_numbers"])
        else:
            reason = identity["unknown_reason"]
            notes.append(
                f"flight_number_unknown_reason={reason['code']}"
                + (f": {reason['detail']}" if reason.get("detail") else "")
            )
    elif raw.get("flight_identity") is not None:
        raise EvidenceAdapterError(f"{field}.flight_identity is only valid for flight mode")
    else:
        if raw.get("operator") is not None:
            normalized["operator"] = _text(raw["operator"], f"{field}.operator")
        if raw.get("service_id") is not None:
            normalized["service_id"] = _text(raw["service_id"], f"{field}.service_id")
    if raw.get("provider_offer_id") is not None:
        normalized["provider_offer_id"] = _text(
            raw["provider_offer_id"], f"{field}.provider_offer_id"
        )
    if raw.get("independent_ticket_group") is not None:
        normalized["independent_ticket_group"] = _text(
            raw["independent_ticket_group"], f"{field}.independent_ticket_group"
        )
    if itinerary_result is not None:
        normalized["ticket_context"] = itinerary_result["ticket_context"]
        path = ">".join(
            [itinerary_result["segments"][0]["from_node"]]
            + [segment["to_node"] for segment in itinerary_result["segments"]]
        )
        notes.append(f"atomic_itinerary_path={path}")
        if itinerary_result["ticket_context"].get("use_pattern") == "planned_exit_at_connection":
            notes.append(
                "planned_exit_at_connection="
                + itinerary_result["ticket_context"]["planned_exit_node"]
                + ";ticketed_final_node="
                + itinerary_result["ticket_context"]["ticketed_final_node"]
            )
    if notes:
        normalized["notes"] = list(dict.fromkeys(notes))
    return normalized


def _normalize_terminal_observation(
    raw: dict[str, Any], index: int, default_source: dict[str, Any]
) -> dict[str, Any]:
    field = f"observations[{index}]"
    _only_keys(
        raw,
        {"observation_id", "evidence_status", "source", "reason", "notes"},
        field,
    )
    source = _normalize_source(raw.get("source"), f"{field}.source", default_source)
    status = _text(raw.get("evidence_status"), f"{field}.evidence_status").lower()
    normalized = {
        "observation_id": _text(raw["observation_id"], f"{field}.observation_id")
        if raw.get("observation_id") is not None
        else _stable_id("observation", [status, source, raw.get("reason")]),
        "evidence_status": status,
        "provider": source["provider"],
        "url": source["url"],
        "observed_at": source["observed_at"],
        "reason": _text(raw.get("reason"), f"{field}.reason"),
    }
    if raw.get("notes") is not None:
        normalized["notes"] = _string_list(raw["notes"], f"{field}.notes")
    return normalized


def adapt_payload(raw: Any) -> dict[str, Any]:
    payload = _object(raw, "input")
    _only_keys(
        payload,
        {
            "adapter_version",
            "bundle_id",
            "task",
            "source",
            "discovered_nodes",
            "observations",
        },
        "input",
    )
    if _text(payload.get("adapter_version"), "adapter_version") != ADAPTER_VERSION:
        raise EvidenceAdapterError(f"adapter_version must be {ADAPTER_VERSION}")
    task = _normalize_task(payload.get("task"))
    default_source = _normalize_source(payload.get("source"), "source")
    discovered_nodes = _normalize_discovered_nodes(
        payload.get("discovered_nodes"), task, default_source
    )
    declared_nodes = {node["node_id"] for node in discovered_nodes}
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise EvidenceAdapterError("observations must be a non-empty array")
    normalized_observations: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_observation in enumerate(observations):
        observation = _object(raw_observation, f"observations[{index}]")
        status = _text(
            observation.get("evidence_status"),
            f"observations[{index}].evidence_status",
        ).lower()
        if status not in TERMINAL_STATUSES:
            raise EvidenceAdapterError(
                f"observations[{index}].evidence_status must be verified_observed, no_result, or rejected"
            )
        if status == VERIFIED_STATUS:
            normalized = _normalize_verified_observation(
                observation, index, task, default_source, declared_nodes
            )
        else:
            normalized = _normalize_terminal_observation(
                observation, index, default_source
            )
        if normalized["observation_id"] in ids:
            raise EvidenceAdapterError(
                f"duplicate observation_id: {normalized['observation_id']}"
            )
        ids.add(normalized["observation_id"])
        normalized_observations.append(normalized)
    _validate_discovered_node_usage(
        discovered_nodes, normalized_observations, task
    )
    identity = {
        "task_id": task["task_id"],
        "source": default_source,
        "discovered_nodes": discovered_nodes,
        "observations": normalized_observations,
    }
    return {
        "bundle_version": task["evidence_contract_version"],
        "bundle_id": _text(payload["bundle_id"], "bundle_id")
        if payload.get("bundle_id") is not None
        else _stable_id("ai-browser-bundle", identity),
        "request_id": task["task_id"],
        "discovered_nodes": discovered_nodes,
        "observations": normalized_observations,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate AI-extracted browser observations and emit a live evidence bundle; "
            "this command never opens or controls a browser"
        )
    )
    parser.add_argument("--input", required=True, help="AI observation input JSON")
    parser.add_argument("--output", required=True, help="live evidence bundle JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = adapt_payload(read_json(args.input))
        write_json_atomic(args.output, bundle)
        print(
            json.dumps(
                {
                    "valid": True,
                    "adapter_version": ADAPTER_VERSION,
                    "request_id": bundle["request_id"],
                    "bundle_id": bundle["bundle_id"],
                    "observations": len(bundle["observations"]),
                    "output": _display_path(args.output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except EvidenceAdapterError as exc:
        print(f"ai_evidence_adapter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
