"""Validate AI-owned regional gateway scope proposals.

This module deliberately contains no city-to-airport dictionary.  The AI owns
semantic enumeration; deterministic code owns boundedness, accounting, and
region-pool fare-screen compilation.
"""

from __future__ import annotations

import copy
from typing import Any


VERSION = "1.0"
REGION_ROLES = {
    "primary_airport",
    "secondary_airport",
    "cross_border_gateway",
    "surface_connected_gateway",
    "specialized_gateway",
}
class AIExplorationError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AIExplorationError(f"{field} must be a non-empty string")
    return value.strip()


def _node(raw: Any, field: str, *, airport_only: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AIExplorationError(f"{field} must be an object")
    result = copy.deepcopy(raw)
    for key in ("node_id", "node_type", "location", "country_or_region", "timezone"):
        result[key] = _text(raw.get(key), f"{field}.{key}")
    if airport_only and result["node_type"] != "airport":
        raise AIExplorationError(f"{field}.node_type must be airport")
    country_code = raw.get("country_code")
    if country_code is not None:
        result["country_code"] = _text(country_code, f"{field}.country_code").upper()
    utc_offset = raw.get("utc_offset")
    if utc_offset is not None:
        result["utc_offset"] = _text(utc_offset, f"{field}.utc_offset")
    return result

def initial_region_scopes(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    intent = scenario["intent"]
    node_map = {
        str(node["node_id"]): copy.deepcopy(node)
        for node in scenario.get("nodes", [])
        if isinstance(node, dict) and node.get("node_id")
    }
    result: dict[str, dict[str, Any]] = {}
    for side in ("origin", "destination"):
        pool = intent[f"{side}_pool"]
        anchor_ids = list(intent[f"{side}_anchor_nodes"])
        exact_airports = [
            node_map[node_id]
            for node_id in anchor_ids
            if node_id in node_map and node_map[node_id].get("node_type") == "airport"
        ]
        max_candidates = int(pool.get("expansion", {}).get("max_discovered_nodes", 12))
        if pool.get("policy") == "exact" and exact_airports:
            result[side] = {
                "scope_version": VERSION,
                "side": side,
                "status": "not_required",
                "decision_owner": "user_exact_anchor",
                "candidate_nodes": exact_airports,
                "candidate_records": [
                    {
                        "candidate_id": f"exact-{side}-{node['node_id']}",
                        "node": copy.deepcopy(node),
                        "roles": ["primary_airport"],
                        "inclusion_reason": "The user supplied this exact airport anchor.",
                    }
                    for node in exact_airports
                ],
                "excluded_seed_nodes": [],
                "coverage": {str(node["node_id"]): "exact_anchor" for node in exact_airports},
                "max_candidates": max_candidates,
                "proposal_history": [],
            }
        else:
            result[side] = {
                "scope_version": VERSION,
                "side": side,
                "status": "awaiting_ai",
                "decision_owner": "ai",
                "candidate_nodes": [],
                "candidate_records": [],
                "excluded_seed_nodes": [],
                "coverage": {},
                "max_candidates": max_candidates,
                "proposal_history": [],
            }
    return result
def region_scope_task_spec(side: str, scenario: dict[str, Any]) -> dict[str, Any]:
    intent = scenario["intent"]
    pool = intent[f"{side}_pool"]
    node_map = {
        str(node["node_id"]): copy.deepcopy(node)
        for node in scenario.get("nodes", [])
        if isinstance(node, dict) and node.get("node_id")
    }
    seed_nodes = [
        node_map[node_id]
        for node_id in pool.get("seed_gateway_node_ids", [])
        if node_id in node_map
    ]
    return {
        "task_id": f"task-region-scope-{side}-g1",
        "research_kind": "region_scope_definition",
        "question": (
            f"Which airports materially belong to or can practically serve the declared {side} "
            "region under its ground-access constraints? Check core, nearby-city, "
            "surface-connected, cross-border, and specialized airports in this one bounded "
            "pass; do not treat supplied examples as an exhaustive airport list."
        ),
        "query_shape": {
            "trip_type": "region_scope_definition",
            "side": side,
            "region_id": pool["region_id"],
            "region_label": pool["label"],
            "anchor_nodes": [node_map[node_id] for node_id in intent[f"{side}_anchor_nodes"]],
            "country_codes": list(pool.get("country_codes", [])),
            "seed_gateway_hints": seed_nodes,
            "max_candidates": int(pool.get("expansion", {}).get("max_discovered_nodes", 12)),
            "max_access_time_minutes": pool["max_access_time_minutes"],
            "max_egress_time_minutes": pool["max_egress_time_minutes"],
            "max_ground_cost": copy.deepcopy(pool["max_ground_cost"]),
            "allowed_ground_modes": list(pool["allowed_ground_modes"]),
            "decision_rule": (
                "AI proposes a bounded representative airport scope. Every seed hint must be "
                "included or explicitly excluded with a reason, and the proposal must account "
                "for plausible non-core gateways. Production code contains no city-to-airport "
                "dictionary."
            ),
        },
    }


def normalize_region_scope_proposal(
    raw: Any, scenario: dict[str, Any], expected_side: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("proposal_version") != VERSION:
        raise AIExplorationError(f"region scope proposal_version must be {VERSION}")
    side = raw.get("side")
    if side != expected_side:
        raise AIExplorationError("region scope proposal side does not match active task")
    pool = scenario["intent"][f"{side}_pool"]
    if raw.get("region_id") != pool["region_id"]:
        raise AIExplorationError("region scope proposal region_id does not match Session")
    candidates_raw = raw.get("candidates")
    max_candidates = int(pool.get("expansion", {}).get("max_discovered_nodes", 12))
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise AIExplorationError("region scope proposal requires at least one candidate")
    if len(candidates_raw) > max_candidates:
        raise AIExplorationError(
            f"region scope proposal exceeds max_candidates={max_candidates}"
        )
    candidates = []
    seen_nodes: set[str] = set()
    for index, item in enumerate(candidates_raw):
        field = f"region_scope.candidates[{index}]"
        if not isinstance(item, dict):
            raise AIExplorationError(f"{field} must be an object")
        node = _node(item.get("node"), f"{field}.node", airport_only=True)
        node_id = node["node_id"]
        if node_id in seen_nodes:
            raise AIExplorationError(f"duplicate region gateway node {node_id}")
        seen_nodes.add(node_id)
        roles = item.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or not all(isinstance(role, str) and role in REGION_ROLES for role in roles)
        ):
            raise AIExplorationError(f"{field}.roles is invalid")
        candidates.append(
            {
                "candidate_id": _text(
                    item.get("candidate_id"), f"{field}.candidate_id"
                ),
                "node": node,
                "roles": list(dict.fromkeys(roles)),
                "inclusion_reason": _text(
                    item.get("inclusion_reason"), f"{field}.inclusion_reason"
                ),
            }
        )
    exclusions_raw = raw.get("excluded_seed_nodes", [])
    if not isinstance(exclusions_raw, list):
        raise AIExplorationError("excluded_seed_nodes must be an array")
    exclusions = []
    excluded_ids: set[str] = set()
    for index, item in enumerate(exclusions_raw):
        field = f"region_scope.excluded_seed_nodes[{index}]"
        if not isinstance(item, dict):
            raise AIExplorationError(f"{field} must be an object")
        node_id = _text(item.get("node_id"), f"{field}.node_id")
        if node_id in excluded_ids:
            raise AIExplorationError(f"duplicate excluded seed {node_id}")
        excluded_ids.add(node_id)
        exclusions.append(
            {
                "node_id": node_id,
                "reason": _text(item.get("reason"), f"{field}.reason"),
            }
        )
    seed_ids = set(pool.get("seed_gateway_node_ids", []))
    unaccounted = sorted(seed_ids - seen_nodes - excluded_ids)
    unknown_exclusions = sorted(excluded_ids - seed_ids)
    if unaccounted:
        raise AIExplorationError(f"seed gateway hints are unaccounted: {unaccounted}")
    if unknown_exclusions:
        raise AIExplorationError(
            f"excluded_seed_nodes contains non-seed nodes: {unknown_exclusions}"
        )
    return {
        "scope_version": VERSION,
        "side": side,
        "status": "defined",
        "decision_owner": "ai",
        "region_id": pool["region_id"],
        "region_label": pool["label"],
        "candidate_nodes": [copy.deepcopy(item["node"]) for item in candidates],
        "candidate_records": candidates,
        "excluded_seed_nodes": exclusions,
        "coverage": {
            item["node"]["node_id"]: "scope_defined" for item in candidates
        },
        "max_candidates": max_candidates,
        "scope_reason": _text(raw.get("scope_reason"), "region_scope.scope_reason"),
    }


def region_pool_fare_screen_spec(
    origin_scope: dict[str, Any],
    destination_scope: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    intent = scenario["intent"]
    origin_ids = [
        str(record["node"]["node_id"])
        for record in origin_scope.get("candidate_records", [])
    ]
    destination_ids = [
        str(record["node"]["node_id"])
        for record in destination_scope.get("candidate_records", [])
    ]
    submitted_scope = {
        "origin_gateway_node_ids": origin_ids,
        "destination_gateway_node_ids": destination_ids,
        "outbound_departure_window": copy.deepcopy(
            intent["outbound_departure_window"]
        ),
        "return_departure_window": copy.deepcopy(
            intent["return_departure_window"]
        ),
        "minimum_stay_nights": intent.get("minimum_stay_nights"),
        "maximum_stay_nights": intent.get("maximum_stay_nights"),
    }
    return {
        "task_id": "task-region-pool-fare-screen-g1",
        "research_kind": "region_pool_fare_screen",
        "question": (
            "Find the lowest observed round-trip totals across the complete admitted "
            "origin and destination airport pools, full departure windows, and stay range."
        ),
        "query_shape": {
            "trip_type": "region_pool_fare_screen",
            **copy.deepcopy(submitted_scope),
            "objective": "lowest_total_price",
            "required_price_selection_method": True,
            "submitted_scope": submitted_scope,
        },
    }
# End of regional scope implementation.
