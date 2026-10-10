"""Evidence-derived frontier archive and compact AI research view."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


VERSION = "1.0"


class FrontierExplorationError(ValueError):
    pass


def _time_bucket(minutes: float) -> str:
    if minutes <= 24 * 60:
        return "within_24h"
    if minutes <= 72 * 60:
        return "within_72h"
    if minutes <= 5 * 24 * 60:
        return "within_5d"
    return "over_5d"


def _mode_family(edge_ids: list[str], edge_map: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    modes = [str(edge_map[edge_id].get("mode")) for edge_id in edge_ids if edge_id in edge_map]
    unique = sorted(set(modes))
    has_air = "flight" in unique
    has_surface = any(mode != "flight" for mode in unique)
    if has_air and has_surface:
        family = "flight_led_multimodal"
    elif has_air:
        family = "flight_only"
    else:
        family = "surface_only"
    return family, unique


def _country_path(node_path: list[str], node_map: dict[str, dict[str, Any]]) -> list[str]:
    values = []
    for node_id in node_path:
        code = node_map.get(node_id, {}).get("country_code")
        if code and (not values or values[-1] != code):
            values.append(str(code))
    return values


def _frontier_from_label(
    label: dict[str, Any], node_map: dict[str, dict[str, Any]], edge_map: dict[str, dict[str, Any]], origin_nodes: set[str], destination_nodes: set[str]
) -> dict[str, Any] | None:
    edge_ids = [str(item) for item in label.get("edge_ids", [])]
    if not edge_ids:
        return None
    direction = str(label.get("direction"))
    node_id = str(label.get("node"))
    node_path = [str(item) for item in label.get("node_path", [])]
    mode_family, modes = _mode_family(edge_ids, edge_map)
    countries = _country_path(node_path, node_map)
    border_count = max(0, len(countries) - 1)
    complete = node_id in (destination_nodes if direction == "forward" else origin_nodes)
    descriptor = {
        "direction": direction,
        "geography": str(label.get("geography_bucket") or node_map.get(node_id, {}).get("country_code") or "unknown"),
        "mode_family": mode_family,
        "border_bucket": "2_plus" if border_count >= 2 else str(border_count),
        "time_bucket": _time_bucket(float(label.get("travel_minutes", 0))),
        "completeness": "complete" if complete else "partial",
    }
    cell_token = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode("utf-8")).hexdigest()[:14]
    frontier_token = hashlib.sha256(f"{direction}|{label.get('label_id')}".encode("utf-8")).hexdigest()[:14]
    return {
        "frontier_id": f"frontier-{frontier_token}",
        "label_id": label.get("label_id"),
        "direction": direction,
        "node_id": node_id,
        "node": copy.deepcopy(node_map.get(node_id)),
        "edge_ids": edge_ids,
        "node_path": node_path,
        "country_path": countries,
        "modes": modes,
        "mode_family": mode_family,
        "verified_cost": float(label.get("cost", 0)),
        "travel_minutes": float(label.get("travel_minutes", 0)),
        "departure": label.get("departure"),
        "arrival": label.get("arrival"),
        "border_crossing_lower_bound": border_count,
        "complete": complete,
        "quality_cell_id": f"cell-{cell_token}",
        "quality_descriptor": descriptor,
        "evidence_status": "derived_from_verified_frontier_labels",
    }


def build_archive(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise FrontierExplorationError("state must be an object")
    result = state.get("result", {})
    frontier_state = result.get("frontier_state", {}) if isinstance(result, dict) else {}
    node_map = {str(item.get("node_id")): item for item in state.get("nodes", []) if isinstance(item, dict) and item.get("node_id")}
    edge_map = {str(item.get("edge_id")): item for item in state.get("edges", []) if isinstance(item, dict) and item.get("edge_id")}
    profile = state.get("profile", {}) if isinstance(state.get("profile"), dict) else {}
    origins = set(str(item) for item in profile.get("origin_start_nodes", []))
    destinations = set(str(item) for item in profile.get("destination_end_nodes", []))
    frontiers = []
    for direction in ("forward", "backward"):
        labels = frontier_state.get(direction, []) if isinstance(frontier_state, dict) else []
        for label in labels if isinstance(labels, list) else []:
            if isinstance(label, dict):
                frontier = _frontier_from_label(label, node_map, edge_map, origins, destinations)
                if frontier is not None:
                    frontiers.append(frontier)
    frontiers.sort(key=lambda item: (item["complete"], item["verified_cost"], item["travel_minutes"], item["frontier_id"]))
    cells: dict[str, dict[str, Any]] = {}
    for frontier in frontiers:
        cell = cells.setdefault(
            frontier["quality_cell_id"],
            {
                "quality_cell_id": frontier["quality_cell_id"],
                "descriptor": copy.deepcopy(frontier["quality_descriptor"]),
                "member_frontier_ids": [],
                "elite_frontier_id": frontier["frontier_id"],
            },
        )
        cell["member_frontier_ids"].append(frontier["frontier_id"])
        elite = next(item for item in frontiers if item["frontier_id"] == cell["elite_frontier_id"])
        if (frontier["verified_cost"], frontier["travel_minutes"], frontier["frontier_id"]) < (elite["verified_cost"], elite["travel_minutes"], elite["frontier_id"]):
            cell["elite_frontier_id"] = frontier["frontier_id"]
    return {
        "frontier_archive_version": VERSION,
        "derivation": "verified_solver_frontier_labels_only",
        "frontier_count": len(frontiers),
        "quality_cell_count": len(cells),
        "frontiers": frontiers,
        "quality_cells": sorted(cells.values(), key=lambda item: item["quality_cell_id"]),
    }


def build_research_view(state: dict[str, Any], archive: dict[str, Any] | None = None) -> dict[str, Any]:
    archive = copy.deepcopy(archive or build_archive(state))
    routes = state.get("result", {}).get("routes", [])
    tasks = state.get("research_tasks", [])
    outcomes = state.get("hypothesis_outcomes", {}) if isinstance(state.get("hypothesis_outcomes"), dict) else {}
    baseline_open = any(
        isinstance(task, dict)
        and task.get("request_type") == "conventional_reference"
        and task.get("status") in {"candidate", "pending"}
        for task in tasks
    )
    baseline_terminal = outcomes.get("conventional-baseline", {}).get("status") in {"complete", "hard_dead", "deferred_low_value"}
    frontier_enabled = bool(routes) or (not baseline_open and baseline_terminal)
    elites = {cell["elite_frontier_id"] for cell in archive.get("quality_cells", [])}
    tasks_by_frontier: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        context = task.get("frontier_context")
        frontier_id = context.get("frontier_id") if isinstance(context, dict) else None
        if frontier_id:
            tasks_by_frontier.setdefault(str(frontier_id), []).append(task)
    actions = []
    if frontier_enabled:
        for frontier in archive.get("frontiers", []):
            if frontier.get("complete") or frontier.get("frontier_id") not in elites:
                continue
            prior_tasks = sorted(
                tasks_by_frontier.get(str(frontier["frontier_id"]), []),
                key=lambda item: (int(item.get("sequence", 0)), str(item.get("task_id"))),
            )
            prior_task = prior_tasks[0] if prior_tasks else None
            hypothesis_id = (
                str(prior_task.get("hypothesis_id"))
                if isinstance(prior_task, dict) and prior_task.get("hypothesis_id")
                else None
            )
            prior_outcome = outcomes.get(hypothesis_id) if hypothesis_id else None
            if prior_task and not (
                isinstance(prior_outcome, dict)
                and prior_outcome.get("status")
                in {"complete", "hard_dead", "deferred_low_value"}
            ):
                continue
            action_kind = "reopen_frontier" if prior_task else "explore_frontier"
            actions.append(
                {
                    "kind": action_kind,
                    "action_key": f"{action_kind}:{frontier['frontier_id']}",
                    "frontier_id": frontier["frontier_id"],
                    "task_id": prior_task.get("task_id") if prior_task else None,
                    "hypothesis_id": hypothesis_id,
                    "node_id": frontier["node_id"],
                    "direction": frontier["direction"],
                    "quality_cell_id": frontier["quality_cell_id"],
                    "verified_cost": frontier["verified_cost"],
                    "travel_minutes": frontier["travel_minutes"],
                    "modes_used": copy.deepcopy(frontier["modes"]),
                    "question": "From this evidence-backed frontier, discover one concrete onward movement or topology signal using an allowed mode. Do not assume a connection exists.",
                    "binding": False,
                }
            )
    actions.sort(key=lambda item: (item["verified_cost"], item["travel_minutes"], item["frontier_id"]))
    balanced_actions = []
    for direction in ("forward", "backward"):
        balanced_actions.extend(
            [
                item
                for item in actions
                if item.get("direction") == direction
            ][:4]
        )
    coverage = state.get("provider_coverage_audit", {})
    coverage_summary = {
        key: copy.deepcopy(coverage.get(key))
        for key in (
            "registry_last_verified",
            "static_source_count",
            "dynamic_rule_count",
            "disposition_counts",
            "task_status_counts",
        )
        if key in coverage
    }
    baseline_tasks = [
        {
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "attempt_count": int(task.get("attempt_count", 0)),
        }
        for task in tasks
        if isinstance(task, dict) and task.get("request_type") == "conventional_reference"
    ]
    return {
        "frontier_research_view_version": VERSION,
        "decision_owner": "ai",
        "complete_route_count": len(routes),
        "active_incumbent": copy.deepcopy(state.get("active_incumbent")),
        "lane_status": {
            "complete_trip_obligation_open": bool(not routes and baseline_open),
            "complete_trip_baseline_terminal": bool(baseline_terminal),
            "frontier_exploration_enabled": frontier_enabled,
            "rule": "Without a complete route, complete-trip baseline and Provider fallbacks precede partial frontier expansion. With an incumbent, frontier actions are structural challengers.",
        },
        "archive_summary": {
            "frontier_count": archive.get("frontier_count", 0),
            "quality_cell_count": archive.get("quality_cell_count", 0),
        },
        "complete_trip_baseline": {
            "tasks": baseline_tasks,
            "outcome": copy.deepcopy(outcomes.get("conventional-baseline")),
        },
        "source_coverage_summary": coverage_summary,
        "frontier_actions": balanced_actions,
        "unsearched_scope": "Any node, mode, Provider, date, or connector not represented by admitted Evidence remains unknown.",
    }


def frontier_by_id(archive: dict[str, Any], frontier_id: str) -> dict[str, Any]:
    match = next((item for item in archive.get("frontiers", []) if item.get("frontier_id") == frontier_id), None)
    if match is None:
        raise FrontierExplorationError(f"unknown frontier_id: {frontier_id}")
    return copy.deepcopy(match)


def compile_frontier_request(state: dict[str, Any], frontier_id: str) -> dict[str, Any]:
    archive = state.get("frontier_archive") if isinstance(state.get("frontier_archive"), dict) else build_archive(state)
    frontier = frontier_by_id(archive, frontier_id)
    profile = state.get("profile", {})
    allowed_modes = list(profile.get("allowed_modes", ["flight"]))
    incumbent = state.get("active_incumbent") or {}
    raw_budget = profile.get("budget")
    ceiling = incumbent.get("total_trip_cost") or (
        raw_budget.get("amount") if isinstance(raw_budget, dict) else None
    )
    remaining = None if ceiling is None else max(0.0, float(ceiling) - float(frontier["verified_cost"]))
    if frontier["direction"] == "forward":
        origins = [frontier["node_id"]]
        destinations = ["ANYWHERE"]
        endpoint_policy = {"from": "exact", "to": "discovered"}
        direction = "depart_after"
        earliest = frontier.get("arrival") or profile["date_window"]["start"]
        latest = profile["date_window"]["end"]
        request_type = "regional_market_scan"
    else:
        origins = ["ANYWHERE"]
        destinations = [frontier["node_id"]]
        endpoint_policy = {"from": "discovered", "to": "exact"}
        direction = "arrive_by"
        earliest = profile["date_window"]["start"]
        latest = frontier.get("departure") or profile["date_window"]["end"]
        request_type = "budgeted_predecessor_scan"
    return {
        "request_id": f"frontier-expand-{frontier_id}",
        "request_type": request_type,
        "hypothesis_id": f"hypothesis-{frontier_id}",
        "hypothesis_kind": "ai_frontier_expansion",
        "route_shape": frontier["mode_family"],
        "geographic_cell": frontier["quality_descriptor"]["geography"],
        "anchor_edge_ids": copy.deepcopy(frontier["edge_ids"]),
        "direction": direction,
        "origins": origins,
        "destinations": destinations,
        "from_node": origins[0],
        "to_node": destinations[0],
        "earliest_departure": earliest,
        "latest_arrival": latest,
        "suggested_modes": allowed_modes,
        "provider_kinds": [f"{mode}_schedule_or_offer" for mode in allowed_modes],
        "endpoint_policy": endpoint_policy,
        "date_search": {"mode": profile.get("date_flexibility", {}).get("mode", "bounded_window")},
        "budget_context": {
            "incumbent_route_id": incumbent.get("route_id"),
            "incumbent_total_cost": ceiling,
            "known_prefix_cost": frontier["verified_cost"] if frontier["direction"] == "forward" else 0,
            "known_suffix_cost": frontier["verified_cost"] if frontier["direction"] == "backward" else 0,
            "remaining_budget": remaining,
            "ceiling_source": incumbent.get("source", "user_budget"),
        },
        "corridor_context": {
            "corridor_id": f"hypothesis-{frontier_id}",
            "corridor_kind": "ai_frontier_expansion",
            "geographic_cell": frontier["quality_descriptor"]["geography"],
            "route_shape": frontier["mode_family"],
            "forward_anchor_edge_ids": frontier["edge_ids"] if frontier["direction"] == "forward" else [],
            "backward_anchor_edge_ids": frontier["edge_ids"] if frontier["direction"] == "backward" else [],
        },
        "frontier_context": frontier,
        "selection_advice": {
            "script_recommended": False,
            "reason": "selected_explicitly_by_ai_from_frontier_archive",
            "binding": False,
        },
    }
