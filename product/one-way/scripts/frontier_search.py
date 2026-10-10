from __future__ import annotations

"""Deterministic bidirectional frontier search for sparse multimodal offers.

The module deliberately separates the mechanical search core from live providers.
Observed ``TravelEdge`` objects are searched locally.  Missing connections between
promising forward/backward labels become bounded, auditable provider requests.
Only the Python standard library is used.
"""

import argparse
import hashlib
import heapq
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ENGINE_VERSION = "0.7.1"
SURFACE_MODES = {"rail", "bus", "coach", "ferry", "local_transit", "walk", "rideshare", "car"}


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()

# These presets intentionally cover only parameters consumed by this module.
# They are not a claim to implement every field of a wider search-policy schema.
SEARCH_INTENSITY_PRESETS: dict[str, dict[str, int]] = {
    "standard": {
        "max_hops": 4,
        "max_labels_per_node": 8,
        "query_budget": 8,
        "geography_quota": 2,
        "mode_quota": 3,
        "incumbent_scan_limit": 10000,
        "incumbent_max_hops": 4,
    },
    "deep_savings": {
        "max_hops": 6,
        "max_labels_per_node": 20,
        "query_budget": 24,
        "geography_quota": 5,
        "mode_quota": 8,
        "incumbent_scan_limit": 25000,
        "incumbent_max_hops": 4,
    },
    "extreme_exploration": {
        "max_hops": 9,
        "max_labels_per_node": 40,
        "query_budget": 64,
        "geography_quota": 12,
        "mode_quota": 16,
        "incumbent_scan_limit": 60000,
        "incumbent_max_hops": 4,
    },
}

DEFAULTS: dict[str, Any] = {
    "max_hops": 6,
    "max_labels_per_node": 16,
    "max_total_cost": math.inf,
    "max_total_minutes": 7 * 24 * 60,
    "minimum_connection_minutes": 0,
    "query_budget": 12,
    "max_unresolved_gap_km": 3000.0,
    "max_surface_gap_km": 1200.0,
    "max_connector_speed_kmh": 900.0,
    "min_connector_cost_per_km": 0.0,
    "query_cost_relaxation_factor": 1.25,
    "geography_quota": 1000000,
    "mode_quota": 1000000,
    "max_trace_entries": 5000,
    "incumbent_scan_limit": 20000,
    "incumbent_max_hops": 6,
    "allowed_connector_modes": ["flight", "rail", "bus", "coach", "ferry", "local_transit"],
    "connector_mode_max_gap_km": {
        "flight": 3000.0,
        "rail": 1200.0,
        "bus": 900.0,
        "coach": 900.0,
        "ferry": 600.0,
        "local_transit": 250.0,
    },
    "connector_mode_max_speed_kmh": {
        "flight": 900.0,
        "rail": 350.0,
        "bus": 120.0,
        "coach": 120.0,
        "ferry": 80.0,
        "local_transit": 100.0,
    },
    "search_as_of": None,
}


class FrontierSearchError(ValueError):
    pass


def _dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FrontierSearchError(f"{field} must be an ISO date-time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise FrontierSearchError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise FrontierSearchError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any, field: str, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FrontierSearchError(f"{field} must be numeric") from exc
    if math.isnan(result) or result < minimum:
        raise FrontierSearchError(f"{field} must be at least {minimum}")
    return result


def _settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is not None and not isinstance(raw, dict):
        raise FrontierSearchError("config must be an object")
    explicit = dict(raw or {})
    requested_intensity = explicit.pop("search_intensity", None)
    result = dict(DEFAULTS)
    if requested_intensity is not None:
        requested_intensity = str(requested_intensity).strip().lower()
        if requested_intensity not in SEARCH_INTENSITY_PRESETS:
            raise FrontierSearchError(
                "search_intensity must be standard, deep_savings, or extreme_exploration"
            )
        result.update(SEARCH_INTENSITY_PRESETS[requested_intensity])
    result.update(explicit)
    if requested_intensity is None and "incumbent_max_hops" not in explicit:
        result["incumbent_max_hops"] = result["max_hops"]
    result["search_intensity"] = requested_intensity or "custom"
    for key in (
        "max_hops",
        "max_labels_per_node",
        "query_budget",
        "geography_quota",
        "mode_quota",
        "max_trace_entries",
        "incumbent_scan_limit",
        "incumbent_max_hops",
    ):
        result[key] = int(_number(result[key], key))
    for key in (
        "max_total_cost",
        "max_total_minutes",
        "minimum_connection_minutes",
        "max_unresolved_gap_km",
        "max_surface_gap_km",
        "max_connector_speed_kmh",
        "min_connector_cost_per_km",
        "query_cost_relaxation_factor",
    ):
        result[key] = _number(result[key], key)
    if result["max_hops"] < 1 or result["max_labels_per_node"] < 1:
        raise FrontierSearchError("max_hops and max_labels_per_node must be positive")
    if result["max_connector_speed_kmh"] <= 0:
        raise FrontierSearchError("max_connector_speed_kmh must be positive")
    modes = result.get("allowed_connector_modes")
    if not isinstance(modes, list) or not all(isinstance(mode, str) and mode for mode in modes):
        raise FrontierSearchError("allowed_connector_modes must be a string array")
    result["allowed_connector_modes"] = list(dict.fromkeys(mode.lower() for mode in modes))
    for map_key, legacy_key in (
        ("connector_mode_max_gap_km", "max_unresolved_gap_km"),
        ("connector_mode_max_speed_kmh", "max_connector_speed_kmh"),
    ):
        values = result.get(map_key)
        if not isinstance(values, dict):
            raise FrontierSearchError(f"{map_key} must be an object")
        normalized = {
            str(mode).lower(): _number(value, f"{map_key}.{mode}")
            for mode, value in values.items()
        }
        for mode in result["allowed_connector_modes"]:
            normalized.setdefault(mode, result[legacy_key])
        result[map_key] = normalized
    as_of = result.get("search_as_of")
    result["_search_as_of"] = _dt(as_of, "search_as_of") if as_of is not None else None
    return result


def _nodes(raw: Any) -> dict[str, dict[str, Any]]:
    source = raw.get("nodes") if isinstance(raw, dict) and isinstance(raw.get("nodes"), list) else raw
    if isinstance(source, dict):
        source = [dict(value, node_id=value.get("node_id", key)) for key, value in source.items()]
    if not isinstance(source, list):
        raise FrontierSearchError("nodes must be an array, mapping, or object containing nodes")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise FrontierSearchError(f"nodes[{index}] must be an object")
        node_id = str(item.get("node_id", "")).strip().upper()
        if not node_id or node_id in result:
            raise FrontierSearchError(f"nodes[{index}] has a missing or duplicate node_id")
        node = dict(item)
        node["node_id"] = node_id
        result[node_id] = node
    return result


def _edge_cost(edge: dict[str, Any]) -> float | None:
    raw = edge.get("effective_cost", edge.get("cost"))
    if isinstance(raw, dict):
        if str(raw.get("status", "known")).lower() == "unknown" or "amount" not in raw:
            return None
        raw = raw.get("amount", 0)
    if raw is None:
        return None
    baggage = edge.get("mandatory_baggage_cost", 0)
    if isinstance(baggage, dict):
        baggage = baggage.get("amount", 0)
    return _number(raw, f"edge {edge['edge_id']} cost") + _number(
        baggage, f"edge {edge['edge_id']} mandatory_baggage_cost"
    )


def _edges(raw: Any, nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    source = raw.get("edges") if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        raise FrontierSearchError("edges must be an array or object containing edges")
    result = []
    seen = set()
    seen_atomic_offers = set()
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise FrontierSearchError(f"edges[{index}] must be an object")
        edge = dict(item)
        edge_id = str(edge.get("edge_id", "")).strip()
        start = str(edge.get("from_node", "")).strip().upper()
        end = str(edge.get("to_node", "")).strip().upper()
        if not edge_id or edge_id in seen:
            raise FrontierSearchError(f"edges[{index}] has a missing or duplicate edge_id")
        if start not in nodes or end not in nodes:
            raise FrontierSearchError(f"edge {edge_id} references an unknown node")
        departure = _dt(edge.get("departure"), f"edge {edge_id}.departure")
        arrival = _dt(edge.get("arrival"), f"edge {edge_id}.arrival")
        if arrival <= departure:
            raise FrontierSearchError(f"edge {edge_id} must arrive after departure")
        source = edge.get("source")
        required_source = ("provider_id", "url", "observed_at", "evidence_type", "confidence")
        if not isinstance(source, dict) or any(source.get(field) in (None, "") for field in required_source):
            raise FrontierSearchError(
                f"edge {edge_id} must have sourced evidence with provider_id, url, observed_at, evidence_type, and confidence"
            )
        observed_at = _dt(source["observed_at"], f"edge {edge_id}.source.observed_at")
        expires_at = _dt(source["expires_at"], f"edge {edge_id}.source.expires_at") if source.get("expires_at") else None
        confidence = _number(source["confidence"], f"edge {edge_id}.source.confidence")
        if confidence > 1:
            raise FrontierSearchError(f"edge {edge_id}.source.confidence must not exceed 1")
        ticket_context = edge.get("ticket_context")
        atomic_offer_id = None
        if ticket_context is not None:
            if not isinstance(ticket_context, dict):
                raise FrontierSearchError(f"edge {edge_id}.ticket_context must be an object")
            if ticket_context.get("pricing_scope") != "atomic_offer":
                raise FrontierSearchError(f"edge {edge_id}.ticket_context.pricing_scope must be atomic_offer")
            atomic_offer_id = str(ticket_context.get("atomic_offer_id", "")).strip()
            components = ticket_context.get("component_edge_ids")
            if not atomic_offer_id or not isinstance(components, list) or len(components) < 2:
                raise FrontierSearchError(
                    f"edge {edge_id} atomic offer needs atomic_offer_id and at least two component_edge_ids"
                )
            via_nodes = ticket_context.get("via_nodes", [])
            if not isinstance(via_nodes, list) or any(str(node).upper() not in nodes for node in via_nodes):
                raise FrontierSearchError(f"edge {edge_id}.ticket_context.via_nodes references an unknown node")
            if atomic_offer_id in seen_atomic_offers:
                raise FrontierSearchError(
                    f"atomic offer {atomic_offer_id} must be represented by one hyperedge, not split into priced component edges"
                )
            seen_atomic_offers.add(atomic_offer_id)
        edge.update(
            {
                "edge_id": edge_id,
                "from_node": start,
                "to_node": end,
                "mode": str(edge.get("mode", "unknown")).lower(),
                "_departure": departure,
                "_arrival": arrival,
                "_cost": _edge_cost(edge),
                "_duration": (arrival - departure).total_seconds() / 60.0,
                "_observed_at": observed_at,
                "_expires_at": expires_at,
                "_snapshot_status": "not_evaluated",
                "_atomic_offer_id": atomic_offer_id,
            }
        )
        seen.add(edge_id)
        result.append(edge)
    return sorted(result, key=lambda edge: (edge["_departure"], edge["_arrival"], edge["edge_id"]))


def _geography(node: dict[str, Any]) -> str:
    return str(
        node.get("geography_bucket")
        or node.get("country_code")
        or node.get("country_or_region")
        or "unknown"
    ).strip().upper()


def _mode_bucket(modes: Iterable[str]) -> str:
    unique = set(modes)
    if not unique:
        return "seed"
    has_air = "flight" in unique
    has_surface = bool(unique & SURFACE_MODES)
    if has_air and has_surface:
        return "air_surface"
    if has_air:
        return "air_only"
    if has_surface:
        return "surface_only"
    return "+".join(sorted(unique))


def _label_id(direction: str, seed: str, edge_ids: tuple[str, ...]) -> str:
    token = ">".join(edge_ids) if edge_ids else "seed"
    return f"{direction[0]}:{seed}:{token}"


def _seed_label(direction: str, node_id: str) -> dict[str, Any]:
    return {
        "id": _label_id(direction, node_id, ()),
        "direction": direction,
        "seed": node_id,
        "node": node_id,
        "edge_ids": (),
        "node_path": (node_id,),
        "modes": (),
        "cost": 0.0,
        "departure": None,
        "arrival": None,
        "travel_minutes": 0.0,
        "hops": 0,
        "atomic_offer_ids": (),
    }


def _extend(label: dict[str, Any], edge: dict[str, Any], direction: str) -> dict[str, Any]:
    if direction == "forward":
        edge_ids = label["edge_ids"] + (edge["edge_id"],)
        node_path = label["node_path"] + (edge["to_node"],)
        modes = label["modes"] + (edge["mode"],)
        departure = label["departure"] or edge["_departure"]
        arrival = edge["_arrival"]
        node = edge["to_node"]
    else:
        edge_ids = (edge["edge_id"],) + label["edge_ids"]
        node_path = (edge["from_node"],) + label["node_path"]
        modes = (edge["mode"],) + label["modes"]
        departure = edge["_departure"]
        arrival = label["arrival"] or edge["_arrival"]
        node = edge["from_node"]
    atomic_offer_ids = label["atomic_offer_ids"]
    if edge["_atomic_offer_id"]:
        atomic_offer_ids = atomic_offer_ids + (edge["_atomic_offer_id"],)
    return {
        "id": _label_id(direction, label["seed"], edge_ids),
        "direction": direction,
        "seed": label["seed"],
        "node": node,
        "edge_ids": edge_ids,
        "node_path": node_path,
        "modes": modes,
        "cost": label["cost"] + edge["_cost"],
        "departure": departure,
        "arrival": arrival,
        "travel_minutes": (arrival - departure).total_seconds() / 60.0,
        "hops": label["hops"] + 1,
        "atomic_offer_ids": atomic_offer_ids,
    }


def _dominates(left: dict[str, Any], right: dict[str, Any], direction: str) -> bool:
    common = (
        left["cost"] <= right["cost"]
        and left["travel_minutes"] <= right["travel_minutes"]
        and left["hops"] <= right["hops"]
    )
    if not common:
        return False
    if direction == "forward":
        time_ok = left["arrival"] is None or (
            right["arrival"] is not None and left["arrival"] <= right["arrival"]
        )
        strict_time = left["arrival"] != right["arrival"]
    else:
        # A later departure improves connection feasibility, but it may also
        # arrive later and increase the end-to-end elapsed time.  Temporal
        # labels are therefore comparable only when both boundary times are no
        # worse; static T-MDA dominance is not sufficient for scheduled edges.
        time_ok = left["departure"] is None or (
            right["departure"] is not None
            and left["departure"] >= right["departure"]
            and left["arrival"] is not None
            and right["arrival"] is not None
            and left["arrival"] <= right["arrival"]
        )
        strict_time = left["departure"] != right["departure"] or left["arrival"] != right["arrival"]
    return time_ok and (
        left["cost"] < right["cost"]
        or left["travel_minutes"] < right["travel_minutes"]
        or left["hops"] < right["hops"]
        or strict_time
    )


def _static_bounds(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seeds: set[str],
    *,
    reverse: bool,
    weight: str,
) -> dict[str, float]:
    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        if edge[weight] is None or edge["_snapshot_status"] != "eligible":
            continue
        if reverse:
            adjacency[edge["to_node"]].append((edge["from_node"], edge[weight]))
        else:
            adjacency[edge["from_node"]].append((edge["to_node"], edge[weight]))
    distances = {node: math.inf for node in nodes}
    heap: list[tuple[float, str]] = []
    for seed in sorted(seeds):
        distances[seed] = 0.0
        heapq.heappush(heap, (0.0, seed))
    while heap:
        distance, node = heapq.heappop(heap)
        if distance != distances[node]:
            continue
        for neighbour, edge_weight in sorted(adjacency.get(node, [])):
            candidate = distance + edge_weight
            if candidate < distances[neighbour]:
                distances[neighbour] = candidate
                heapq.heappush(heap, (candidate, neighbour))
    return distances


def _best_incumbent(
    origins: set[str],
    destinations: set[str],
    outgoing: dict[str, list[dict[str, Any]]],
    settings: dict[str, Any],
    complete_route_validator: Any = None,
) -> tuple[float, tuple[str, ...] | None, int]:
    heap: list[tuple[float, int, str, tuple[str, ...], tuple[str, ...], datetime | None, datetime | None]] = []
    serial = 0
    for origin in sorted(origins):
        heapq.heappush(heap, (0.0, serial, origin, (), (origin,), None, None))
        serial += 1
    scanned = 0
    connection = timedelta(minutes=settings["minimum_connection_minutes"])
    while heap and scanned < settings["incumbent_scan_limit"]:
        cost, _, node, edge_ids, path, first_departure, arrival = heapq.heappop(heap)
        scanned += 1
        if node in destinations and edge_ids:
            audit = complete_route_validator(edge_ids) if complete_route_validator else {"accepted": True}
            if audit.get("accepted", False):
                return cost, edge_ids, scanned
            continue
        if len(edge_ids) >= settings.get("incumbent_max_hops", settings["max_hops"]):
            continue
        for edge in outgoing.get(node, []):
            if edge["_snapshot_status"] != "eligible" or edge["_cost"] is None:
                continue
            if edge["to_node"] in path:
                continue
            if arrival is not None and edge["_departure"] < arrival + connection:
                continue
            new_first = first_departure or edge["_departure"]
            elapsed = (edge["_arrival"] - new_first).total_seconds() / 60.0
            new_cost = cost + edge["_cost"]
            if new_cost > settings["max_total_cost"] or elapsed > settings["max_total_minutes"]:
                continue
            heapq.heappush(
                heap,
                (
                    new_cost,
                    serial,
                    edge["to_node"],
                    edge_ids + (edge["edge_id"],),
                    path + (edge["to_node"],),
                    new_first,
                    edge["_arrival"],
                ),
            )
            serial += 1
    return math.inf, None, scanned


def _trace_recorder(limit: int) -> tuple[list[dict[str, Any]], Any]:
    trace: list[dict[str, Any]] = []

    def record(stage: str, reason: str, **details: Any) -> None:
        if len(trace) < limit:
            trace.append({"sequence": len(trace) + 1, "stage": stage, "reason": reason, **details})

    return trace, record


def _search_direction(
    direction: str,
    seeds: set[str],
    adjacency: dict[str, list[dict[str, Any]]],
    nodes: dict[str, dict[str, Any]],
    settings: dict[str, Any],
    cost_bounds: dict[str, float],
    time_bounds: dict[str, float],
    incumbent: float,
    record: Any,
) -> dict[str, list[dict[str, Any]]]:
    labels_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    frontier = [_seed_label(direction, seed) for seed in sorted(seeds)]
    for label in frontier:
        labels_by_node[label["node"]].append(label)
    connection = timedelta(minutes=settings["minimum_connection_minutes"])

    for depth in range(1, settings["max_hops"] + 1):
        candidates: list[dict[str, Any]] = []
        for label in sorted(frontier, key=lambda item: item["id"]):
            for edge in adjacency.get(label["node"], []):
                if edge["_snapshot_status"] != "eligible":
                    record(
                        "evidence",
                        "snapshot_ineligible",
                        direction=direction,
                        label_id=label["id"],
                        edge_id=edge["edge_id"],
                        snapshot_status=edge["_snapshot_status"],
                    )
                    continue
                if edge["_cost"] is None:
                    record(
                        "evidence",
                        "price_unknown",
                        direction=direction,
                        label_id=label["id"],
                        edge_id=edge["edge_id"],
                    )
                    continue
                next_node = edge["to_node"] if direction == "forward" else edge["from_node"]
                if next_node in label["node_path"]:
                    record("expand", "cycle", direction=direction, label_id=label["id"], edge_id=edge["edge_id"])
                    continue
                if direction == "forward" and label["arrival"] is not None:
                    if edge["_departure"] < label["arrival"] + connection:
                        record("expand", "time_incompatible", direction=direction, label_id=label["id"], edge_id=edge["edge_id"])
                        continue
                if direction == "backward" and label["departure"] is not None:
                    if edge["_arrival"] > label["departure"] - connection:
                        record("expand", "time_incompatible", direction=direction, label_id=label["id"], edge_id=edge["edge_id"])
                        continue
                candidate = _extend(label, edge, direction)
                lower_cost = cost_bounds.get(candidate["node"], math.inf)
                projected_cost = candidate["cost"] + lower_cost
                cost_ceiling = settings["max_total_cost"]
                if math.isfinite(lower_cost) and projected_cost > cost_ceiling:
                    record(
                        "bound",
                        "cost_lower_bound",
                        direction=direction,
                        label_id=candidate["id"],
                        accumulated_cost=round(candidate["cost"], 6),
                        remaining_lower_bound=round(lower_cost, 6),
                        ceiling=round(cost_ceiling, 6),
                    )
                    continue
                lower_time = time_bounds.get(candidate["node"], math.inf)
                if math.isfinite(lower_time) and candidate["travel_minutes"] + lower_time > settings["max_total_minutes"]:
                    record(
                        "bound",
                        "time_lower_bound",
                        direction=direction,
                        label_id=candidate["id"],
                        accumulated_minutes=round(candidate["travel_minutes"], 6),
                        remaining_lower_bound=round(lower_time, 6),
                        ceiling=round(settings["max_total_minutes"], 6),
                    )
                    continue
                candidates.append(candidate)

        accepted: list[dict[str, Any]] = []
        geography_counts: dict[str, int] = defaultdict(int)
        mode_counts: dict[str, int] = defaultdict(int)
        ordering = sorted(
            candidates,
            key=lambda item: (
                item["cost"] + (cost_bounds.get(item["node"], math.inf)),
                item["travel_minutes"],
                item["hops"],
                item["id"],
            ),
        )
        for candidate in ordering:
            existing = labels_by_node[candidate["node"]]
            dominator = next((label for label in existing if _dominates(label, candidate, direction)), None)
            if dominator:
                record(
                    "pareto",
                    "pareto_dominated",
                    direction=direction,
                    label_id=candidate["id"],
                    dominated_by=dominator["id"],
                )
                continue
            dominated = [label for label in existing if _dominates(candidate, label, direction)]
            for label in dominated:
                existing.remove(label)
                record(
                    "pareto",
                    "pareto_replaced",
                    direction=direction,
                    label_id=label["id"],
                    dominated_by=candidate["id"],
                )
            geography = _geography(nodes[candidate["node"]])
            mode = _mode_bucket(candidate["modes"])
            if geography_counts[geography] >= settings["geography_quota"]:
                record(
                    "diversity",
                    "geography_quota",
                    direction=direction,
                    label_id=candidate["id"],
                    bucket=geography,
                    quota=settings["geography_quota"],
                )
                continue
            if mode_counts[mode] >= settings["mode_quota"]:
                record(
                    "diversity",
                    "mode_quota",
                    direction=direction,
                    label_id=candidate["id"],
                    bucket=mode,
                    quota=settings["mode_quota"],
                )
                continue
            if len(existing) >= settings["max_labels_per_node"]:
                record(
                    "pareto",
                    "label_limit",
                    direction=direction,
                    label_id=candidate["id"],
                    node=candidate["node"],
                    limit=settings["max_labels_per_node"],
                )
                continue
            existing.append(candidate)
            accepted.append(candidate)
            geography_counts[geography] += 1
            mode_counts[mode] += 1
        frontier = accepted
        if not frontier:
            break
    return {node: sorted(labels, key=lambda item: item["id"]) for node, labels in sorted(labels_by_node.items())}


def _compatible(forward: dict[str, Any], backward: dict[str, Any], settings: dict[str, Any]) -> bool:
    if forward["arrival"] is None or backward["departure"] is None:
        return True
    return forward["arrival"] + timedelta(minutes=settings["minimum_connection_minutes"]) <= backward["departure"]


def _route(forward: dict[str, Any], backward: dict[str, Any]) -> dict[str, Any] | None:
    edge_ids = forward["edge_ids"] + backward["edge_ids"]
    if not edge_ids:
        return None
    path = forward["node_path"] + backward["node_path"][1:]
    departure = forward["departure"] or backward["departure"]
    arrival = backward["arrival"] or forward["arrival"]
    route_hash = hashlib.sha1((">".join(edge_ids)).encode("utf-8")).hexdigest()[:12]
    return {
        "route_id": f"route-{route_hash}",
        "meeting_node": forward["node"],
        "edge_ids": list(edge_ids),
        "node_path": list(path),
        "modes": list(forward["modes"] + backward["modes"]),
        "total_cost": round(forward["cost"] + backward["cost"], 6),
        "total_travel_minutes": round((arrival - departure).total_seconds() / 60.0, 6),
        "departure": _iso(departure),
        "arrival": _iso(arrival),
        "forward_label_id": forward["id"],
        "backward_label_id": backward["id"],
        "atomic_offer_ids": list(dict.fromkeys(forward["atomic_offer_ids"] + backward["atomic_offer_ids"])),
    }


def _route_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["total_cost"] <= right["total_cost"]
        and left["total_travel_minutes"] <= right["total_travel_minutes"]
        and len(left["edge_ids"]) <= len(right["edge_ids"])
        and (
            left["total_cost"] < right["total_cost"]
            or left["total_travel_minutes"] < right["total_travel_minutes"]
            or len(left["edge_ids"]) < len(right["edge_ids"])
        )
    )


def _haversine(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    a = left.get("coordinates")
    b = right.get("coordinates")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    try:
        lat1, lon1 = math.radians(float(a["latitude"])), math.radians(float(a["longitude"]))
        lat2, lon2 = math.radians(float(b["latitude"])), math.radians(float(b["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _serial_label(label: dict[str, Any]) -> dict[str, Any]:
    return {
        "label_id": label["id"],
        "direction": label["direction"],
        "seed": label["seed"],
        "node": label["node"],
        "edge_ids": list(label["edge_ids"]),
        "node_path": list(label["node_path"]),
        "cost": round(label["cost"], 6),
        "travel_minutes": round(label["travel_minutes"], 6),
        "departure": _iso(label["departure"]),
        "arrival": _iso(label["arrival"]),
        "geography_bucket": None,
        "mode_bucket": _mode_bucket(label["modes"]),
    }


def _gap_candidates(
    forward_labels: dict[str, list[dict[str, Any]]],
    backward_labels: dict[str, list[dict[str, Any]]],
    nodes: dict[str, dict[str, Any]],
    existing_pairs: set[tuple[str, str]],
    settings: dict[str, Any],
    incumbent: float,
    record: Any,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    raw: list[dict[str, Any]] = []
    coverage_statuses: list[dict[str, Any]] = []
    all_forward = [label for labels in forward_labels.values() for label in labels if label["edge_ids"]]
    all_backward = [label for labels in backward_labels.values() for label in labels if label["edge_ids"]]
    for forward in sorted(all_forward, key=lambda item: item["id"]):
        for backward in sorted(all_backward, key=lambda item: item["id"]):
            if forward["node"] == backward["node"] or (forward["node"], backward["node"]) in existing_pairs:
                continue
            distance = _haversine(nodes[forward["node"]], nodes[backward["node"]])
            if distance is None:
                record("gap", "insufficient_endpoint_data", from_label=forward["id"], to_label=backward["id"])
                coverage_statuses.append(
                    {
                        "from_node": forward["node"],
                        "to_node": backward["node"],
                        "status": "insufficient_endpoint_data",
                        "reason": "coordinates_unknown",
                    }
                )
                continue
            allowed = settings["allowed_connector_modes"]
            modes = [
                mode
                for mode in allowed
                if distance <= settings["connector_mode_max_gap_km"].get(mode, 0)
            ]
            if not modes:
                record(
                    "gap",
                    "mode_distance_not_covered",
                    from_label=forward["id"],
                    to_label=backward["id"],
                    distance_km=round(distance, 3),
                    mode_distance_limits=settings["connector_mode_max_gap_km"],
                )
                coverage_statuses.append(
                    {
                        "from_node": forward["node"],
                        "to_node": backward["node"],
                        "status": "not_covered",
                        "reason": "mode_distance_not_covered",
                        "straight_line_distance_km": round(distance, 3),
                    }
                )
                continue
            minimum_minutes = min(
                distance / settings["connector_mode_max_speed_kmh"].get(mode, settings["max_connector_speed_kmh"]) * 60.0
                for mode in modes
            )
            earliest = forward["arrival"]
            latest = backward["departure"]
            if earliest is not None and latest is not None:
                available = (latest - earliest).total_seconds() / 60.0
                if available < minimum_minutes + settings["minimum_connection_minutes"]:
                    record(
                        "gap",
                        "gap_time_infeasible",
                        from_label=forward["id"],
                        to_label=backward["id"],
                        available_minutes=round(available, 3),
                        minimum_minutes=round(minimum_minutes, 3),
                    )
                    continue
            minimum_cost = distance * settings["min_connector_cost_per_km"]
            total_cost_lower_bound = forward["cost"] + minimum_cost + backward["cost"]
            ceiling = min(settings["max_total_cost"], incumbent * settings["query_cost_relaxation_factor"])
            if math.isfinite(ceiling) and total_cost_lower_bound > ceiling:
                record(
                    "gap",
                    "cost_lower_bound",
                    from_label=forward["id"],
                    to_label=backward["id"],
                    total_cost_lower_bound=round(total_cost_lower_bound, 6),
                    ceiling=round(ceiling, 6),
                )
                continue
            geography = f"{_geography(nodes[forward['node']])}->{_geography(nodes[backward['node']])}"
            mode_bucket = "air_or_surface" if len(modes) > 1 else (modes[0] if modes else "unknown")
            earliest_iso = _iso(earliest)
            latest_iso = _iso(latest)
            semantic_modes = tuple(sorted(modes))
            request_key = json.dumps(
                [forward["node"], backward["node"], earliest_iso, latest_iso, semantic_modes],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            request_hash = hashlib.sha1(request_key.encode("utf-8")).hexdigest()[:12]
            consumer_context = {
                "forward_label_id": forward["id"],
                "backward_label_id": backward["id"],
                "total_cost_lower_bound": round(total_cost_lower_bound, 6),
            }
            raw.append(
                {
                    "request_id": f"gap-{request_hash}",
                    "request_type": "unresolved_connector",
                    "from_node": forward["node"],
                    "to_node": backward["node"],
                    "earliest_departure": earliest_iso,
                    "latest_arrival": latest_iso,
                    "straight_line_distance_km": round(distance, 3),
                    "connector_time_lower_bound_minutes": round(minimum_minutes, 3),
                    "connector_cost_lower_bound": round(minimum_cost, 6),
                    "total_cost_lower_bound": round(total_cost_lower_bound, 6),
                    "suggested_modes": modes,
                    "provider_kinds": [f"{mode}_schedule_or_offer" for mode in modes],
                    "forward_label_id": forward["id"],
                    "backward_label_id": backward["id"],
                    "consumer_contexts": [consumer_context],
                    "geography_bucket": geography,
                    "mode_bucket": mode_bucket,
                    "priority": [round(total_cost_lower_bound, 6), round(minimum_minutes, 3), request_hash],
                }
            )
    deduplicated: dict[tuple[str, str, str | None, str | None, tuple[str, ...]], dict[str, Any]] = {}
    for request in sorted(raw, key=lambda item: tuple(item["priority"])):
        key = (
            request["from_node"],
            request["to_node"],
            request["earliest_departure"],
            request["latest_arrival"],
            tuple(sorted(request["suggested_modes"])),
        )
        if key not in deduplicated:
            deduplicated[key] = request
        else:
            retained = deduplicated[key]
            known_contexts = {
                (context["forward_label_id"], context["backward_label_id"])
                for context in retained["consumer_contexts"]
            }
            for context in request["consumer_contexts"]:
                context_key = (context["forward_label_id"], context["backward_label_id"])
                if context_key not in known_contexts:
                    retained["consumer_contexts"].append(context)
                    known_contexts.add(context_key)
            retained["consumer_contexts"].sort(
                key=lambda context: (context["forward_label_id"], context["backward_label_id"])
            )
            record(
                "gap",
                "duplicate_gap",
                request_id=request["request_id"],
                merged_into=retained["request_id"],
                consumer_context_count=len(retained["consumer_contexts"]),
            )
    return list(deduplicated.values()), len(raw), coverage_statuses


def _allocate_requests(
    candidates: list[dict[str, Any]], settings: dict[str, Any], record: Any
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    geography_counts: dict[str, int] = defaultdict(int)
    mode_counts: dict[str, int] = defaultdict(int)
    for request in sorted(candidates, key=lambda item: tuple(item["priority"])):
        geography = request["geography_bucket"]
        mode = request["mode_bucket"]
        if geography_counts[geography] >= settings["geography_quota"]:
            record("query_allocation", "geography_quota", request_id=request["request_id"], bucket=geography)
            continue
        if mode_counts[mode] >= settings["mode_quota"]:
            record("query_allocation", "mode_quota", request_id=request["request_id"], bucket=mode)
            continue
        if len(selected) >= settings["query_budget"]:
            record("query_allocation", "query_budget_exhausted", request_id=request["request_id"], budget=settings["query_budget"])
            continue
        request = dict(request)
        request["query_sequence"] = len(selected) + 1
        selected.append(request)
        geography_counts[geography] += 1
        mode_counts[mode] += 1
    return selected


def frontier_search(
    nodes: Any,
    edges: Any,
    origin_nodes: Iterable[str],
    destination_nodes: Iterable[str],
    config: dict[str, Any] | None = None,
    *,
    complete_route_validator: Any = None,
) -> dict[str, Any]:
    """Search observed edges and return routes plus live-provider gap requests.

    The result is deterministic for equivalent inputs.  Bounds are explicitly
    scoped to the observed graph; unresolved connections are never represented as
    real routes until a provider supplies a concrete ``TravelEdge``.
    """

    settings = _settings(config)
    node_map = _nodes(nodes)
    edge_list = _edges(edges, node_map)
    origins = {str(node).strip().upper() for node in origin_nodes}
    destinations = {str(node).strip().upper() for node in destination_nodes}
    if not origins or not destinations:
        raise FrontierSearchError("origin_nodes and destination_nodes must be non-empty")
    missing = sorted((origins | destinations) - set(node_map))
    if missing:
        raise FrontierSearchError("unknown endpoint nodes: " + ", ".join(missing))

    trace, record = _trace_recorder(settings["max_trace_entries"])
    as_of = settings["_search_as_of"] or max(edge["_observed_at"] for edge in edge_list)
    snapshot_counts: dict[str, int] = defaultdict(int)
    for edge in edge_list:
        if edge["_observed_at"] > as_of:
            edge["_snapshot_status"] = "future_snapshot"
        elif edge["_expires_at"] is not None and edge["_expires_at"] <= as_of:
            edge["_snapshot_status"] = "expired"
        else:
            edge["_snapshot_status"] = "eligible"
        snapshot_counts[edge["_snapshot_status"]] += 1
    record(
        "initialization",
        "snapshot_audit",
        as_of=_iso(as_of),
        counts=dict(sorted(snapshot_counts.items())),
    )
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edge_list:
        outgoing[edge["from_node"]].append(edge)
        incoming[edge["to_node"]].append(edge)
    for values in outgoing.values():
        values.sort(key=lambda item: (item["_departure"], item["edge_id"]))
    for values in incoming.values():
        values.sort(key=lambda item: (item["_arrival"], item["edge_id"]), reverse=True)

    incumbent, incumbent_edges, incumbent_scanned = _best_incumbent(
        origins, destinations, outgoing, settings, complete_route_validator
    )
    record(
        "initialization",
        "incumbent_scan",
        complete=math.isfinite(incumbent),
        incumbent_cost=round(incumbent, 6) if math.isfinite(incumbent) else None,
        incumbent_edge_ids=list(incumbent_edges or ()),
        scanned_states=incumbent_scanned,
    )

    forward_cost = _static_bounds(node_map, edge_list, destinations, reverse=True, weight="_cost")
    forward_time = _static_bounds(node_map, edge_list, destinations, reverse=True, weight="_duration")
    backward_cost = _static_bounds(node_map, edge_list, origins, reverse=False, weight="_cost")
    backward_time = _static_bounds(node_map, edge_list, origins, reverse=False, weight="_duration")

    forward_labels = _search_direction(
        "forward", origins, outgoing, node_map, settings, forward_cost, forward_time, incumbent, record
    )
    backward_labels = _search_direction(
        "backward", destinations, incoming, node_map, settings, backward_cost, backward_time, incumbent, record
    )

    routes_by_edges: dict[tuple[str, ...], dict[str, Any]] = {}
    meeting_nodes = sorted(set(forward_labels) & set(backward_labels))
    for node in meeting_nodes:
        for forward in forward_labels[node]:
            for backward in backward_labels[node]:
                if not _compatible(forward, backward, settings):
                    record("pair", "time_incompatible", forward_label_id=forward["id"], backward_label_id=backward["id"])
                    continue
                route = _route(forward, backward)
                if route is None:
                    continue
                if route["total_cost"] > settings["max_total_cost"]:
                    record("pair", "cost_ceiling", route_id=route["route_id"], total_cost=route["total_cost"])
                    continue
                if route["total_travel_minutes"] > settings["max_total_minutes"]:
                    record("pair", "time_ceiling", route_id=route["route_id"], total_minutes=route["total_travel_minutes"])
                    continue
                audit = complete_route_validator(route["edge_ids"]) if complete_route_validator else {"accepted": True}
                route["complete_route_audit"] = audit
                if not audit.get("accepted", False):
                    record(
                        "pair",
                        "complete_route_audit_rejected",
                        route_id=route["route_id"],
                        reason_codes=audit.get("reason_codes", []),
                    )
                    continue
                key = tuple(route["edge_ids"])
                routes_by_edges.setdefault(key, route)
    routes = sorted(routes_by_edges.values(), key=lambda item: (item["total_cost"], item["total_travel_minutes"], item["route_id"]))
    pareto = [
        route
        for route in routes
        if not any(other["route_id"] != route["route_id"] and _route_dominates(other, route) for other in routes)
    ]

    existing_pairs = {(edge["from_node"], edge["to_node"]) for edge in edge_list}
    gap_candidates, raw_gap_count, coverage_statuses = _gap_candidates(
        forward_labels,
        backward_labels,
        node_map,
        existing_pairs,
        settings,
        incumbent,
        record,
    )
    requests = _allocate_requests(gap_candidates, settings, record)
    selected_request_ids = {request["request_id"] for request in requests}
    allocation_reasons = {
        item.get("request_id"): item["reason"]
        for item in trace
        if item["stage"] == "query_allocation" and item.get("request_id")
    }
    gap_statuses = list(coverage_statuses)
    for candidate in gap_candidates:
        selected = candidate["request_id"] in selected_request_ids
        gap_statuses.append(
            {
                "request_id": candidate["request_id"],
                "from_node": candidate["from_node"],
                "to_node": candidate["to_node"],
                "status": "pending_provider_query" if selected else "not_found_in_budget",
                "reason": "selected" if selected else allocation_reasons.get(candidate["request_id"], "budget_or_diversity_limit"),
            }
        )

    def finite_map(values: dict[str, float]) -> dict[str, float | None]:
        return {
            node: round(values[node], 6) if math.isfinite(values[node]) else None
            for node in sorted(values)
        }

    serialized_forward = [_serial_label(label) for node in sorted(forward_labels) for label in forward_labels[node]]
    serialized_backward = [_serial_label(label) for node in sorted(backward_labels) for label in backward_labels[node]]
    for label in serialized_forward + serialized_backward:
        label["geography_bucket"] = _geography(node_map[label["node"]])

    reason_counts: dict[str, int] = defaultdict(int)
    for item in trace:
        reason_counts[item["reason"]] += 1
    return {
        "engine_version": ENGINE_VERSION,
        "search_model": "bidirectional_multi_source_pareto_frontier",
        "search_intensity": settings["search_intensity"],
        "effective_search_budget": {
            key: settings[key]
            for key in (
                "max_hops",
                "max_labels_per_node",
                "query_budget",
                "geography_quota",
                "mode_quota",
                "incumbent_scan_limit",
            )
        },
        "origins": sorted(origins),
        "destinations": sorted(destinations),
        "incumbent": {
            "total_cost": round(incumbent, 6) if math.isfinite(incumbent) else None,
            "edge_ids": list(incumbent_edges or ()),
            "scanned_states": incumbent_scanned,
        },
        "snapshot_audit": {
            "as_of": _iso(as_of),
            "status_counts": dict(sorted(snapshot_counts.items())),
            "eligible_edge_ids": sorted(edge["edge_id"] for edge in edge_list if edge["_snapshot_status"] == "eligible"),
            "excluded_edge_ids": sorted(edge["edge_id"] for edge in edge_list if edge["_snapshot_status"] != "eligible"),
            "rule": "only snapshots observed by and unexpired at search_as_of may form priced routes",
        },
        "lower_bounds": {
            "scope": "observed_graph_only",
            "forward_remaining_cost": finite_map(forward_cost),
            "forward_remaining_minutes": finite_map(forward_time),
            "backward_accumulated_cost": finite_map(backward_cost),
            "backward_accumulated_minutes": finite_map(backward_time),
        },
        "frontiers": {"forward": serialized_forward, "backward": serialized_backward},
        "routes": routes,
        "pareto_route_ids": [route["route_id"] for route in pareto],
        "unresolved_gaps": gap_candidates,
        "gap_statuses": gap_statuses,
        "provider_requests": requests,
        "query_budget": {
            "limit": settings["query_budget"],
            "used": len(requests),
            "remaining": settings["query_budget"] - len(requests),
            "eligible_gap_count": len(gap_candidates),
            "raw_gap_pair_count": raw_gap_count,
            "exhausted": len(gap_candidates) > len(requests),
        },
        "prune_trace": trace,
        "prune_reason_counts": dict(sorted(reason_counts.items())),
        "searched_scope": {
            "observed_nodes": len(node_map),
            "observed_edges": len(edge_list),
            "forward_labels": len(serialized_forward),
            "backward_labels": len(serialized_backward),
            "complete_routes": len(routes),
            "claim": "best candidates within observed edges, constraints, diversity quotas, and provider query budget",
        },
    }


__all__ = ["ENGINE_VERSION", "SEARCH_INTENSITY_PRESETS", "FrontierSearchError", "frontier_search"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic bidirectional multimodal frontier search")
    parser.add_argument("--input", required=True, help="JSON bundle with nodes, edges, origin_nodes, and destination_nodes")
    parser.add_argument("--output", required=True, help="Path for the JSON result")
    args = parser.parse_args(argv)
    try:
        bundle = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise FrontierSearchError("input bundle must be an object")
        result = frontier_search(
            bundle.get("nodes"),
            bundle.get("edges"),
            bundle.get("origin_nodes", []),
            bundle.get("destination_nodes", []),
            bundle.get("config"),
        )
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"valid": True, "routes": len(result["routes"]), "provider_requests": len(result["provider_requests"]), "output": _display_path(target)}, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, FrontierSearchError) as exc:
        print(f"frontier_search: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
