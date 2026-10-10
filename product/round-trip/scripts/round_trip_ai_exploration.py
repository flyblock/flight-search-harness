"""Validate AI-owned regional scope and structural corridor proposals.

This module deliberately contains no city-to-airport dictionary.  The AI owns
semantic enumeration; deterministic code owns boundedness, accounting, task
compilation, and the facts required before a stop can become final.
"""

from __future__ import annotations

import copy
import re
from typing import Any


VERSION = "1.0"
REGION_ROLES = {
    "primary_airport",
    "secondary_airport",
    "cross_border_gateway",
    "surface_connected_gateway",
    "specialized_gateway",
}
REGION_SCOPE_REVIEW_CATEGORIES = {
    "core_city_code_airports",
    "nearby_city_airports",
    "surface_connected_airports",
    "cross_border_gateways",
    "specialized_airports",
}
STRUCTURAL_SHAPES = {
    "regional_open_jaw",
    "nested_round_trips",
    "mixed_atomic_portfolio",
    "custom_complete_journey",
}
OFFER_SCOPES = {"joint_round_trip", "one_way", "atomic_itinerary", "surface"}


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


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return token or "gateway"


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
            "region under its ground-access constraints? Use semantic geographic judgment; "
            "do not treat supplied examples as an exhaustive airport list."
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
            "max_access_time_minutes": int(pool["max_access_time_minutes"]),
            "max_egress_time_minutes": int(pool["max_egress_time_minutes"]),
            "max_ground_cost": copy.deepcopy(pool["max_ground_cost"]),
            "allowed_ground_modes": list(pool["allowed_ground_modes"]),
            "decision_rule": (
                "AI proposes a bounded representative airport scope. Every seed hint must be "
                "included or explicitly excluded with a reason. Production code contains no "
                "city-to-airport dictionary."
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
        "status": "defined_pending_challenge",
        "decision_owner": "ai",
        "region_id": pool["region_id"],
        "region_label": pool["label"],
        "candidate_nodes": [copy.deepcopy(item["node"]) for item in candidates],
        "candidate_records": candidates,
        "excluded_seed_nodes": exclusions,
        "coverage": {item["node"]["node_id"]: "pending" for item in candidates},
        "max_candidates": max_candidates,
        "scope_reason": _text(raw.get("scope_reason"), "region_scope.scope_reason"),
    }


def region_scope_challenge_task_spec(
    side: str, scenario: dict[str, Any], scope: dict[str, Any]
) -> dict[str, Any]:
    intent = scenario["intent"]
    pool = intent[f"{side}_pool"]
    return {
        "task_id": f"task-region-scope-challenge-{side}-g1",
        "research_kind": "region_scope_challenge",
        "question": (
            f"Challenge the proposed {side} airport scope. Look beyond core city codes and "
            "seed anchoring for nearby-city, surface-connected, cross-border, or specialized "
            "airports that can serve the user's named region within the declared constraints."
        ),
        "query_shape": {
            "trip_type": "region_scope_challenge",
            "side": side,
            "region_id": pool["region_id"],
            "region_label": pool["label"],
            "current_candidate_records": copy.deepcopy(
                scope.get("candidate_records", [])
            ),
            "required_review_categories": sorted(REGION_SCOPE_REVIEW_CATEGORIES),
            "remaining_candidate_capacity": max(
                int(scope.get("max_candidates", 0))
                - len(scope.get("candidate_records", [])),
                0,
            ),
            "anchor_node_ids": list(intent[f"{side}_anchor_nodes"]),
            "max_access_time_minutes": int(pool["max_access_time_minutes"]),
            "max_egress_time_minutes": int(pool["max_egress_time_minutes"]),
            "max_ground_cost": copy.deepcopy(pool["max_ground_cost"]),
            "allowed_ground_modes": list(pool["allowed_ground_modes"]),
            "decision_rule": (
                "This is an adversarial second pass. Do not restate the current city-code set. "
                "Add omitted airports when semantically plausible; an empty addition requires "
                "a concrete category-by-category explanation, not absence of an existing fare signal."
            ),
        },
    }


def normalize_region_scope_challenge(
    raw: Any,
    scenario: dict[str, Any],
    expected_side: str,
    current_scope: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("proposal_version") != VERSION:
        raise AIExplorationError(
            f"region scope challenge proposal_version must be {VERSION}"
        )
    if raw.get("side") != expected_side:
        raise AIExplorationError(
            "region scope challenge side does not match active task"
        )
    pool = scenario["intent"][f"{expected_side}_pool"]
    if raw.get("region_id") != pool["region_id"]:
        raise AIExplorationError(
            "region scope challenge region_id does not match Session"
        )
    reviewed = raw.get("reviewed_categories")
    if not isinstance(reviewed, list) or set(reviewed) != REGION_SCOPE_REVIEW_CATEGORIES:
        raise AIExplorationError(
            "region scope challenge must review every required category"
        )
    additions_raw = raw.get("additional_candidates")
    if not isinstance(additions_raw, list):
        raise AIExplorationError("additional_candidates must be an array")
    existing_ids = {
        str(item["node"]["node_id"])
        for item in current_scope.get("candidate_records", [])
    }
    remaining = max(
        int(current_scope.get("max_candidates", 0)) - len(existing_ids), 0
    )
    if len(additions_raw) > remaining:
        raise AIExplorationError(
            f"region scope challenge exceeds remaining_candidate_capacity={remaining}"
        )
    additions = []
    seen = set(existing_ids)
    for index, item in enumerate(additions_raw):
        field = f"region_scope_challenge.additional_candidates[{index}]"
        if not isinstance(item, dict):
            raise AIExplorationError(f"{field} must be an object")
        node = _node(item.get("node"), f"{field}.node", airport_only=True)
        node_id = node["node_id"]
        if node_id in seen:
            raise AIExplorationError(
                f"region scope challenge duplicates existing node {node_id}"
            )
        seen.add(node_id)
        roles = item.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or not all(isinstance(role, str) and role in REGION_ROLES for role in roles)
        ):
            raise AIExplorationError(f"{field}.roles is invalid")
        additions.append(
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
    no_reason = raw.get("no_additional_candidate_reason")
    if not additions:
        no_reason = _text(
            no_reason,
            "region_scope_challenge.no_additional_candidate_reason",
        )
    elif no_reason is not None:
        raise AIExplorationError(
            "no_additional_candidate_reason is allowed only when additions are empty"
        )
    return {
        "challenge_version": VERSION,
        "side": expected_side,
        "region_id": pool["region_id"],
        "reviewed_categories": sorted(REGION_SCOPE_REVIEW_CATEGORIES),
        "additional_candidates": additions,
        "no_additional_candidate_reason": no_reason,
        "challenge_reason": _text(
            raw.get("challenge_reason"),
            "region_scope_challenge.challenge_reason",
        ),
    }


def merge_region_scope_challenge(
    current_scope: dict[str, Any], challenge: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(current_scope)
    additions = copy.deepcopy(challenge.get("additional_candidates", []))
    result["candidate_records"].extend(additions)
    result["candidate_nodes"].extend(
        copy.deepcopy(item["node"]) for item in additions
    )
    for item in additions:
        result["coverage"][str(item["node"]["node_id"])] = "pending"
    result["status"] = "defined"
    result.setdefault("challenge_history", []).append(copy.deepcopy(challenge))
    return result


def gateway_task_specs(
    scope: dict[str, Any], scenario: dict[str, Any]
) -> list[dict[str, Any]]:
    side = scope["side"]
    intent = scenario["intent"]
    pool = intent[f"{side}_pool"]
    result = []
    for record in scope.get("candidate_records", []):
        node = record["node"]
        node_id = str(node["node_id"])
        result.append(
            {
                "task_id": f"task-gateway-{side}-{_safe_token(node_id)}-g1",
                "research_kind": "gateway_discovery",
                "question": (
                    f"Research whether {node_id} materially serves the declared {side} region "
                    "and capture a concrete access, connectivity, or dated fare signal."
                ),
                "query_shape": {
                    "trip_type": "gateway_candidate_research",
                    "side": side,
                    "required_candidate_node_id": node_id,
                    "candidate_node": copy.deepcopy(node),
                    "candidate_roles": list(record["roles"]),
                    "inclusion_reason": record["inclusion_reason"],
                    "anchor_node_ids": list(intent[f"{side}_anchor_nodes"]),
                    "pool": copy.deepcopy(pool),
                    "travel_windows": {
                        "outbound": intent["outbound_departure_window"],
                        "return": intent["return_departure_window"],
                    },
                    "date_flexibility": copy.deepcopy(intent["date_flexibility"]),
                },
            }
        )
    return result


def initial_structural_exploration() -> dict[str, Any]:
    return {
        "exploration_version": VERSION,
        "status": "waiting_for_incumbent",
        "decision_owner": "ai",
        "max_proposals": 3,
        "minimum_proposals": 0,
        "proposals": [],
        "dispositions": {},
        "no_material_challenger_reason": None,
        "proposal_history": [],
    }


def structural_minimum_proposals(state: dict[str, Any]) -> int:
    intent = state.get("scenario", {}).get("intent", {})
    regional = any(
        intent.get(f"{side}_pool", {}).get("policy") != "exact"
        for side in ("origin", "destination")
    )
    flexibility = intent.get("date_flexibility", {})
    modes = {
        str(item) for item in intent.get("allowed_modes", [])
    }
    meaningful_surface = bool(
        modes & {"rail", "bus", "coach", "ferry", "rideshare", "car"}
    )
    flexible = flexibility.get("mode", "fully_flexible") != "exact_dates"
    return 1 if regional and meaningful_surface and flexible else 0


def structural_task_spec(state: dict[str, Any]) -> dict[str, Any]:
    incumbent = state.get("result", {}).get("best_portfolio")
    minimum_proposals = structural_minimum_proposals(state)
    return {
        "task_id": "task-structural-corridor-g1",
        "research_kind": "structural_challenger_definition",
        "question": (
            "Propose a bounded set of materially different complete-journey structures that "
            "could challenge the current round-trip incumbent, including third-country flight "
            "gateways, surface corridors, nested atomic tickets, or open jaws when plausible. "
            "Do not anchor on any example and do not invent prices."
        ),
        "query_shape": {
            "trip_type": "structural_challenger_definition",
            "incumbent": copy.deepcopy(incumbent),
            "origin_gateway_node_ids": sorted(
                {
                    item["node"]["node_id"]
                    for item in state.get("gateway_candidates", {}).get("origin", [])
                    if item.get("evidence_status") in {"research_observed", "exact_anchor"}
                }
            ),
            "destination_gateway_node_ids": sorted(
                {
                    item["node"]["node_id"]
                    for item in state.get("gateway_candidates", {}).get("destination", [])
                    if item.get("evidence_status") in {"research_observed", "exact_anchor"}
                }
            ),
            "allowed_modes": list(state["scenario"]["intent"].get("allowed_modes", [])),
            "travel_windows": {
                "outbound": state["scenario"]["intent"]["outbound_departure_window"],
                "return": state["scenario"]["intent"]["return_departure_window"],
            },
            "max_proposals": int(
                state.get("structural_exploration", {}).get("max_proposals", 3)
            ),
            "minimum_proposals": minimum_proposals,
            "rule": (
                "AI owns semantic corridor discovery. Each proposal must describe a complete "
                "journey hypothesis through explicit atomic research task specs. When the "
                "published minimum is one, absence of prior price evidence cannot justify "
                "empty output; propose one bounded structure for verification or later dismissal."
            ),
        },
    }


def normalize_structural_proposal(
    raw: Any, state: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("proposal_version") != VERSION:
        raise AIExplorationError(
            f"structural proposal_version must be {VERSION}"
        )
    proposals_raw = raw.get("proposals")
    if not isinstance(proposals_raw, list):
        raise AIExplorationError("structural proposals must be an array")
    max_proposals = int(
        state.get("structural_exploration", {}).get("max_proposals", 3)
    )
    minimum_proposals = int(
        state.get("structural_exploration", {}).get(
            "minimum_proposals", structural_minimum_proposals(state)
        )
    )
    if len(proposals_raw) > max_proposals:
        raise AIExplorationError(
            f"structural proposals exceed max_proposals={max_proposals}"
        )
    if len(proposals_raw) < minimum_proposals:
        raise AIExplorationError(
            f"structural proposals require minimum_proposals={minimum_proposals}"
        )
    admitted = {
        str(item["node"]["node_id"])
        for side in ("origin", "destination")
        for item in state.get("gateway_candidates", {}).get(side, [])
        if item.get("evidence_status") in {"research_observed", "exact_anchor"}
    }
    result = []
    seen: set[str] = set()
    for index, item in enumerate(proposals_raw):
        field = f"structural.proposals[{index}]"
        if not isinstance(item, dict):
            raise AIExplorationError(f"{field} must be an object")
        proposal_id = _text(item.get("proposal_id"), f"{field}.proposal_id")
        if proposal_id in seen:
            raise AIExplorationError(f"duplicate structural proposal {proposal_id}")
        seen.add(proposal_id)
        route_shape = item.get("route_shape")
        if route_shape not in STRUCTURAL_SHAPES:
            raise AIExplorationError(f"{field}.route_shape is unsupported")
        origin_ids = [str(value) for value in item.get("origin_gateway_nodes", [])]
        destination_ids = [
            str(value) for value in item.get("destination_gateway_nodes", [])
        ]
        if (
            not origin_ids
            or not destination_ids
            or not set(origin_ids + destination_ids) <= admitted
        ):
            raise AIExplorationError(
                f"{field} must use observed origin/destination gateways"
            )
        intermediate_raw = item.get("intermediate_nodes", [])
        if not isinstance(intermediate_raw, list) or len(intermediate_raw) > 8:
            raise AIExplorationError(
                f"{field}.intermediate_nodes must contain at most 8 nodes"
            )
        intermediate_nodes = []
        intermediate_seen: set[str] = set()
        for node_index, node_raw in enumerate(intermediate_raw):
            node = _node(node_raw, f"{field}.intermediate_nodes[{node_index}]")
            if node["node_id"] in intermediate_seen:
                raise AIExplorationError(
                    f"duplicate intermediate node {node['node_id']}"
                )
            intermediate_seen.add(node["node_id"])
            intermediate_nodes.append(node)
        task_specs_raw = item.get("task_specs")
        if not isinstance(task_specs_raw, list) or not task_specs_raw:
            raise AIExplorationError(f"{field}.task_specs must be non-empty")
        task_specs = []
        task_ids: set[str] = set()
        for task_index, task in enumerate(task_specs_raw):
            task_field = f"{field}.task_specs[{task_index}]"
            if not isinstance(task, dict):
                raise AIExplorationError(f"{task_field} must be an object")
            task_id = _text(task.get("task_id"), f"{task_field}.task_id")
            if task_id in task_ids:
                raise AIExplorationError(f"duplicate structural task {task_id}")
            task_ids.add(task_id)
            journey_scope = task.get("journey_scope")
            if journey_scope not in OFFER_SCOPES:
                raise AIExplorationError(
                    f"{task_field}.journey_scope is unsupported"
                )
            query_shape = task.get("query_shape")
            if not isinstance(query_shape, dict):
                raise AIExplorationError(f"{task_field}.query_shape must be an object")
            normalized_task = copy.deepcopy(task)
            normalized_task["task_id"] = task_id
            normalized_task["journey_scope"] = journey_scope
            normalized_task["question"] = _text(
                task.get("question"), f"{task_field}.question"
            )
            normalized_task.setdefault("research_kind", "priced_offer")
            normalized_task.setdefault("required", True)
            task_specs.append(normalized_task)
        result.append(
            {
                "proposal_id": proposal_id,
                "hypothesis_id": f"hypothesis-structural-{_safe_token(proposal_id)}",
                "route_shape": route_shape,
                "origin_gateway_nodes": origin_ids,
                "destination_gateway_nodes": destination_ids,
                "intermediate_nodes": intermediate_nodes,
                "mechanism": _text(item.get("mechanism"), f"{field}.mechanism"),
                "reason": _text(item.get("reason"), f"{field}.reason"),
                "task_specs": task_specs,
            }
        )
    no_reason = raw.get("no_material_challenger_reason")
    if not result:
        no_reason = _text(
            no_reason,
            "structural.no_material_challenger_reason",
        )
    elif no_reason is not None:
        raise AIExplorationError(
            "no_material_challenger_reason is allowed only when proposals is empty"
        )
    return {
        "exploration_version": VERSION,
        "status": "proposed" if result else "complete_no_proposal",
        "decision_owner": "ai",
        "max_proposals": max_proposals,
        "minimum_proposals": minimum_proposals,
        "proposals": result,
        "dispositions": {item["proposal_id"]: "pending" for item in result},
        "no_material_challenger_reason": no_reason,
    }


def structural_hypothesis_specs(exploration: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for proposal in exploration.get("proposals", []):
        result.append(
            {
                "hypothesis_id": proposal["hypothesis_id"],
                "route_shape": proposal["route_shape"],
                "origin_gateway_nodes": list(proposal["origin_gateway_nodes"]),
                "destination_gateway_nodes": list(
                    proposal["destination_gateway_nodes"]
                ),
                "geographic_cell": "ai-structural-corridor",
                "advisory_basis": {
                    "evidence_backed": False,
                    "ai_proposed": True,
                    "proposal_id": proposal["proposal_id"],
                    "mechanism": proposal["mechanism"],
                    "reason": proposal["reason"],
                    "binding": False,
                },
                "research_sequence": [
                    "verify_each_atomic_offer",
                    "verify_surface_feasibility",
                    "solve_complete_portfolio",
                ],
                "temporal_strategy": "ai_led_date_frontier",
                "joint_baseline_reason": None,
                "task_specs": copy.deepcopy(proposal["task_specs"]),
            }
        )
    return result


def unresolved_structural_proposal_ids(state: dict[str, Any]) -> list[str]:
    exploration = state.get("structural_exploration", {})
    return sorted(
        proposal_id
        for proposal_id, status in exploration.get("dispositions", {}).items()
        if status == "pending"
    )
