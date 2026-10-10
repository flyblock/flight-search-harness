"""Compile a compact AI portfolio-challenger view from round-trip Session state."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


VERSION = "1.0"
TERMINAL_HYPOTHESIS_STATUSES = {"complete", "hard_dead", "deferred_low_value"}
ACTIONABLE_TASK_STATUSES = {"candidate", "pending"}


class PortfolioExplorationError(ValueError):
    pass


def _load_generator() -> Any:
    path = Path(__file__).with_name("round_trip_hypothesis_generator.py")
    spec = importlib.util.spec_from_file_location(
        "round_trip_portfolio_exploration_generator", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load round-trip hypothesis generator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _money_amount(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _gateway_pattern(origin_ids: list[str], destination_ids: list[str]) -> str:
    if len(origin_ids) > 1 and len(destination_ids) > 1:
        return "two_sided_open_jaw"
    if len(origin_ids) > 1:
        return "origin_open_jaw"
    if len(destination_ids) > 1:
        return "destination_open_jaw"
    return "closed_pair"


def _cell(descriptor: dict[str, Any]) -> str:
    token = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:14]
    return f"portfolio-cell-{token}"


def _challenger_id(hypothesis_id: str) -> str:
    token = hashlib.sha256(hypothesis_id.encode("utf-8")).hexdigest()[:14]
    return f"portfolio-challenger-{token}"


def _portfolio_hypotheses(state: dict[str, Any]) -> dict[str, set[str]]:
    task_to_hypothesis = {
        str(task.get("task_id")): str(task.get("hypothesis_id"))
        for task in state.get("research_tasks", [])
        if isinstance(task, dict) and task.get("task_id") and task.get("hypothesis_id")
    }
    result: dict[str, set[str]] = {}
    for portfolio in state.get("result", {}).get("portfolios", []):
        if not isinstance(portfolio, dict) or not portfolio.get("portfolio_id"):
            continue
        result[str(portfolio["portfolio_id"])] = {
            task_to_hypothesis[task_id]
            for task_id in portfolio.get("evidence_task_ids", [])
            if task_id in task_to_hypothesis
        }
    return result


def _registered_challenger(
    state: dict[str, Any], hypothesis_id: str, node: dict[str, Any]
) -> dict[str, Any]:
    gateways = node.get("gateway_nodes", {}) if isinstance(node.get("gateway_nodes"), dict) else {}
    origin_ids = [str(item) for item in gateways.get("origin", [])]
    destination_ids = [str(item) for item in gateways.get("destination", [])]
    tasks = [
        task
        for task in state.get("research_tasks", [])
        if isinstance(task, dict) and task.get("hypothesis_id") == hypothesis_id
    ]
    task_ids = {str(task.get("task_id")) for task in tasks if task.get("task_id")}
    portfolios = [
        portfolio
        for portfolio in state.get("result", {}).get("portfolios", [])
        if isinstance(portfolio, dict)
        and task_ids.intersection(portfolio.get("evidence_task_ids", []))
    ]
    costs = [
        _money_amount(portfolio.get("total_mandatory_cost"))
        for portfolio in portfolios
    ]
    known_costs = [value for value in costs if value is not None]
    task_status_counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status", "unknown"))
        task_status_counts[status] = task_status_counts.get(status, 0) + 1
    descriptor = {
        "route_shape": str(node.get("route_shape") or "unknown"),
        "gateway_pattern": _gateway_pattern(origin_ids, destination_ids),
        "evidence_stage": "complete_portfolio" if portfolios else "registered_research",
    }
    return {
        "challenger_id": _challenger_id(hypothesis_id),
        "hypothesis_id": hypothesis_id,
        "registration_state": "registered",
        "hypothesis_status": node.get("status"),
        "route_shape": node.get("route_shape"),
        "geographic_cell": node.get("geographic_cell"),
        "origin_gateway_nodes": origin_ids,
        "destination_gateway_nodes": destination_ids,
        "gateway_pattern": descriptor["gateway_pattern"],
        "advisory_complete_cost_lower_bound": min(known_costs) if known_costs else None,
        "portfolio_ids": [str(item["portfolio_id"]) for item in portfolios],
        "portfolio_kinds": sorted(
            {str(item.get("portfolio_kind")) for item in portfolios if item.get("portfolio_kind")}
        ),
        "task_status_counts": task_status_counts,
        "quality_descriptor": descriptor,
        "quality_cell_id": _cell(descriptor),
        "hypothesis_spec": None,
        "evidence_status": "registered_session_hypothesis",
        "binding": False,
    }


def _suggested_challenger(spec: dict[str, Any]) -> dict[str, Any]:
    hypothesis_id = str(spec["hypothesis_id"])
    origin_ids = [str(item) for item in spec.get("origin_gateway_nodes", [])]
    destination_ids = [str(item) for item in spec.get("destination_gateway_nodes", [])]
    basis = spec.get("advisory_basis", {}) if isinstance(spec.get("advisory_basis"), dict) else {}
    evidence_stage = (
        "ai_structural_proposal"
        if basis.get("ai_proposed") is True
        else "gateway_evidence_only"
    )
    descriptor = {
        "route_shape": str(spec.get("route_shape") or "unknown"),
        "gateway_pattern": _gateway_pattern(origin_ids, destination_ids),
        "evidence_stage": evidence_stage,
    }
    return {
        "challenger_id": _challenger_id(hypothesis_id),
        "hypothesis_id": hypothesis_id,
        "registration_state": "suggested",
        "hypothesis_status": "unregistered",
        "route_shape": spec.get("route_shape"),
        "geographic_cell": spec.get("geographic_cell"),
        "origin_gateway_nodes": origin_ids,
        "destination_gateway_nodes": destination_ids,
        "gateway_pattern": descriptor["gateway_pattern"],
        "advisory_complete_cost_lower_bound": _money_amount(
            basis.get("complete_cost_lower_bound")
        ),
        "portfolio_ids": [],
        "portfolio_kinds": [],
        "task_status_counts": {},
        "quality_descriptor": descriptor,
        "quality_cell_id": _cell(descriptor),
        "hypothesis_spec": copy.deepcopy(spec),
        "advisory_basis": copy.deepcopy(basis),
        "evidence_status": (
            "ai_proposed_unverified_structure"
            if basis.get("ai_proposed") is True
            else "admitted_gateway_evidence_only"
        ),
        "binding": False,
    }


def _order(item: dict[str, Any]) -> tuple[Any, ...]:
    lower = item.get("advisory_complete_cost_lower_bound")
    return (
        0 if item.get("registration_state") == "registered" else 1,
        lower is None,
        lower if lower is not None else float("inf"),
        str(item.get("route_shape") or ""),
        str(item.get("challenger_id") or ""),
    )


def build_archive(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("trip_scope") != "round_trip":
        raise PortfolioExplorationError("state must be a round_trip Session")
    nodes = state.get("hypothesis_tree", {}).get("nodes", {})
    if not isinstance(nodes, dict):
        raise PortfolioExplorationError("hypothesis tree nodes must be an object")
    challengers = []
    for hypothesis_id, node in nodes.items():
        if (
            isinstance(node, dict)
            and node.get("hypothesis_kind") == "complete_journey"
        ):
            challengers.append(_registered_challenger(state, str(hypothesis_id), node))
    try:
        suggestions = GENERATOR.generate_suggestions(state).get("suggestions", [])
    except GENERATOR.HypothesisGeneratorError as exc:
        raise PortfolioExplorationError(str(exc)) from exc
    challengers.extend(
        _suggested_challenger(spec)
        for spec in suggestions
        if isinstance(spec, dict) and spec.get("hypothesis_id")
    )
    challengers.sort(key=_order)
    cells: dict[str, dict[str, Any]] = {}
    for challenger in challengers:
        cell = cells.setdefault(
            challenger["quality_cell_id"],
            {
                "quality_cell_id": challenger["quality_cell_id"],
                "descriptor": copy.deepcopy(challenger["quality_descriptor"]),
                "member_challenger_ids": [],
                "elite_challenger_id": challenger["challenger_id"],
            },
        )
        cell["member_challenger_ids"].append(challenger["challenger_id"])
        elite = next(
            item
            for item in challengers
            if item["challenger_id"] == cell["elite_challenger_id"]
        )
        if _order(challenger) < _order(elite):
            cell["elite_challenger_id"] = challenger["challenger_id"]
    incumbent = copy.deepcopy(state.get("result", {}).get("best_portfolio"))
    return {
        "portfolio_exploration_version": VERSION,
        "derivation": "admitted_gateway_evidence_registered_hypotheses_and_verified_solver_portfolios",
        "incumbent": incumbent,
        "challenger_count": len(challengers),
        "quality_cell_count": len(cells),
        "challengers": challengers,
        "quality_cells": sorted(cells.values(), key=lambda item: item["quality_cell_id"]),
    }


def build_view(
    state: dict[str, Any], archive: dict[str, Any] | None = None
) -> dict[str, Any]:
    archive = copy.deepcopy(archive or build_archive(state))
    nodes = state.get("hypothesis_tree", {}).get("nodes", {})
    gateway_blockers = [
        hypothesis_id
        for hypothesis_id in (
            "hypothesis-gateway-origin",
            "hypothesis-gateway-destination",
        )
        if nodes.get(hypothesis_id, {}).get("status")
        not in TERMINAL_HYPOTHESIS_STATUSES
    ]
    active = state.get("active_hypothesis")
    actions = []
    if not gateway_blockers and active is None:
        for challenger in archive["challengers"]:
            if challenger["registration_state"] == "registered":
                if challenger.get("hypothesis_status") != "candidate":
                    continue
                task_ids = [
                    str(task.get("task_id"))
                    for task in state.get("research_tasks", [])
                    if isinstance(task, dict)
                    and task.get("hypothesis_id") == challenger["hypothesis_id"]
                    and task.get("status") in ACTIONABLE_TASK_STATUSES
                ]
                if not task_ids:
                    continue
            actions.append(
                {
                    "kind": "activate_portfolio_challenger",
                    "action_key": f"activate_portfolio_challenger:{challenger['challenger_id']}",
                    "challenger_id": challenger["challenger_id"],
                    "hypothesis_id": challenger["hypothesis_id"],
                    "registration_state": challenger["registration_state"],
                    "route_shape": challenger["route_shape"],
                    "gateway_pattern": challenger["gateway_pattern"],
                    "origin_gateway_nodes": copy.deepcopy(
                        challenger["origin_gateway_nodes"]
                    ),
                    "destination_gateway_nodes": copy.deepcopy(
                        challenger["destination_gateway_nodes"]
                    ),
                    "quality_cell_id": challenger["quality_cell_id"],
                    "advisory_complete_cost_lower_bound": challenger.get(
                        "advisory_complete_cost_lower_bound"
                    ),
                    "question": "Is this observed-gateway or explicitly AI-proposed complete round-trip structure worth verifying across its required atomic and surface tasks?",
                    "binding": False,
                }
            )
    elites = {
        cell["elite_challenger_id"] for cell in archive.get("quality_cells", [])
    }
    return {
        "portfolio_exploration_view_version": VERSION,
        "decision_owner": "ai",
        "active_hypothesis_id": (
            active.get("hypothesis_id") if isinstance(active, dict) else None
        ),
        "gateway_blockers": gateway_blockers,
        "incumbent": copy.deepcopy(archive.get("incumbent")),
        "archive_summary": {
            "challenger_count": archive["challenger_count"],
            "quality_cell_count": archive["quality_cell_count"],
        },
        "recommended_challenger_ids": [
            item["challenger_id"]
            for item in archive["challengers"]
            if item["challenger_id"] in elites
        ],
        "challenger_actions": actions,
        "rule": "Select a complete portfolio structure, not an isolated leg. Cell elites and lower bounds are non-binding; every published action remains AI-selectable.",
        "unsearched_scope": "Unregistered gateways, unobserved date pairs, Providers, and portfolio structures remain unknown.",
    }


def challenger_by_id(
    archive: dict[str, Any], challenger_id: str
) -> dict[str, Any]:
    match = next(
        (
            item
            for item in archive.get("challengers", [])
            if item.get("challenger_id") == challenger_id
        ),
        None,
    )
    if match is None:
        raise PortfolioExplorationError(f"unknown challenger_id: {challenger_id}")
    return copy.deepcopy(match)
