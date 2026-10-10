#!/usr/bin/env python3
"""Bounded corridor-aware temporal-frontier request scheduler.

The scheduler never turns an unknown connector into a travel edge.  It only
turns observed forward/backward flight anchors into bounded calendar and
research requests that an AI/browser evidence loop may execute later.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.7.1"

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": VERSION,
    "policy_id": "temporal-corridor-default",
    "query_budget": 12,
    "date_search": {
        "calendar_bucket_days": 7,
        "anchor_window_days_before": 2,
        "anchor_window_days_after": 3,
        "max_pareto_dates_per_corridor_direction": 4,
        "max_exact_dates_per_request": 14,
        "pareto_dimensions": [
            "mandatory_total_cost",
            "elapsed_minutes",
            "departure_time",
            "arrival_time",
        ],
    },
    "corridor_discovery": {
        "max_anchor_pairs": 24,
        "max_days_between_anchors": 14,
        "include_observed_connector_paths": True,
        "unknown_connector_research": True,
    },
    "quotas": {
        "per_corridor": 3,
        "per_geographic_cell": 6,
        "per_route_shape": 6,
        "per_date_bucket": 6,
        "exploration_reserved": 3,
        "conventional_baseline_reserved": 1,
    },
}


class SchedulerError(ValueError):
    pass


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerError(f"{field} must be an ISO date-time string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchedulerError(f"{field} must be an ISO date-time string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _merge_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_POLICY)
    if policy is None:
        return merged
    if not isinstance(policy, dict):
        raise SchedulerError("policy must be an object")
    for key, value in policy.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    if merged.get("schema_version") != VERSION:
        raise SchedulerError(f"policy.schema_version must be {VERSION}")
    if not isinstance(merged.get("query_budget"), int) or merged["query_budget"] < 1:
        raise SchedulerError("policy.query_budget must be a positive integer")
    for section in ("date_search", "corridor_discovery", "quotas"):
        if not isinstance(merged.get(section), dict):
            raise SchedulerError(f"policy.{section} must be an object")
    for key in (
        "per_corridor",
        "per_geographic_cell",
        "per_route_shape",
        "per_date_bucket",
    ):
        if not isinstance(merged["quotas"].get(key), int) or merged["quotas"][key] < 1:
            raise SchedulerError(f"policy.quotas.{key} must be a positive integer")
    for key in ("exploration_reserved", "conventional_baseline_reserved"):
        if not isinstance(merged["quotas"].get(key), int) or merged["quotas"][key] < 0:
            raise SchedulerError(f"policy.quotas.{key} must be a non-negative integer")
    return merged


def _items(value: Any, wrapper: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get(wrapper, [])
    if not isinstance(value, list):
        raise SchedulerError(f"{wrapper} must be an array or wrapper object")
    if not all(isinstance(item, dict) for item in value):
        raise SchedulerError(f"{wrapper} entries must be objects")
    return copy.deepcopy(value)


def _node_index(nodes: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in _items(nodes, "nodes"):
        node_id = node.get("node_id")
        if isinstance(node_id, str) and node_id:
            result[node_id] = node
    return result


def _pool_nodes(profile: dict[str, Any], side: str) -> list[str]:
    explicit = profile.get(f"{side}_airport_nodes")
    if isinstance(explicit, list) and explicit:
        return [str(item) for item in explicit]
    pool = profile.get(f"{side}_pool")
    if isinstance(pool, dict):
        seeds = pool.get("seed_nodes")
        if isinstance(seeds, list) and seeds:
            return [str(item) for item in seeds]
    fallback = profile.get("origin_start_nodes" if side == "origin" else "destination_end_nodes")
    return [str(item) for item in fallback] if isinstance(fallback, list) else []


def _date_contract(profile: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    hard = profile.get("date_window")
    if not isinstance(hard, dict):
        raise SchedulerError("profile.date_window must be an object")
    hard_start = _parse_time(hard.get("start"), "profile.date_window.start")
    hard_end = _parse_time(hard.get("end"), "profile.date_window.end")
    if hard_end <= hard_start:
        raise SchedulerError("profile.date_window.end must be after start")

    raw = profile.get("date_flexibility", {"mode": "bounded_window"})
    if not isinstance(raw, dict):
        raise SchedulerError("profile.date_flexibility must be an object")
    mode = raw.get("mode", "bounded_window")
    if mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise SchedulerError("unsupported profile.date_flexibility.mode")

    dates: list[str] = []
    if mode == "exact_dates":
        values = raw.get("exact_departure_dates", raw.get("dates"))
        if not isinstance(values, list) or not values:
            raise SchedulerError("exact_dates requires exact_departure_dates[]")
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise SchedulerError(f"exact_departure_dates[{index}] must be a date")
            try:
                day = datetime.fromisoformat(value).date()
            except ValueError as exc:
                raise SchedulerError(f"exact_departure_dates[{index}] must be YYYY-MM-DD") from exc
            instant = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            if instant.date() < hard_start.date() or instant.date() > hard_end.date():
                raise SchedulerError("exact departure date lies outside profile.date_window")
            dates.append(day.isoformat())
        dates = sorted(set(dates))
        if len(dates) > policy["date_search"]["max_exact_dates_per_request"]:
            raise SchedulerError("exact departure date count exceeds policy limit")

    bounded_start, bounded_end = hard_start, hard_end
    if mode == "bounded_window":
        if raw.get("start") is not None:
            bounded_start = max(hard_start, _parse_time(raw["start"], "date_flexibility.start"))
        if raw.get("end") is not None:
            bounded_end = min(hard_end, _parse_time(raw["end"], "date_flexibility.end"))
        if bounded_end <= bounded_start:
            raise SchedulerError("bounded date flexibility has an empty window")

    return {
        "mode": mode,
        "hard_start": hard_start,
        "hard_end": hard_end,
        "search_start": bounded_start,
        "search_end": bounded_end,
        "exact_dates": dates,
    }


def _edge_cost(edge: dict[str, Any]) -> float:
    total = 0.0
    for field in ("cost", "mandatory_baggage_cost"):
        money = edge.get(field)
        if isinstance(money, dict) and isinstance(money.get("amount"), (int, float)):
            total += float(money["amount"])
    return total


def _observed_anchor(edge: dict[str, Any]) -> bool:
    if edge.get("mode") != "flight":
        return False
    if not all(isinstance(edge.get(key), str) and edge[key] for key in ("edge_id", "from_node", "to_node")):
        return False
    explicit = edge.get("evidence_status", edge.get("status"))
    if explicit in {"unknown", "hypothesis", "unverified", "rejected", "no_result"}:
        return False
    try:
        departure = _parse_time(edge.get("departure"), "edge.departure")
        arrival = _parse_time(edge.get("arrival"), "edge.arrival")
    except SchedulerError:
        return False
    return arrival > departure and isinstance(edge.get("cost"), dict)


def _cell(node_id: str, nodes: dict[str, dict[str, Any]]) -> str:
    node = nodes.get(node_id, {})
    for key in ("geographic_cell", "country_code", "country_or_region"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper().replace(" ", "_")
    return "UNMAPPED"


def _known_path(
    start: str,
    end: str,
    edges: Iterable[dict[str, Any]],
    excluded: set[str],
    earliest: datetime,
    latest: datetime,
) -> tuple[list[str], list[str]] | None:
    if start == end:
        return ([], [])
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        if edge.get("edge_id") in excluded:
            continue
        if not _observed_or_surface(edge):
            continue
        adjacency[str(edge.get("from_node"))].append(edge)
    queue: deque[tuple[str, list[str], list[str], datetime]] = deque([(start, [], [], earliest)])
    best_arrival = {start: earliest}
    while queue:
        node, edge_ids, modes, current_time = queue.popleft()
        if len(edge_ids) >= 5:
            continue
        for edge in sorted(adjacency.get(node, []), key=lambda item: str(item.get("edge_id"))):
            departure = _parse_time(edge["departure"], "connector departure")
            arrival = _parse_time(edge["arrival"], "connector arrival")
            if departure < current_time or arrival > latest:
                continue
            nxt = str(edge.get("to_node"))
            next_ids = edge_ids + [str(edge.get("edge_id"))]
            next_modes = modes + [str(edge.get("mode"))]
            if nxt == end:
                return next_ids, next_modes
            if nxt not in best_arrival or arrival < best_arrival[nxt]:
                best_arrival[nxt] = arrival
                queue.append((nxt, next_ids, next_modes, arrival))
    return None


def _observed_or_surface(edge: dict[str, Any]) -> bool:
    if edge.get("status") in {"unknown", "hypothesis", "unverified", "no_result"}:
        return False
    if not all(isinstance(edge.get(key), str) and edge[key] for key in ("edge_id", "from_node", "to_node", "mode")):
        return False
    try:
        return _parse_time(edge.get("arrival"), "arrival") > _parse_time(edge.get("departure"), "departure")
    except SchedulerError:
        return False


def _bucket(day: datetime, contract: dict[str, Any], bucket_days: int) -> str:
    delta = max(0, (day.date() - contract["hard_start"].date()).days)
    start = contract["hard_start"].date() + timedelta(days=(delta // bucket_days) * bucket_days)
    return start.isoformat()


def _anchor_window(anchor: datetime, contract: dict[str, Any], date_policy: dict[str, Any]) -> dict[str, str]:
    if contract["mode"] == "exact_dates":
        start, end = contract["hard_start"], contract["hard_end"]
    else:
        start = max(contract["search_start"], anchor - timedelta(days=date_policy["anchor_window_days_before"]))
        end = min(contract["search_end"], anchor + timedelta(days=date_policy["anchor_window_days_after"]))
    return {"start": _iso(start), "end": _iso(end)}


def _date_search(
    contract: dict[str, Any],
    policy: dict[str, Any],
    anchor_times: list[datetime],
    role: str,
) -> dict[str, Any]:
    date_policy = policy["date_search"]
    anchors = sorted(set(time.date().isoformat() for time in anchor_times))
    if contract["mode"] == "exact_dates":
        method = "exact_date_set"
        target = {"dates": contract["exact_dates"]}
    elif contract["mode"] == "bounded_window":
        method = "bounded_flexible_grid"
        target = {
            "window": {
                "start": _iso(contract["search_start"]),
                "end": _iso(contract["search_end"]),
            }
        }
    else:
        method = "flexible_calendar"
        target = {
            "calendar_window": {
                "start": _iso(contract["hard_start"]),
                "end": _iso(contract["hard_end"]),
            }
        }
    result = {
        "mode": contract["mode"],
        "method": method,
        "frontier_role": role,
        "hard_window": {"start": _iso(contract["hard_start"]), "end": _iso(contract["hard_end"])},
        "target": target,
        "observed_anchor_dates": anchors,
        "return_policy": {
            "selection": "price_time_pareto_by_date_bucket",
            "pareto_dimensions": date_policy["pareto_dimensions"],
            "max_dates": date_policy["max_pareto_dates_per_corridor_direction"],
            "do_not_return_only_global_cheapest_date": True,
        },
    }
    if anchor_times:
        result["anchor_verification_window"] = _anchor_window(
            min(anchor_times), contract, date_policy
        )
    return result


def _request_window(contract: dict[str, Any]) -> dict[str, str]:
    if contract["mode"] == "bounded_window":
        start, end = contract["search_start"], contract["search_end"]
    else:
        start, end = contract["hard_start"], contract["hard_end"]
    return {"start": _iso(start), "end": _iso(end)}


def _slug(parts: Iterable[str]) -> str:
    readable = "-".join(str(part).lower().replace("_", "-") for part in parts)
    readable = re.sub(r"[^a-z0-9-]+", "-", readable)
    readable = re.sub(r"-+", "-", readable).strip("-")
    digest = hashlib.sha256(readable.encode("utf-8")).hexdigest()[:8]
    return f"corridor-{readable[:54].strip('-')}-{digest}"


def _candidate_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("priority", 100),
        item["corridor_context"]["corridor_id"],
        item["request_id"],
    )


def _attach_baseline(
    request: dict[str, Any],
    index: int,
    contract: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    item = copy.deepcopy(request)
    item.setdefault("request_id", f"base-request-{index + 1}")
    item.setdefault("request_type", "conventional_reference")
    item["date_window"] = {"start": _iso(contract["hard_start"]), "end": _iso(contract["hard_end"])}
    request_type = str(item["request_type"])
    if request_type == "conventional_reference":
        corridor_id = "conventional-baseline"
        corridor_kind = "conventional_baseline"
        geographic_cell = "BASELINE"
        route_shape = "provider_complete_trip"
        frontier_role = "complete_trip_baseline"
        priority = 0
    elif request_type == "origin_market_scan":
        corridor_id = "market-discovery-origin"
        corridor_kind = "origin_market_discovery"
        geographic_cell = "ORIGIN_MARKET"
        route_shape = "origin_pool_to_open_destination"
        frontier_role = "origin_market_discovery"
        priority = 10 + index
    elif request_type == "destination_market_scan":
        corridor_id = "market-discovery-destination"
        corridor_kind = "destination_market_discovery"
        geographic_cell = "DESTINATION_MARKET"
        route_shape = "open_origin_to_destination_pool"
        frontier_role = "destination_market_discovery"
        priority = 10 + index
    else:
        corridor_id = f"seed-{request_type}-{index + 1}"
        corridor_kind = "seed_research"
        geographic_cell = "SEED_RESEARCH"
        route_shape = request_type
        frontier_role = "seed_research"
        priority = 10 + index
    item["corridor_context"] = {
        "corridor_id": corridor_id,
        "corridor_kind": corridor_kind,
        "geographic_cell": geographic_cell,
        "route_shape": route_shape,
        "independent_temporal_frontier": True,
    }
    item["date_search"] = _date_search(contract, policy, [], frontier_role)
    item["priority"] = priority
    return item


def _form_corridors(
    profile: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    contract: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    origins = set(_pool_nodes(profile, "origin"))
    destinations = set(_pool_nodes(profile, "destination"))
    forward = [edge for edge in edges if _observed_anchor(edge) and edge["from_node"] in origins and edge["to_node"] not in destinations]
    backward = [edge for edge in edges if _observed_anchor(edge) and edge["to_node"] in destinations and edge["from_node"] not in origins]
    max_gap = policy["corridor_discovery"]["max_days_between_anchors"]
    pair_limit = policy["corridor_discovery"]["max_anchor_pairs"]
    raw: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for left in forward:
        left_arrival = _parse_time(left["arrival"], "forward arrival")
        for right in backward:
            right_departure = _parse_time(right["departure"], "backward departure")
            gap = right_departure - left_arrival
            if gap.total_seconds() < 0 or gap > timedelta(days=max_gap):
                rejected.append({
                    "forward_anchor_edge_id": left["edge_id"],
                    "backward_anchor_edge_id": right["edge_id"],
                    "reason": "anchor_windows_do_not_overlap",
                })
                continue
            known = ([], []) if left["to_node"] == right["from_node"] else None
            if known is None and policy["corridor_discovery"]["include_observed_connector_paths"]:
                known = _known_path(
                    left["to_node"],
                    right["from_node"],
                    edges,
                    {left["edge_id"], right["edge_id"]},
                    left_arrival,
                    right_departure,
                )
            if known is None:
                connector_status, connector_edges, connector_modes = "unknown_requires_research", [], ["research"]
            else:
                connector_status, connector_edges, connector_modes = "observed", known[0], known[1]
            forward_cell = _cell(left["to_node"], nodes)
            backward_cell = _cell(right["from_node"], nodes)
            geography = f"{forward_cell}->{backward_cell}"
            middle_shape = "+".join(connector_modes) if connector_modes else "same_node"
            route_shape = f"flight>{middle_shape}>flight"
            # Two anchor pairs that share the same geography/shape may have
            # different prices and cheap dates.  Keep them as distinct temporal
            # hypotheses so one observed date surface cannot overwrite another.
            corridor_id = _slug(
                (geography, route_shape, left["edge_id"], right["edge_id"])
            )
            raw.append({
                "corridor_id": corridor_id,
                "corridor_kind": "observed_bidirectional_anchor",
                "geographic_cell": geography,
                "route_shape": route_shape,
                "forward_anchor": left,
                "backward_anchor": right,
                "connector_status": connector_status,
                "connector_edge_ids": connector_edges,
                "optimistic_anchor_cost": _edge_cost(left) + _edge_cost(right),
                "temporal_gap_minutes": int(gap.total_seconds() // 60),
            })
    raw.sort(key=lambda item: (item["optimistic_anchor_cost"], item["temporal_gap_minutes"], item["corridor_id"], item["forward_anchor"]["edge_id"], item["backward_anchor"]["edge_id"]))
    return raw, rejected + [
        {
            "forward_anchor_edge_id": item["forward_anchor"]["edge_id"],
            "backward_anchor_edge_id": item["backward_anchor"]["edge_id"],
            "reason": "beyond_script_recommended_max_anchor_pairs",
            "advisory_only": True,
        }
        for item in raw[pair_limit:]
    ]


def _corridor_requests(
    corridor: dict[str, Any],
    contract: dict[str, Any],
    policy: dict[str, Any],
    allowed_modes: list[str],
) -> list[dict[str, Any]]:
    left = corridor["forward_anchor"]
    right = corridor["backward_anchor"]
    pair_tag = hashlib.sha256(
        f"{left['edge_id']}|{right['edge_id']}".encode("utf-8")
    ).hexdigest()[:8]
    left_departure = _parse_time(left["departure"], "forward departure")
    right_departure = _parse_time(right["departure"], "backward departure")
    context = {
        "corridor_id": corridor["corridor_id"],
        "corridor_kind": corridor["corridor_kind"],
        "geographic_cell": corridor["geographic_cell"],
        "route_shape": corridor["route_shape"],
        "independent_temporal_frontier": True,
        "forward_anchor_edge_ids": [left["edge_id"]],
        "backward_anchor_edge_ids": [right["edge_id"]],
        "connector_status": corridor["connector_status"],
    }
    requests = [
        {
            "request_id": f"{corridor['corridor_id']}-{pair_tag}-forward-calendar",
            "request_type": "corridor_calendar_scan",
            "direction": "depart_after",
            "origins": [left["from_node"]],
            "destinations": [left["to_node"]],
            "date_window": _request_window(contract),
            "suggested_modes": ["flight"],
            "provider_kinds": ["flight_calendar_or_offer"],
            "corridor_context": copy.deepcopy(context),
            "date_search": _date_search(contract, policy, [left_departure], "forward_anchor"),
            "priority": 20,
        },
        {
            "request_id": f"{corridor['corridor_id']}-{pair_tag}-backward-calendar",
            "request_type": "corridor_calendar_scan",
            "direction": "arrive_by",
            "origins": [right["from_node"]],
            "destinations": [right["to_node"]],
            "date_window": _request_window(contract),
            "suggested_modes": ["flight"],
            "provider_kinds": ["flight_calendar_or_offer"],
            "corridor_context": copy.deepcopy(context),
            "date_search": _date_search(contract, policy, [right_departure], "backward_anchor"),
            "priority": 21,
        },
    ]
    if corridor["connector_status"] == "unknown_requires_research" and policy["corridor_discovery"]["unknown_connector_research"]:
        requests.append({
            "request_id": f"{corridor['corridor_id']}-{pair_tag}-connector-research",
            "request_type": "corridor_connector_research",
            "direction": "join",
            "from_node": left["to_node"],
            "to_node": right["from_node"],
            "earliest_departure": _iso(_parse_time(left["arrival"], "forward arrival")),
            "latest_arrival": _iso(right_departure),
            "allowed_modes": copy.deepcopy(allowed_modes),
            "provider_kinds": ["targeted_browser_research", "structured_transit"],
            "corridor_context": copy.deepcopy(context),
            "date_search": _date_search(contract, policy, [left_departure, right_departure], "unknown_connector"),
            "research_only": True,
            "admission_rule": "unknown_connector_must_return_observed_evidence_before_route_use",
            "priority": 22,
        })
    return requests


def _quota_dimensions(request: dict[str, Any], contract: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    context = request["corridor_context"]
    dates = request["date_search"].get("observed_anchor_dates", [])
    if dates:
        day = datetime.fromisoformat(dates[0]).replace(tzinfo=timezone.utc)
        date_bucket = _bucket(day, contract, policy["date_search"]["calendar_bucket_days"])
    else:
        date_bucket = "BASELINE"
    return {
        "corridor": context["corridor_id"],
        "geographic_cell": context["geographic_cell"],
        "route_shape": context["route_shape"],
        "date_bucket": date_bucket,
    }


def _admit(
    request: dict[str, Any],
    usage: dict[str, dict[str, int]],
    contract: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str | None]:
    dimensions = _quota_dimensions(request, contract, policy)
    limits = {
        "corridor": policy["quotas"]["per_corridor"],
        "geographic_cell": policy["quotas"]["per_geographic_cell"],
        "route_shape": policy["quotas"]["per_route_shape"],
        "date_bucket": policy["quotas"]["per_date_bucket"],
    }
    for dimension, value in dimensions.items():
        if usage[dimension][value] >= limits[dimension]:
            return False, f"per_{dimension}_quota"
    for dimension, value in dimensions.items():
        usage[dimension][value] += 1
    request["quota_context"] = dimensions
    return True, None


def schedule_temporal_corridors(
    profile: dict[str, Any],
    nodes: Any,
    edges: Any,
    base_requests: list[dict[str, Any]] | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded browser/provider requests and auditable coverage metadata."""
    if not isinstance(profile, dict):
        raise SchedulerError("profile must be an object")
    effective = _merge_policy(policy)
    contract = _date_contract(profile, effective)
    node_map = _node_index(nodes)
    observed_edges = _items(edges, "edges")
    if base_requests is None:
        base_requests = []
    if not isinstance(base_requests, list) or not all(isinstance(item, dict) for item in base_requests):
        raise SchedulerError("base_requests must be an array of objects")

    baseline = [_attach_baseline(item, index, contract, effective) for index, item in enumerate(base_requests)]
    corridors, discovery_pruned = _form_corridors(profile, node_map, observed_edges, contract, effective)
    allowed_modes = profile.get("allowed_modes")
    if not isinstance(allowed_modes, list) or not allowed_modes:
        allowed_modes = ["flight", "rail", "bus", "coach", "ferry", "local_transit"]
    exploration = [
        request
        for corridor in corridors
        for request in _corridor_requests(corridor, contract, effective, allowed_modes)
    ]

    limit = effective["query_budget"]
    selected: list[dict[str, Any]] = []
    pruned = copy.deepcopy(discovery_pruned)
    usage: dict[str, dict[str, int]] = {
        "corridor": defaultdict(int),
        "geographic_cell": defaultdict(int),
        "route_shape": defaultdict(int),
        "date_bucket": defaultdict(int),
    }

    baseline_reserved = min(effective["quotas"]["conventional_baseline_reserved"], len(baseline), limit)
    for request in sorted(baseline, key=_candidate_key)[:baseline_reserved]:
        admitted, reason = _admit(request, usage, contract, effective)
        if admitted:
            selected.append(request)
        else:
            pruned.append({"request_id": request["request_id"], "reason": reason})

    exploration_reserved = min(effective["quotas"]["exploration_reserved"], len(exploration), max(0, limit - len(selected)))
    reserved_ids: set[str] = set()
    for request in sorted(exploration, key=_candidate_key):
        if len(reserved_ids) >= exploration_reserved or len(selected) >= limit:
            break
        admitted, reason = _admit(request, usage, contract, effective)
        if admitted:
            selected.append(request)
            reserved_ids.add(request["request_id"])
        else:
            pruned.append({"request_id": request["request_id"], "reason": reason})

    already = {request["request_id"] for request in selected}
    remaining = sorted([*baseline, *exploration], key=_candidate_key)
    for request in remaining:
        if request["request_id"] in already:
            continue
        if len(selected) >= limit:
            pruned.append({"request_id": request["request_id"], "reason": "query_budget_exhausted"})
            continue
        admitted, reason = _admit(request, usage, contract, effective)
        if admitted:
            selected.append(request)
            already.add(request["request_id"])
        else:
            pruned.append({"request_id": request["request_id"], "reason": reason})

    selected.sort(key=_candidate_key)
    selected_ids = {request["request_id"] for request in selected}
    prune_reason_by_id = {
        str(item.get("request_id")): item.get("reason")
        for item in pruned
        if item.get("request_id")
    }
    candidates: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for request in sorted([*baseline, *exploration], key=_candidate_key):
        if request["request_id"] in candidate_ids:
            continue
        candidate_ids.add(request["request_id"])
        item = copy.deepcopy(request)
        item["selection_advice"] = {
            "script_recommended": request["request_id"] in selected_ids,
            "reason": (
                "selected_by_temporal_scheduler"
                if request["request_id"] in selected_ids
                else prune_reason_by_id.get(
                    request["request_id"], "outside_script_recommended_subset"
                )
            ),
            "binding": False,
        }
        candidates.append(item)
    corridor_summary = []
    selected_ids = {request["request_id"] for request in selected}
    for corridor in corridors:
        planned = _corridor_requests(corridor, contract, effective, allowed_modes)
        corridor_summary.append({
            "corridor_id": corridor["corridor_id"],
            "geographic_cell": corridor["geographic_cell"],
            "route_shape": corridor["route_shape"],
            "forward_anchor_edge_ids": [corridor["forward_anchor"]["edge_id"]],
            "backward_anchor_edge_ids": [corridor["backward_anchor"]["edge_id"]],
            "forward_anchor_window": planned[0]["date_search"]["anchor_verification_window"],
            "backward_anchor_window": planned[1]["date_search"]["anchor_verification_window"],
            "connector_status": corridor["connector_status"],
            "connector_edge_ids": corridor["connector_edge_ids"],
            "optimistic_anchor_cost": corridor["optimistic_anchor_cost"],
            "scheduled_request_ids": [item["request_id"] for item in planned if item["request_id"] in selected_ids],
        })

    quota_usage = {
        dimension: dict(sorted(values.items())) for dimension, values in usage.items()
    }
    calendar_tasks = sum(request["request_type"] == "corridor_calendar_scan" for request in selected)
    return {
        "scheduler_version": VERSION,
        "policy_id": effective["policy_id"],
        "requests": selected,
        "candidates": candidates,
        "corridors": corridor_summary,
        "coverage": {
            "date_intent": {
                "mode": contract["mode"],
                "hard_window": {"start": _iso(contract["hard_start"]), "end": _iso(contract["hard_end"])},
                "exact_departure_dates": contract["exact_dates"],
                "daily_brute_force_queries": 0,
                "calendar_or_grid_requests": calendar_tasks,
            },
            "observed_anchor_edges": {
                "forward": sorted({item["forward_anchor_edge_ids"][0] for item in corridor_summary}),
                "backward": sorted({item["backward_anchor_edge_ids"][0] for item in corridor_summary}),
            },
            "corridors_discovered": len(corridor_summary),
            "corridors_scheduled": sum(bool(item["scheduled_request_ids"]) for item in corridor_summary),
            "unknown_connector_research_requests": sum(request["request_type"] == "corridor_connector_research" for request in selected),
            "conventional_baseline_preserved": any(request["corridor_context"]["corridor_kind"] == "conventional_baseline" for request in selected),
            "quota_usage": quota_usage,
        },
        "budget": {
            "limit": limit,
            "scheduled": len(selected),
            "remaining": limit - len(selected),
            "conventional_baseline_reserved": baseline_reserved,
            "exploration_reserved": exploration_reserved,
            "exhausted": len(selected) >= limit,
        },
        "pruned": pruned,
    }


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Schedule bounded temporal corridor requests")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--nodes", required=True)
    parser.add_argument("--edges", required=True)
    parser.add_argument("--base-requests", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = schedule_temporal_corridors(
        _read(args.profile),
        _read(args.nodes),
        _read(args.edges),
        _read(args.base_requests),
        _read(args.policy) if args.policy else None,
    )
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
