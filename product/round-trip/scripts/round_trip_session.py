"""Persistent AI-led session for city/region round-trip research."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flight_search_harness.price_freshness import (
    offer_price_deadline,
    offset_timestamp,
)
from flight_search_harness.search_effort import search_effort_policy


SESSION_VERSION = "0.4.0"
TRIP_SCOPE = "round_trip"
ROOT_HYPOTHESIS = "hypothesis-root"
ORIGIN_GATEWAY_HYPOTHESIS = "hypothesis-gateway-origin"
DESTINATION_GATEWAY_HYPOTHESIS = "hypothesis-gateway-destination"
TERMINAL_HYPOTHESIS_STATUSES = {"complete", "hard_dead", "deferred_low_value"}
REQUIREMENT_STATUSES = {"open", "satisfied", "exhausted", "superseded"}
SURFACE_DISCOVERY_HORIZON_MINUTES = 1440
FRONTIER_SCREEN_RESEARCH_KIND = "portfolio_frontier_screen"
PRICE_CHALLENGE_SCREEN_RESEARCH_KIND = "price_challenge_screen"
INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND = "internal_surface_transfer"
TERMINAL_GATEWAY_COVERAGE = {
    "exact_anchor",
    "legacy_terminal",
    "provider_scope_executed",
    "research_observed",
}


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


def _load_solver():
    path = Path(__file__).with_name("round_trip_portfolio_solver.py")
    spec = importlib.util.spec_from_file_location("round_trip_portfolio_solver", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SOLVER = _load_solver()


def _load_projection():
    path = Path(__file__).with_name("round_trip_solver_projection.py")
    spec = importlib.util.spec_from_file_location("round_trip_solver_projection", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROJECTION = _load_projection()


def _load_identity():
    path = Path(__file__).with_name("round_trip_identity.py")
    spec = importlib.util.spec_from_file_location("round_trip_identity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


IDENTITY = _load_identity()


def _load_task_compatibility():
    path = Path(__file__).with_name("round_trip_task_compatibility.py")
    spec = importlib.util.spec_from_file_location(
        "round_trip_task_compatibility", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TASK_COMPATIBILITY = _load_task_compatibility()


def _load_ai_exploration():
    path = Path(__file__).with_name("round_trip_ai_exploration.py")
    spec = importlib.util.spec_from_file_location("round_trip_ai_exploration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


AI_EXPLORATION = _load_ai_exploration()


def _load_scope_compiler():
    path = Path(__file__).with_name("round_trip_scope_compiler.py")
    spec = importlib.util.spec_from_file_location("round_trip_scope_compiler", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SCOPE_COMPILER = _load_scope_compiler()


class RoundTripSessionError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoundTripSessionError(f"{field} must be a non-empty string")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _initial_research_credits(scenario: dict[str, Any]) -> dict[str, Any]:
    policy = search_effort_policy(
        scenario.get("intent", {}).get("search_effort")
    )
    budget = int(policy["research_credit_budget"])
    return {
        "research_credit_ledger_version": "1.0",
        **policy,
        "used": 0,
        "reserved": 0,
        "remaining": budget,
        "price_refresh_used": 0,
        "price_refresh_reserved": 0,
        "entries": {},
        "stop_reason": None,
    }


def research_credit_cost(task: dict[str, Any]) -> int:
    """Return the Product-owned cost of one real external investigation."""

    explicit = task.get("research_credit_cost")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return max(0, explicit)
    research_kind = str(task.get("research_kind") or "")
    if research_kind == "region_scope_definition":
        return 0
    if research_kind in {
        "region_pool_fare_screen",
        PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
        FRONTIER_SCREEN_RESEARCH_KIND,
    }:
        return 1
    if research_kind == INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND:
        return 1
    if research_kind == "priced_offer":
        return 2
    return 0


def reserve_research_credit(
    raw_state: Any,
    *,
    item_id: str,
    task_id: str,
    cost: int,
    budget_kind: str = "search",
) -> dict[str, Any]:
    """Reserve credits for a Work Item without charging retries twice."""

    state = validate_session(raw_state)
    ledger = state["research_credits"]
    entries = ledger["entries"]
    existing = entries.get(item_id)
    if isinstance(existing, dict):
        return state
    if cost < 0:
        raise RoundTripSessionError("research credit cost must be non-negative")
    if budget_kind not in {"search", "price_refresh"}:
        raise RoundTripSessionError("research credit budget_kind is unsupported")
    if budget_kind == "search" and cost > int(ledger["remaining"]):
        raise RoundTripSessionError("Research Credit budget is exhausted")
    entries[item_id] = {
        "item_id": item_id,
        "task_id": task_id,
        "cost": int(cost),
        "budget_kind": budget_kind,
        "status": "reserved" if cost else "committed",
        "reserved_at": _now(),
        "execution_origin": "non_provider" if cost == 0 else None,
    }
    if cost and budget_kind == "price_refresh":
        ledger["price_refresh_reserved"] = int(
            ledger.get("price_refresh_reserved", 0)
        ) + cost
    elif cost:
        ledger["reserved"] = int(ledger["reserved"]) + cost
        ledger["remaining"] = int(ledger["research_credit_budget"]) - int(
            ledger["used"]
        ) - int(ledger["reserved"])
    _commit_product_mutation(state)
    return state


def record_research_receipt(
    raw_state: Any,
    *,
    item_id: str,
    execution_origin: str | None,
    completed: bool,
) -> dict[str, Any]:
    """Commit or release one idempotent Research Credit reservation."""

    state = validate_session(raw_state)
    ledger = state["research_credits"]
    entry = ledger["entries"].get(item_id)
    if not isinstance(entry, dict) or entry.get("status") != "reserved":
        return state
    cost = int(entry.get("cost", 0))
    budget_kind = str(entry.get("budget_kind") or "search")
    if execution_origin == "provider_query":
        entry["status"] = "committed"
        if budget_kind == "price_refresh":
            ledger["price_refresh_used"] = int(
                ledger.get("price_refresh_used", 0)
            ) + cost
        else:
            ledger["used"] = int(ledger["used"]) + cost
    else:
        entry["status"] = "released"
    if budget_kind == "price_refresh":
        ledger["price_refresh_reserved"] = max(
            0, int(ledger.get("price_refresh_reserved", 0)) - cost
        )
    else:
        ledger["reserved"] = max(0, int(ledger["reserved"]) - cost)
    ledger["remaining"] = int(ledger["research_credit_budget"]) - int(
        ledger["used"]
    ) - int(ledger["reserved"])
    entry["execution_origin"] = execution_origin
    entry["decided_at"] = _now()
    _commit_product_mutation(state)
    return state


def _node_id(node: Any, field: str) -> str:
    if isinstance(node, str):
        return _text(node, field)
    if isinstance(node, dict):
        return _text(node.get("node_id"), f"{field}.node_id")
    raise RoundTripSessionError(f"{field} must be a node object or string")


def _empty_result(currency: str) -> dict[str, Any]:
    return {
        "solver_version": SOLVER.VERSION,
        "search_scope": "verified_atomic_offers_in_session",
        "enumeration_complete_within_supplied_offer_pool": True,
        "base_currency": currency,
        "offers_considered": 0,
        "portfolios": [],
        "best_portfolio": None,
        "portfolio_kind_counts": {kind: 0 for kind in SOLVER.PORTFOLIO_KINDS},
    }


def _validate_scenario(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("scenario_version") != SESSION_VERSION:
        raise RoundTripSessionError(f"scenario_version must be {SESSION_VERSION}")
    scenario = copy.deepcopy(raw)
    try:
        SOLVER._validate_bundle(
            {
                "schema_version": SOLVER.VERSION,
                "base_currency": scenario.get("base_currency"),
                "intent": scenario.get("intent"),
                "nodes": scenario.get("nodes"),
                "transport_entitlements": scenario.get("transport_entitlements", []),
                "fx_snapshot": scenario.get("fx_snapshot"),
                "movements": [],
                "offers": [],
            }
        )
    except SOLVER.RoundTripError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    intent = scenario["intent"]
    for side in ("origin_pool", "destination_pool"):
        pool = intent.get(side)
        if not isinstance(pool, dict):
            raise RoundTripSessionError(f"intent.{side} must be an object")
        if pool.get("policy") not in {"exact", "city_region", "extended_region"}:
            raise RoundTripSessionError(f"intent.{side}.policy is unsupported")
    return scenario


def _new_temporal_state(research_kind: str, journey_scope: str | None, query_shape: dict[str, Any]) -> dict[str, Any]:
    applicable = research_kind == "priced_offer" and journey_scope in {"joint_round_trip", "one_way"}
    if not applicable:
        phase = "not_applicable"
    else:
        date_search = query_shape.get("date_search", {}) if isinstance(query_shape, dict) else {}
        phase = "exact_verification" if date_search.get("mode") == "exact_dates" else "surface_discovery"
    return {
        "phase": phase,
        "signal_refs": [],
        "active_selection": None,
        "selection_history": [],
    }


def _requirement_status_from_legacy(status: Any) -> tuple[str, str | None]:
    if status in {"candidate", "pending"}:
        return "open", None
    if status in {"ai_proposed", "verified_observed", "research_observed"}:
        return "satisfied", str(status)
    if status in {"no_result", "rejected"}:
        return "exhausted", str(status)
    if status in {"superseded", "covered_by_hypothesis_disposition"}:
        return "superseded", None
    raise RoundTripSessionError(f"unsupported legacy research requirement status: {status}")


def _set_requirement_state(
    task: dict[str, Any],
    requirement_status: str,
    *,
    evidence_disposition: str | None = None,
) -> None:
    if requirement_status not in REQUIREMENT_STATUSES:
        raise RoundTripSessionError(
            f"unsupported research requirement status: {requirement_status}"
        )
    task["requirement_status"] = requirement_status
    task["evidence_disposition"] = evidence_disposition


def _refresh_requirement_status_projection(state: dict[str, Any]) -> None:
    active_id = state.get("active_hypothesis_id")
    for task in state.get("research_tasks", []):
        requirement_status = task.get("requirement_status")
        if requirement_status not in REQUIREMENT_STATUSES:
            requirement_status, evidence_disposition = _requirement_status_from_legacy(
                task.get("status")
            )
            task["requirement_status"] = requirement_status
            task.setdefault("evidence_disposition", evidence_disposition)
        evidence_disposition = task.get("evidence_disposition")
        if requirement_status == "open":
            task["status"] = (
                "pending" if task.get("hypothesis_id") == active_id else "candidate"
            )
        elif requirement_status == "satisfied":
            task["status"] = evidence_disposition or "verified_observed"
        elif requirement_status == "exhausted":
            task["status"] = evidence_disposition or "no_result"
        else:
            task["status"] = "superseded"


def _task(
    task_id: str,
    hypothesis_id: str,
    hypothesis_kind: str,
    route_shape: str,
    research_kind: str,
    question: str,
    query_shape: dict[str, Any],
    *,
    journey_scope: str | None = None,
    direction: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    task = {
        "task_id": task_id,
        "hypothesis_id": hypothesis_id,
        "hypothesis_kind": hypothesis_kind,
        "parent_hypothesis_id": ROOT_HYPOTHESIS,
        "route_shape": route_shape,
        "research_kind": research_kind,
        "journey_scope": journey_scope,
        "direction": direction,
        "question": question,
        "query_shape": copy.deepcopy(query_shape),
        "required": required,
        "requirement_status": "open",
        "evidence_disposition": None,
        "status": "candidate",
        "attempt_count": 0,
        "evidence_bundle_ids": [],
        "terminal_reason": None,
        "temporal_state": _new_temporal_state(research_kind, journey_scope, query_shape),
    }
    task["research_credit_cost"] = research_credit_cost(task)
    return task


def _region_scope_task(side: str, scenario: dict[str, Any]) -> dict[str, Any]:
    spec = AI_EXPLORATION.region_scope_task_spec(side, scenario)
    return _task(
        spec["task_id"],
        f"hypothesis-gateway-{side}",
        "gateway_discovery",
        f"{side}_regional_gateway_pool",
        spec["research_kind"],
        spec["question"],
        spec["query_shape"],
    )


def _date_search(intent: dict[str, Any], direction: str) -> dict[str, Any]:
    flexibility = copy.deepcopy(intent.get("date_flexibility", {"mode": "fully_flexible"}))
    mode = flexibility.get(f"{direction}_mode", flexibility.get("mode", "fully_flexible"))
    result = {"mode": mode}
    if mode == "exact_dates":
        field = f"{direction}_exact_departure_dates"
        result["exact_departure_dates"] = list(flexibility[field])
    return result


def _joint_date_search(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": intent.get("date_flexibility", {}).get("mode", "fully_flexible"),
        "outbound": _date_search(intent, "outbound"),
        "return": _date_search(intent, "return"),
        "atomic_total_required": True,
        "rule": "Observe each joint date pair as one indivisible round-trip total; never derive it from directional prices.",
    }


def _tree_node(hypothesis_id: str, kind: str, route_shape: str, order: int, status: str = "candidate") -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "hypothesis_kind": kind,
        "sibling_order": order,
        "route_shape": route_shape,
        "geographic_cell": None,
        "status": status,
        "selected_at_round": None,
        "terminal_reason": None,
    }


def _initial_control_plane(scenario: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    nodes = {ROOT_HYPOTHESIS: _tree_node(ROOT_HYPOTHESIS, "root", "round_trip", 0, "active")}
    gateway_candidates = {"origin": [], "destination": []}
    region_scopes = AI_EXPLORATION.initial_region_scopes(scenario)
    for order, side in enumerate(("origin", "destination"), start=1):
        hypothesis_id = f"hypothesis-gateway-{side}"
        pool = scenario["intent"][f"{side}_pool"]
        anchor_ids = set(scenario["intent"][f"{side}_anchor_nodes"])
        node_by_id = {_node_id(node, "scenario.nodes[]"): node for node in scenario["nodes"]}
        seed_ids = list(pool.get("seed_gateway_node_ids", []))
        exact_airport_anchors = [node_id for node_id in anchor_ids if isinstance(node_by_id[node_id], dict) and node_by_id[node_id].get("node_type") == "airport"]
        extra_seed_ids = sorted(set(seed_ids) - set(exact_airport_anchors))
        auto_complete = (
            pool.get("policy") == "exact"
            and bool(exact_airport_anchors)
            and not extra_seed_ids
        )
        nodes[hypothesis_id] = _tree_node(hypothesis_id, "gateway_discovery", f"{side}_regional_gateway_pool", order, "complete" if auto_complete else "candidate")
        for node_id in dict.fromkeys(exact_airport_anchors):
            node = copy.deepcopy(node_by_id[node_id])
            gateway_candidates[side].append({"candidate_id": f"exact-{side}-{node_id}", "side": side, "node": node, "signal": {"kind": "exact_anchor"}, "evidence_status": "exact_anchor"})
        if not auto_complete:
            tasks.append(_region_scope_task(side, scenario))
    tree = {
        "tree_version": SESSION_VERSION,
        "root_hypothesis_id": ROOT_HYPOTHESIS,
        "nodes": nodes,
        "next_candidate_id": next((hid for hid in (ORIGIN_GATEWAY_HYPOTHESIS, DESTINATION_GATEWAY_HYPOTHESIS) if nodes[hid]["status"] == "candidate"), None),
    }
    return tasks, tree, gateway_candidates, region_scopes


def _synchronize_canonical_state(state: dict[str, Any]) -> None:
    """Capture canonical product state from the Adapter compatibility projection."""

    nodes = state.get("hypothesis_tree", {}).get("nodes", {})
    selected_ids = {
        str(item.get("hypothesis_id"))
        for item in state.get("hypothesis_history", [])
        if isinstance(item, dict)
        and item.get("action") in {"select", "resolve_region_endpoints"}
        and item.get("hypothesis_id")
    }
    hypotheses: dict[str, dict[str, Any]] = {}
    for hypothesis_id, node in nodes.items():
        if hypothesis_id == ROOT_HYPOTHESIS or not isinstance(node, dict):
            continue
        value = copy.deepcopy(node)
        value["was_selected"] = bool(
            value.get("was_selected")
            or value.get("selected_at_round") is not None
            or hypothesis_id in selected_ids
        )
        hypotheses[str(hypothesis_id)] = value
    state["hypotheses"] = hypotheses

    active = state.get("active_hypothesis")
    if isinstance(active, dict):
        state["active_hypothesis_id"] = active.get("hypothesis_id")
        state["active_requirement_id"] = active.get("selected_task_id")
        state["active_hypothesis_reason"] = active.get("reason")
    else:
        state["active_hypothesis_id"] = None
        state["active_requirement_id"] = None
        state["active_hypothesis_reason"] = None

    _refresh_requirement_status_projection(state)


def _rebuild_compatibility_projections(state: dict[str, Any]) -> None:
    """Rebuild fields required by the current Adapter from canonical state."""

    root = _tree_node(ROOT_HYPOTHESIS, "root", "round_trip", 0, "active")
    nodes = {ROOT_HYPOTHESIS: root}
    nodes.update(copy.deepcopy(state.get("hypotheses", {})))
    state["hypothesis_tree"] = {
        "tree_version": SESSION_VERSION,
        "root_hypothesis_id": ROOT_HYPOTHESIS,
        "nodes": nodes,
        "next_candidate_id": _next_hypothesis({"hypothesis_tree": {"nodes": nodes}}),
    }

    active_id = state.get("active_hypothesis_id")
    state["active_hypothesis"] = (
        {
            "hypothesis_id": active_id,
            "selected_task_id": state.get("active_requirement_id"),
            "reason": state.get("active_hypothesis_reason"),
        }
        if active_id
        else None
    )
    state["hypothesis_history"] = [
        {
            "action": "select",
            "hypothesis_id": hypothesis_id,
            "task_id": None,
            "reason": "reconstructed from canonical hypothesis state",
            "search_round": node.get("selected_at_round", 0) or 0,
            "at": None,
        }
        for hypothesis_id, node in sorted(state.get("hypotheses", {}).items())
        if node.get("was_selected")
    ]

    _refresh_requirement_status_projection(state)


def _derive_hypothesis_outcomes(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "hypothesis_id": hypothesis_id,
            "status": node.get("status"),
            "reason": node.get("terminal_reason"),
            "search_round": node.get("terminal_at_round", state.get("search_round", 0)),
        }
        for hypothesis_id, node in sorted(state.get("hypotheses", {}).items())
        if node.get("status") in TERMINAL_HYPOTHESIS_STATUSES
    ]


def _validate_canonical_state(state: dict[str, Any]) -> None:
    hypotheses = state.get("hypotheses")
    if not isinstance(hypotheses, dict):
        raise RoundTripSessionError("hypotheses must be an object")
    allowed_hypothesis_statuses = {
        "candidate",
        "selected",
        "dormant",
        *TERMINAL_HYPOTHESIS_STATUSES,
    }
    for hypothesis_id, hypothesis in hypotheses.items():
        if not isinstance(hypothesis, dict):
            raise RoundTripSessionError(f"hypothesis {hypothesis_id} must be an object")
        if hypothesis.get("hypothesis_id") != hypothesis_id:
            raise RoundTripSessionError(
                f"hypothesis key does not match hypothesis_id: {hypothesis_id}"
            )
        if hypothesis.get("status") not in allowed_hypothesis_statuses:
            raise RoundTripSessionError(
                f"hypothesis {hypothesis_id} has unsupported status"
            )

    active_id = state.get("active_hypothesis_id")
    selected = sorted(
        hypothesis_id
        for hypothesis_id, hypothesis in hypotheses.items()
        if hypothesis.get("status") == "selected"
    )
    if active_id is None:
        if selected:
            raise RoundTripSessionError(
                f"selected hypotheses require active_hypothesis_id: {selected}"
            )
    elif active_id not in hypotheses or selected != [active_id]:
        raise RoundTripSessionError(
            "active_hypothesis_id must identify the only selected hypothesis"
        )

    for task in state.get("research_tasks", []):
        if task.get("hypothesis_id") not in hypotheses:
            raise RoundTripSessionError(
                f"research requirement references unknown hypothesis: {task.get('task_id')}"
            )
        if task.get("requirement_status") not in REQUIREMENT_STATUSES:
            raise RoundTripSessionError(
                f"research requirement has unsupported status: {task.get('task_id')}"
            )


def _compact_session(state: dict[str, Any]) -> dict[str, Any]:
    """Remove reconstructable Adapter projections before persistence."""

    for field in (
        "active_hypothesis",
        "hypothesis_history",
        "hypothesis_tree",
        "evidence_bundle_ids",
    ):
        state.pop(field, None)
    for task in state.get("research_tasks", []):
        task.pop("status", None)
    return state


def _commit_product_mutation(state: dict[str, Any]) -> int:
    if "hypothesis_tree" not in state:
        hydrated = validate_session(state)
        state.clear()
        state.update(hydrated)
    _synchronize_canonical_state(state)
    revision = _advance_product_revision(state)
    _rebuild_compatibility_projections(state)
    _compact_session(state)
    return revision


def _invalidate_finalization(state: dict[str, Any]) -> None:
    state["finalization_status"] = "open"


def _advance_product_revision(state: dict[str, Any]) -> int:
    """Advance the Product Kernel's own mutation revision.

    This revision belongs to the round-trip Product Kernel.  It is deliberately
    independent from Harness Run, Search Mission, Lease, and Receipt revisions.
    """

    revision = state.get("product_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RoundTripSessionError("product_revision must be a non-negative integer")
    revision += 1
    state["product_revision"] = revision
    return revision


def initialize_session(raw_scenario: Any) -> dict[str, Any]:
    scenario = _validate_scenario(raw_scenario)
    tasks, tree, gateway_candidates, region_scopes = _initial_control_plane(scenario)
    token = hashlib.sha256(json.dumps(scenario, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    state = {
        "session_version": SESSION_VERSION,
        "session_id": f"round-trip-{token}",
        "trip_scope": TRIP_SCOPE,
        "scenario": scenario,
        "product_revision": 0,
        "search_round": 0,
        "active_hypothesis": None,
        "hypothesis_tree": tree,
        "research_tasks": tasks,
        "gateway_candidates": gateway_candidates,
        "region_scopes": region_scopes,
        "temporal_signals": [],
        "internal_surface_transfers": [],
        "evidence_bundles": [],
        "movements": [],
        "offers": [],
        "solver_projection": {
            "projection_version": PROJECTION.PROJECTION_VERSION,
            "status": "complete_ledger",
            "ledger_offer_count": 0,
            "solver_offer_limit": scenario["intent"]["max_solver_offers"],
            "pinned_offer_ids": [],
            "retired_offer_ids": [],
            "selected_offer_ids": [],
            "excluded_offer_ids": [],
            "unresolved_capacity_exclusion_offer_ids": [],
            "unresolved_competitive_alternative_offer_ids": [],
            "exclusion_reasons": {},
            "represented_offer_ids": {},
            "selection_reasons": {},
            "coverage_claim": "solver result is complete only within selected_offer_ids; excluded ledger evidence remains persisted",
        },
        "result": _empty_result(scenario["base_currency"]),
        "research_credits": _initial_research_credits(scenario),
        "finalization_status": "open",
    }
    _synchronize_canonical_state(state)
    _rebuild_compatibility_projections(state)
    return _compact_session(state)


def migrate_session(raw: Any) -> dict[str, Any]:
    """Return a migrated copy of a persisted Session.

    Migration is kept separate from derived-view refresh and never mutates the
    caller's object.  The public validator calls this compatibility step until
    the Harness Adapter has an explicit migration hook.
    """

    if not isinstance(raw, dict) or raw.get("session_version") != SESSION_VERSION or raw.get("trip_scope") != TRIP_SCOPE:
        raise RoundTripSessionError(f"Session must be {SESSION_VERSION} round_trip")
    _validate_scenario(raw.get("scenario"))
    required = ["research_tasks", "gateway_candidates", "movements", "offers", "result", "finalization_status"]
    missing = [field for field in required if field not in raw]
    if missing:
        raise RoundTripSessionError(f"Session missing fields {missing}")
    state = copy.deepcopy(raw)
    if "hypothesis_tree" not in state and "hypotheses" not in state:
        raise RoundTripSessionError(
            "Session requires canonical hypotheses or a legacy hypothesis_tree"
        )
    migrated = "product_revision" not in state
    state.setdefault("product_revision", 0)
    state.setdefault("temporal_signals", [])
    state.setdefault("internal_surface_transfers", [])
    state.setdefault("research_credits", _initial_research_credits(state["scenario"]))
    state["research_credits"].setdefault("price_refresh_used", 0)
    state["research_credits"].setdefault("price_refresh_reserved", 0)
    state.setdefault("solver_projection", {"pinned_offer_ids": []})
    for field in (
        "portfolio_challenger_dispositions",
        "portfolio_exploration_archive",
        "portfolio_exploration_view",
    ):
        state.pop(field, None)
    if "hypothesis_tree" not in state:
        state.setdefault("active_hypothesis_id", None)
        state.setdefault("active_requirement_id", None)
        state.setdefault("active_hypothesis_reason", None)
        _rebuild_compatibility_projections(state)
    if "region_scopes" not in state:
        migrated = True
        state["region_scopes"] = AI_EXPLORATION.initial_region_scopes(
            state["scenario"]
        )
        for side in ("origin", "destination"):
            scope = state["region_scopes"][side]
            existing = [
                copy.deepcopy(item)
                for item in state.get("gateway_candidates", {}).get(side, [])
                if isinstance(item, dict) and isinstance(item.get("node"), dict)
            ]
            if existing and scope["status"] != "not_required":
                scope.update(
                    {
                        "status": "legacy_complete",
                        "decision_owner": "legacy_session_migration",
                        "candidate_nodes": [item["node"] for item in existing],
                        "candidate_records": [
                            {
                                "candidate_id": item.get(
                                    "candidate_id", f"legacy-{side}-{item['node']['node_id']}"
                                ),
                                "node": item["node"],
                                "roles": ["specialized_gateway"],
                                "inclusion_reason": "Migrated from pre-contract Session gateway state.",
                            }
                            for item in existing
                        ],
                        "coverage": {
                            str(item["node"]["node_id"]): "legacy_terminal"
                            for item in existing
                        },
                    }
                )
    structural_id = "hypothesis-structural-challenger-discovery"
    state["hypothesis_tree"]["nodes"].pop(structural_id, None)
    state["research_tasks"] = [
        task
        for task in state["research_tasks"]
        if task.get("hypothesis_id") != structural_id
        and task.get("research_kind") != "structural_challenger_definition"
    ]
    active = state.get("active_hypothesis")
    if isinstance(active, dict) and active.get("hypothesis_id") == structural_id:
        state["active_hypothesis"] = None
    for field in (
        "structural_exploration",
        "challenger_dispositions",
        "stop_assessment",
    ):
        state.pop(field, None)
    for task in state["research_tasks"]:
        task.setdefault(
            "temporal_state",
            _new_temporal_state(task.get("research_kind"), task.get("journey_scope"), task.get("query_shape", {})),
        )
        if task.get("research_kind") == "region_pool_fare_screen":
            task.setdefault("scope_partition", _scope_partition(task))
            if not isinstance(task.get("scope_execution"), dict):
                admitted_scopes = [
                    copy.deepcopy(provider_scope)
                    for bundle in state.get("evidence_bundles", [])
                    if isinstance(bundle, dict)
                    and bundle.get("task_id") == task.get("task_id")
                    for provider_scope in bundle.get("provider_scopes", [])
                    if isinstance(provider_scope, dict)
                ]
                if admitted_scopes:
                    execution_status = "executed"
                elif (
                    task.get("evidence_disposition") == "rejected"
                    and task.get("query_shape", {}).get("scope_strategy")
                    == "provider_native_first"
                ):
                    execution_status = "fallback_required"
                elif int(task.get("attempt_count", 0)) > 0:
                    execution_status = "unproven"
                else:
                    execution_status = "unsearched"
                task["scope_execution"] = {
                    "status": execution_status,
                    "provider_scopes": admitted_scopes,
                }
    node_by_id = {
        _node_id(node, "scenario.nodes[]"): node
        for node in state["scenario"]["nodes"]
    }
    for side in ("origin", "destination"):
        pool = state["scenario"]["intent"][f"{side}_pool"]
        anchor_ids = set(
            state["scenario"]["intent"][f"{side}_anchor_nodes"]
        )
        exact_airports = {
            node_id
            for node_id in anchor_ids
            if node_id in node_by_id
            and node_by_id[node_id].get("node_type") == "airport"
        }
        seed_ids = set(pool.get("seed_gateway_node_ids", []))
        if (
            pool.get("policy") == "exact"
            and exact_airports
            and seed_ids <= exact_airports
        ):
            hypothesis_id = f"hypothesis-gateway-{side}"
            node = state["hypothesis_tree"]["nodes"].get(hypothesis_id)
            if isinstance(node, dict) and node.get("status") == "candidate":
                migrated = True
                node["status"] = "complete"
                node["terminal_reason"] = "exact airport anchor is its own admitted gateway"
                for task in state["research_tasks"]:
                    if (
                        task.get("hypothesis_id") == hypothesis_id
                        and task.get("requirement_status", "open") == "open"
                    ):
                        _set_requirement_state(task, "superseded")
                        task["status"] = "superseded"
                        task["terminal_reason"] = node["terminal_reason"]
        scope = state.get("region_scopes", {}).get(side, {})
        if scope.get("status") == "defined_pending_challenge":
            migrated = True
            scope["status"] = "defined"
            for task in state["research_tasks"]:
                if (
                    task.get("research_kind") == "region_scope_challenge"
                    and task.get("query_shape", {}).get("side") == side
                    and task.get("requirement_status") == "open"
                ):
                    _set_requirement_state(task, "superseded")
                    task["status"] = "superseded"
                    task["terminal_reason"] = (
                        "Regional scope now uses one bounded semantic pass."
                    )
            _publish_region_pool_fare_screen_if_ready(state, side)
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    if "hypotheses" not in state:
        _synchronize_canonical_state(state)
    else:
        state.setdefault("active_hypothesis_id", None)
        state.setdefault("active_requirement_id", None)
        state.setdefault("active_hypothesis_reason", None)
        _rebuild_compatibility_projections(state)
    for field in (
        "query_ledger",
        "offer_sets",
        "portfolio_challenger_history",
        "finalization_history",
    ):
        state.pop(field, None)
    return state


def validate_session(raw: Any) -> dict[str, Any]:
    """Validate and return an isolated Session copy.

    Current-format Sessions pass through without product transitions or view
    recomputation.  Compatibility migration remains explicit above and is only
    exercised for documents that actually lack a current field.
    """

    state = migrate_session(raw)
    required = [
        "product_revision",
        "hypotheses",
        "active_hypothesis_id",
        "region_scopes",
        "temporal_signals",
        "solver_projection",
    ]
    missing = [field for field in required if field not in state]
    if missing:
        raise RoundTripSessionError(f"Session missing fields {missing}")
    revision = state["product_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RoundTripSessionError("product_revision must be a non-negative integer")
    _validate_canonical_state(state)
    return state


def _task_by_id(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = next((item for item in state["research_tasks"] if item["task_id"] == task_id), None)
    if task is None:
        raise RoundTripSessionError(f"unknown task_id {task_id}")
    return task


def _iso_date(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError as exc:
        raise RoundTripSessionError(f"{field} must be an ISO date") from exc


def _round_trip_signal_dates(
    signal: dict[str, Any], field: str
) -> tuple[str, str, str]:
    """Return departure, destination-arrival, and return-departure Local Dates."""

    if signal.get("temporal_signal_version") == "2.0":
        dates = signal.get("itinerary_dates")
        if not isinstance(dates, dict):
            raise RoundTripSessionError(f"{field}.itinerary_dates must be an object")
        return (
            _iso_date(
                dates.get("outbound_departure_local_date"),
                f"{field}.itinerary_dates.outbound_departure_local_date",
            ),
            _iso_date(
                dates.get("outbound_destination_arrival_local_date"),
                f"{field}.itinerary_dates.outbound_destination_arrival_local_date",
            ),
            _iso_date(
                dates.get("return_departure_local_date"),
                f"{field}.itinerary_dates.return_departure_local_date",
            ),
        )
    outbound = _iso_date(signal.get("outbound_date"), f"{field}.outbound_date")
    # Legacy evidence never distinguished arrival from departure.  Preserve
    # read compatibility without permitting this encoding in new Receipts.
    arrival = _iso_date(
        signal.get("outbound_destination_arrival_date", outbound),
        f"{field}.outbound_destination_arrival_date",
    )
    returning = _iso_date(signal.get("return_date"), f"{field}.return_date")
    return outbound, arrival, returning


def _date_in_window(value: str, window: dict[str, Any], field: str) -> None:
    day = datetime.fromisoformat(value).date()
    start = datetime.fromisoformat(_text(window.get("start"), f"{field}.start").replace("Z", "+00:00")).date()
    end = datetime.fromisoformat(_text(window.get("end"), f"{field}.end").replace("Z", "+00:00")).date()
    if not start <= day <= end:
        raise RoundTripSessionError(f"{field} date falls outside the user window")


def record_temporal_selection(raw_state: Any, task_id: str, raw_spec: Any, reason: str) -> dict[str, Any]:
    """Record an explicit AI date choice without letting advisory ranking bind execution."""
    state = validate_session(raw_state)
    task = _task_by_id(state, _text(task_id, "task_id"))
    if task.get("requirement_status") != "open":
        raise RoundTripSessionError("temporal selection requires an actionable task")
    active = state.get("active_hypothesis")
    if not isinstance(active, dict) or active.get("hypothesis_id") != task["hypothesis_id"]:
        raise RoundTripSessionError("temporal selection task must belong to the active hypothesis")
    temporal = task.get("temporal_state", {})
    if temporal.get("phase") in {None, "not_applicable", "terminal"}:
        raise RoundTripSessionError("task does not accept a temporal selection")
    if not isinstance(raw_spec, dict):
        raise RoundTripSessionError("temporal selection spec must be an object")
    origin = raw_spec.get("origin", "ai_proposed")
    if origin not in {"scheduler_candidate", "ai_proposed", "user_exact"}:
        raise RoundTripSessionError("temporal selection origin is unsupported")
    selection: dict[str, Any] = {
        "candidate_id": _text(raw_spec.get("candidate_id"), "candidate_id"),
        "origin": origin,
        "selected_by": "ai",
        "reason": _text(reason, "reason"),
        "selected_at": _now(),
        "binding_source": "explicit_ai_action",
        "advisory_rank_required": False,
    }
    query = task["query_shape"]
    if task.get("journey_scope") == "joint_round_trip":
        outbound = _iso_date(raw_spec.get("outbound_date"), "outbound_date")
        returning = _iso_date(raw_spec.get("return_date"), "return_date")
        if returning < outbound:
            raise RoundTripSessionError("return_date must not precede outbound_date")
        _date_in_window(outbound, query["outbound_departure_window"], "outbound_departure_window")
        _date_in_window(returning, query["return_departure_window"], "return_departure_window")
        date_search = query.get("date_search", {})
        outbound_exact = set(date_search.get("outbound", {}).get("exact_departure_dates", []))
        return_exact = set(date_search.get("return", {}).get("exact_departure_dates", []))
        if outbound_exact and outbound not in outbound_exact:
            raise RoundTripSessionError("outbound_date is outside the exact user date set")
        if return_exact and returning not in return_exact:
            raise RoundTripSessionError("return_date is outside the exact user date set")
        selection.update({"outbound_date": outbound, "return_date": returning, "stay_requires_exact_timeline_validation": True})
    elif task.get("journey_scope") == "one_way":
        departure = _iso_date(raw_spec.get("departure_date"), "departure_date")
        _date_in_window(departure, query["departure_window"], "departure_window")
        exact = set(query.get("date_search", {}).get("exact_departure_dates", []))
        if exact and departure not in exact:
            raise RoundTripSessionError("departure_date is outside the exact user date set")
        selection.update({"direction": task.get("direction"), "departure_date": departure})
    else:
        raise RoundTripSessionError("temporal selection supports joint and directional flight tasks")
    refs = raw_spec.get("basis_signal_ids", [])
    if not isinstance(refs, list) or not all(isinstance(item, str) and item.strip() for item in refs):
        raise RoundTripSessionError("basis_signal_ids must be an array of non-empty strings")
    known = {item["signal_id"]: item for item in state.get("temporal_signals", [])}
    unknown = [item for item in refs if item not in known]
    if unknown:
        raise RoundTripSessionError(f"temporal selection references unknown signals {unknown}")
    wrong_hypothesis = [item for item in refs if known[item].get("hypothesis_id") != task["hypothesis_id"]]
    if wrong_hypothesis:
        raise RoundTripSessionError(f"temporal selection references another hypothesis {wrong_hypothesis}")
    selection["basis_signal_ids"] = list(dict.fromkeys(refs))
    temporal["active_selection"] = selection
    temporal.setdefault("selection_history", []).append(copy.deepcopy(selection))
    temporal["phase"] = "exact_verification"
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def _next_hypothesis(state: dict[str, Any]) -> str | None:
    nodes = state["hypothesis_tree"]["nodes"]
    candidates = [node for node in nodes.values() if node["hypothesis_id"] != ROOT_HYPOTHESIS and node["status"] == "candidate"]
    candidates.sort(key=lambda item: (item.get("sibling_order", 10**9), item["hypothesis_id"]))
    return candidates[0]["hypothesis_id"] if candidates else None


def _unique_id(state: dict[str, Any], value: str, kind: str) -> str:
    existing = set(state["hypothesis_tree"]["nodes"]) if kind == "hypothesis" else {task["task_id"] for task in state["research_tasks"]}
    if value in existing:
        raise RoundTripSessionError(f"duplicate {kind} id {value}")
    return value


def _surface_task_specs(
    state: dict[str, Any],
    hypothesis_id: str,
    origin_gateways: list[str],
    destination_gateways: list[str],
    schedule: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    intent = state["scenario"]["intent"]
    specs = []
    legs = []
    if intent["origin_pool"].get("endpoint_policy", "anchor_required") == "anchor_required":
        legs.extend(
            [
                (
                    "origin-access-outbound",
                    intent["origin_anchor_nodes"],
                    [schedule["origin-access-outbound"]["gateway_node_id"]],
                    intent["origin_pool"],
                    "max_access_time_minutes",
                    "outbound",
                ),
                (
                    "origin-egress-return",
                    [schedule["origin-egress-return"]["gateway_node_id"]],
                    intent["origin_anchor_nodes"],
                    intent["origin_pool"],
                    "max_egress_time_minutes",
                    "return",
                ),
            ]
        )
    if intent["destination_pool"].get("endpoint_policy", "anchor_required") == "anchor_required":
        legs.extend(
            [
                (
                    "destination-egress-outbound",
                    [schedule["destination-egress-outbound"]["gateway_node_id"]],
                    intent["destination_anchor_nodes"],
                    intent["destination_pool"],
                    "max_egress_time_minutes",
                    "outbound",
                ),
                (
                    "destination-access-return",
                    intent["destination_anchor_nodes"],
                    [schedule["destination-access-return"]["gateway_node_id"]],
                    intent["destination_pool"],
                    "max_access_time_minutes",
                    "return",
                ),
            ]
        )
    for suffix, starts, goals, pool, time_key, direction in legs:
        if set(starts) & set(goals):
            continue
        specs.append(
            {
                "task_id": f"task-{hypothesis_id.removeprefix('hypothesis-')}-{suffix}",
                "research_kind": "priced_offer",
                "journey_scope": "surface",
                "question": f"Find exact scheduled and priced surface movement for {suffix.replace('-', ' ')}.",
                "query_shape": {
                    "trip_type": "surface_one_way",
                    "surface_role": suffix,
                    "origins": starts,
                    "destinations": goals,
                    "departure_window": copy.deepcopy(schedule[suffix]["departure_window"]),
                    "date_search": {
                        "mode": "exact_dates",
                        "exact_departure_dates": list(
                            schedule[suffix].get(
                                "service_dates", [schedule[suffix]["service_date"]]
                            )
                        ),
                        "selection_order": "listed",
                    },
                    "schedule_context": copy.deepcopy(schedule[suffix]),
                    "pool_constraints": {
                        "max_time_minutes": pool[time_key],
                        "max_cost": copy.deepcopy(pool["max_ground_cost"]),
                        "allowed_modes": list(pool["allowed_ground_modes"]),
                    },
                },
                "required": True,
            }
        )
    return specs


def _exact_surface_schedule(
    state: dict[str, Any], hypothesis_id: str
) -> dict[str, dict[str, str]] | None:
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    gateways = node.get("gateway_nodes", {})
    origins = set(gateways.get("origin", []))
    destinations = set(gateways.get("destination", []))
    movement_map = {item["movement_id"]: item for item in state.get("movements", [])}
    flight_movement_ids = []
    for offer in state.get("offers", []):
        if offer.get("hypothesis_id") != hypothesis_id or offer.get("journey_scope") == "surface":
            continue
        flight_movement_ids.extend(offer.get("movement_ids", []))
    flights = sorted(
        {
            movement_id: movement_map[movement_id]
            for movement_id in flight_movement_ids
            if movement_id in movement_map and movement_map[movement_id].get("mode") == "flight"
        }.values(),
        key=lambda item: item["departure"],
    )
    if not flights or not origins or not destinations:
        return None
    outbound_first = next((item for item in flights if item.get("from_node") in origins), None)
    outbound_last = next((item for item in flights if item.get("to_node") in destinations), None)
    if outbound_first is None or outbound_last is None:
        return None
    outbound_arrival = datetime.fromisoformat(outbound_last["arrival"].replace("Z", "+00:00"))
    return_first = next(
        (
            item
            for item in flights
            if item.get("from_node") in destinations
            and datetime.fromisoformat(item["departure"].replace("Z", "+00:00")) > outbound_arrival
        ),
        None,
    )
    return_last = next(
        (
            item
            for item in reversed(flights)
            if item.get("to_node") in origins
            and return_first is not None
            and datetime.fromisoformat(item["arrival"].replace("Z", "+00:00"))
            >= datetime.fromisoformat(return_first["arrival"].replace("Z", "+00:00"))
        ),
        None,
    )
    if return_first is None or return_last is None:
        return None
    intent = state["scenario"]["intent"]
    access_buffer_minutes = int(
        intent["mode_transition_buffers_minutes"].get("surface_to_flight", 120)
    )
    egress_buffer_minutes = int(
        intent["mode_transition_buffers_minutes"].get("flight_to_surface", 60)
    )

    def access_window(flight: dict[str, Any], pool: dict[str, Any]) -> dict[str, str]:
        departure = datetime.fromisoformat(flight["departure"].replace("Z", "+00:00"))
        deadline = departure - timedelta(minutes=access_buffer_minutes)
        hard_limit = pool.get("max_access_time_minutes")
        horizon = (
            int(hard_limit)
            if hard_limit is not None
            else SURFACE_DISCOVERY_HORIZON_MINUTES
        )
        start = deadline - timedelta(minutes=horizon)
        service_dates = [deadline.date().isoformat()]
        if start.date() != deadline.date():
            service_dates.append(start.date().isoformat())
        return {
            "departure_window": {"start": start.isoformat(), "end": deadline.isoformat()},
            "must_arrive_by": deadline.isoformat(),
            "service_date": service_dates[0],
            "service_dates": service_dates,
            "flight_movement_id": flight["movement_id"],
        }

    def egress_window(flight: dict[str, Any], pool: dict[str, Any]) -> dict[str, str]:
        arrival = datetime.fromisoformat(flight["arrival"].replace("Z", "+00:00"))
        start = arrival + timedelta(minutes=egress_buffer_minutes)
        hard_limit = pool.get("max_egress_time_minutes")
        horizon = (
            int(hard_limit)
            if hard_limit is not None
            else SURFACE_DISCOVERY_HORIZON_MINUTES
        )
        end = start + timedelta(minutes=horizon)
        service_dates = [start.date().isoformat()]
        if end.date() != start.date():
            service_dates.append(end.date().isoformat())
        return {
            "departure_window": {"start": start.isoformat(), "end": end.isoformat()},
            "not_before": start.isoformat(),
            "service_date": start.date().isoformat(),
            "service_dates": service_dates,
            "flight_movement_id": flight["movement_id"],
        }

    result = {
        "origin-access-outbound": access_window(outbound_first, intent["origin_pool"]),
        "destination-egress-outbound": egress_window(outbound_last, intent["destination_pool"]),
        "destination-access-return": access_window(return_first, intent["destination_pool"]),
        "origin-egress-return": egress_window(return_last, intent["origin_pool"]),
    }
    result["origin-access-outbound"]["gateway_node_id"] = outbound_first[
        "from_node"
    ]
    result["destination-egress-outbound"]["gateway_node_id"] = outbound_last[
        "to_node"
    ]
    result["destination-access-return"]["gateway_node_id"] = return_first[
        "from_node"
    ]
    result["origin-egress-return"]["gateway_node_id"] = return_last["to_node"]
    return result


def _publish_exact_surface_tasks(state: dict[str, Any], hypothesis_id: str) -> None:
    """Publish only non-Offer Internal Surface Transfer checks.

    Endpoint Access and Endpoint Egress belong to the user for new Runs and do
    not create Work Items. A discontinuity between verified flight Movements
    inside the Core Itinerary receives one external route check.
    """

    movement_map = {
        str(item.get("movement_id")): item
        for item in state.get("movements", [])
        if isinstance(item, dict) and item.get("movement_id")
    }
    flight_ids = {
        str(movement_id)
        for offer in state.get("offers", [])
        if offer.get("hypothesis_id") == hypothesis_id
        and offer.get("journey_scope") != "surface"
        for movement_id in offer.get("movement_ids", [])
        if movement_map.get(str(movement_id), {}).get("mode") == "flight"
    }
    flights = sorted(
        (movement_map[movement_id] for movement_id in flight_ids),
        key=lambda item: (item.get("departure"), item.get("arrival")),
    )
    existing_pairs = {
        (str(item.get("from_node")), str(item.get("to_node")))
        for item in state.get("internal_surface_transfers", [])
        if isinstance(item, dict)
    }
    existing_task_ids = {str(task.get("task_id")) for task in state["research_tasks"]}
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    order = 80
    for previous, following in zip(flights, flights[1:]):
        from_node = str(previous.get("to_node") or "")
        to_node = str(following.get("from_node") or "")
        if not from_node or not to_node or from_node == to_node:
            continue
        if (from_node, to_node) in existing_pairs:
            continue
        previous_arrival = datetime.fromisoformat(
            str(previous["arrival"]).replace("Z", "+00:00")
        )
        following_departure = datetime.fromisoformat(
            str(following["departure"]).replace("Z", "+00:00")
        )
        available_minutes = int(
            (following_departure - previous_arrival).total_seconds() // 60
        )
        if available_minutes <= 0:
            continue
        token = hashlib.sha256(
            f"{hypothesis_id}|{from_node}|{to_node}".encode("utf-8")
        ).hexdigest()[:12]
        task_id = f"task-internal-surface-transfer-{token}"
        if task_id in existing_task_ids:
            continue
        task = _task(
            task_id,
            hypothesis_id,
            "complete_journey",
            node.get("route_shape"),
            INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND,
            (
                f"Externally check a physical route from {from_node} to {to_node}; "
                "report only approximate duration, feasibility, and one Evidence link."
            ),
            {
                "trip_type": "internal_surface_transfer",
                "from_node": from_node,
                "to_node": to_node,
                "available_window": {
                    "start": previous["arrival"],
                    "end": following["departure"],
                },
                "available_minutes": available_minutes,
                "price_required": False,
                "taxi_or_road_fallback_allowed": True,
            },
            journey_scope="internal_surface_transfer",
            required=True,
        )
        active = state.get("active_hypothesis")
        task["status"] = (
            "pending"
            if isinstance(active, dict)
            and active.get("hypothesis_id") == hypothesis_id
            else "candidate"
        )
        task["research_order"] = order
        order += 1
        state["research_tasks"].append(task)
        existing_task_ids.add(task_id)


def _publish_frontier_positioning_tasks(
    state: dict[str, Any], hypothesis_id: str
) -> None:
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    plan = (node.get("advisory_basis") or {}).get("frontier_positioning")
    if not isinstance(plan, dict) or plan.get("mode") not in {
        "joint_round_trip",
        "split_one_ways",
    }:
        return
    source_task_id = str(plan.get("dynamic_after_task_id") or "")
    source_task = next(
        (
            task
            for task in state["research_tasks"]
            if task.get("task_id") == source_task_id
            and task.get("requirement_status") == "satisfied"
        ),
        None,
    )
    if source_task is None:
        return
    signal_id = _text(plan.get("signal_id"), "frontier_positioning.signal_id")
    token = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:12]
    task_ids = (
        {f"task-frontier-{token}-positioning"}
        if plan.get("mode") == "joint_round_trip"
        else {
            f"task-frontier-{token}-positioning-outbound",
            f"task-frontier-{token}-positioning-return",
        }
    )
    if task_ids & {task["task_id"] for task in state["research_tasks"]}:
        return
    movement_map = {
        movement["movement_id"]: movement
        for movement in state.get("movements", [])
        if isinstance(movement, dict) and movement.get("movement_id")
    }
    flight_ids = [
        movement_id
        for offer in state.get("offers", [])
        if offer.get("research_task_id") == source_task_id
        for movement_id in offer.get("movement_ids", [])
        if movement_id in movement_map
        and movement_map[movement_id].get("mode") == "flight"
    ]
    flights = [movement_map[movement_id] for movement_id in set(flight_ids)]
    outbound_hub_id = str(plan.get("outbound_hub_node_id") or "")
    return_hub_id = str(plan.get("return_hub_node_id") or "")
    buffer_minutes = int(
        state["scenario"]["intent"]["mode_transition_buffers_minutes"].get(
            "self_transfer_to_flight", 120
        )
    )
    try:
        windows = TASK_COMPATIBILITY.derive_positioning_windows(
            flights,
            outbound_hub_node_id=outbound_hub_id,
            return_hub_node_id=return_hub_id,
            connection_buffer_minutes=buffer_minutes,
            discovery_horizon_minutes=SURFACE_DISCOVERY_HORIZON_MINUTES,
        )
    except TASK_COMPATIBILITY.TaskCompatibilityError:
        return
    local_origin_ids = list(plan.get("local_origin_gateway_node_ids", []))
    if plan.get("mode") == "joint_round_trip":
        task = _task(
            f"task-frontier-{token}-positioning",
            hypothesis_id,
            "complete_journey",
            node.get("route_shape"),
            "priced_offer",
            (
                "Verify one atomic positioning round trip between the admitted "
                "origin gateway pool and the remote hub inside the exact "
                "connection windows derived from the verified long haul."
            ),
            {
                "trip_type": "round_trip",
                "origins": local_origin_ids,
                "destinations": [outbound_hub_id],
                "outbound_departure_window": copy.deepcopy(
                    windows["outbound"]["departure_window"]
                ),
                "return_departure_window": copy.deepcopy(
                    windows["return"]["departure_window"]
                ),
                "date_search": {
                    "mode": "exact_dates",
                    "outbound": {
                        "mode": "exact_dates",
                        "exact_departure_dates": list(
                            windows["outbound"]["exact_departure_dates"]
                        ),
                    },
                    "return": {
                        "mode": "exact_dates",
                        "exact_departure_dates": list(
                            windows["return"]["exact_departure_dates"]
                        ),
                    },
                    "atomic_total_required": True,
                },
                "schedule_context": {
                    "outbound": copy.deepcopy(
                        windows["outbound"]["schedule_context"]
                    ),
                    "return": copy.deepcopy(
                        windows["return"]["schedule_context"]
                    ),
                },
                "connects_to_frontier_signal_id": signal_id,
            },
            journey_scope="joint_round_trip",
            required=True,
        )
        active = state.get("active_hypothesis")
        task["status"] = (
            "pending"
            if isinstance(active, dict)
            and active.get("hypothesis_id") == hypothesis_id
            else "candidate"
        )
        task["research_order"] = 2
        state["research_tasks"].append(task)
        plan["published_with_challenger"] = True
        return

    specs = [
        {
            "task_id": f"task-frontier-{token}-positioning-outbound",
            "direction": "outbound",
            "question": (
                "Verify one priced positioning flight from the admitted origin "
                "gateway pool to the remote outbound hub before the long-haul departure."
            ),
            "origins": local_origin_ids,
            "destinations": [outbound_hub_id],
            "window": windows["outbound"]["departure_window"],
            "dates": windows["outbound"]["exact_departure_dates"],
            "schedule_context": windows["outbound"]["schedule_context"],
        },
        {
            "task_id": f"task-frontier-{token}-positioning-return",
            "direction": "return",
            "question": (
                "Verify one priced positioning flight from the remote return hub "
                "to the admitted origin gateway pool after the long-haul arrival."
            ),
            "origins": [return_hub_id],
            "destinations": local_origin_ids,
            "window": windows["return"]["departure_window"],
            "dates": windows["return"]["exact_departure_dates"],
            "schedule_context": windows["return"]["schedule_context"],
        },
    ]
    for order, spec in enumerate(specs, start=2):
        task = _task(
            spec["task_id"],
            hypothesis_id,
            "complete_journey",
            node.get("route_shape"),
            "priced_offer",
            spec["question"],
            {
                "trip_type": "one_way",
                "direction": spec["direction"],
                "origins": spec["origins"],
                "destinations": spec["destinations"],
                "departure_window": spec["window"],
                "coverage_departure_window": spec["window"],
                "date_search": {
                    "mode": "exact_dates",
                    "exact_departure_dates": spec["dates"],
                    "selection_order": "listed",
                },
                "schedule_context": spec["schedule_context"],
                "connects_to_frontier_signal_id": signal_id,
            },
            journey_scope="one_way",
            direction=spec["direction"],
            required=True,
        )
        active = state.get("active_hypothesis")
        task["status"] = (
            "pending"
            if isinstance(active, dict)
            and active.get("hypothesis_id") == hypothesis_id
            else "candidate"
        )
        task["research_order"] = order
        state["research_tasks"].append(task)
    plan["published_with_challenger"] = True


def add_route_hypothesis(raw_state: Any, raw_spec: Any) -> dict[str, Any]:
    state = validate_session(raw_state)
    if not isinstance(raw_spec, dict):
        raise RoundTripSessionError("hypothesis spec must be an object")
    hypothesis_id = _unique_id(state, _text(raw_spec.get("hypothesis_id"), "hypothesis_id"), "hypothesis")
    route_shape = raw_spec.get("route_shape", "closed_airport_pair")
    if route_shape not in {"closed_airport_pair", "regional_open_jaw", "nested_round_trips", "mixed_atomic_portfolio", "custom_complete_journey"}:
        raise RoundTripSessionError("unsupported route_shape")
    origin_gateways = list(raw_spec.get("origin_gateway_nodes", []))
    destination_gateways = list(raw_spec.get("destination_gateway_nodes", []))
    known_gateway_ids = {
        candidate["node"]["node_id"]
        for side in state["gateway_candidates"].values()
        for candidate in side
        if candidate.get("evidence_status") in {"research_observed", "exact_anchor"}
    }
    if not origin_gateways or not destination_gateways or not set(origin_gateways + destination_gateways) <= known_gateway_ids:
        raise RoundTripSessionError("route hypothesis gateways must be admitted origin/destination candidates")
    order = max(node.get("sibling_order", 0) for node in state["hypothesis_tree"]["nodes"].values()) + 1
    node = _tree_node(hypothesis_id, "complete_journey", route_shape, order)
    node["geographic_cell"] = raw_spec.get("geographic_cell")
    node["gateway_nodes"] = {"origin": origin_gateways, "destination": destination_gateways}
    node["advisory_basis"] = copy.deepcopy(raw_spec.get("advisory_basis"))
    node["research_sequence"] = copy.deepcopy(raw_spec.get("research_sequence", []))
    node["temporal_strategy"] = raw_spec.get("temporal_strategy", "ai_led_date_frontier")
    node["joint_baseline_reason"] = raw_spec.get("joint_baseline_reason")
    state["hypothesis_tree"]["nodes"][hypothesis_id] = node
    intent = state["scenario"]["intent"]
    task_specs = raw_spec.get("task_specs")
    if task_specs is None:
        screen_candidate = raw_spec.get("screen_candidate")
        date_search = _joint_date_search(intent)
        if isinstance(screen_candidate, dict):
            screen_dates = screen_candidate.get("itinerary_dates", {})
            date_search = {
                "mode": "exact_dates",
                "outbound": {
                    "mode": "exact_dates",
                    "exact_departure_dates": [
                        screen_dates.get("outbound_departure_local_date")
                    ],
                },
                "return": {
                    "mode": "exact_dates",
                    "exact_departure_dates": [
                        screen_dates.get("return_departure_local_date")
                    ],
                },
                "atomic_total_required": True,
                "rule": (
                    "Verify the exact date pair selected from the admitted "
                    "regional fare signal."
                ),
            }
        task_specs = [
            {
                "task_id": f"task-{hypothesis_id.removeprefix('hypothesis-')}-joint",
                "research_kind": "priced_offer",
                "journey_scope": "joint_round_trip",
                "question": "Research multiple useful joint date-pair signals in the allowed windows, then verify payable atomic round-trip quotes selected by the AI for this gateway hypothesis.",
                "query_shape": {
                    "trip_type": "round_trip",
                    "origins": origin_gateways,
                    "destinations": destination_gateways,
                    "outbound_departure_window": intent["outbound_departure_window"],
                    "return_departure_window": intent["return_departure_window"],
                    "date_search": date_search,
                    **(
                        {
                            "travel_window_id": screen_candidate.get(
                                "travel_window_id"
                            )
                        }
                        if isinstance(screen_candidate, dict)
                        and screen_candidate.get("travel_window_id")
                        else {}
                    ),
                    **(
                        {"screen_candidate": copy.deepcopy(raw_spec["screen_candidate"])}
                        if raw_spec.get("screen_candidate") is not None
                        else {}
                    ),
                },
                "required": True,
            },
        ]
        advisory_basis = raw_spec.get("advisory_basis")
        if isinstance(advisory_basis, dict) and advisory_basis.get(
            "split_verification_indicated"
        ) is True:
            task_specs.extend(
                [
                    {
                        "task_id": f"task-{hypothesis_id.removeprefix('hypothesis-')}-outbound",
                        "research_kind": "priced_offer",
                        "journey_scope": "one_way",
                        "direction": "outbound",
                        "question": "Research multiple useful outbound date signals, then verify payable outbound one-way quotes selected by the AI for this gateway hypothesis.",
                        "query_shape": {"trip_type": "one_way", "direction": "outbound", "origins": origin_gateways, "destinations": destination_gateways, "departure_window": intent["outbound_departure_window"], "date_search": _date_search(intent, "outbound")},
                        "required": True,
                    },
                    {
                        "task_id": f"task-{hypothesis_id.removeprefix('hypothesis-')}-return",
                        "research_kind": "priced_offer",
                        "journey_scope": "one_way",
                        "direction": "return",
                        "question": "Research multiple useful return date signals, then verify payable return one-way quotes selected by the AI for this gateway hypothesis.",
                        "query_shape": {"trip_type": "one_way", "direction": "return", "origins": destination_gateways, "destinations": origin_gateways, "departure_window": intent["return_departure_window"], "date_search": _date_search(intent, "return")},
                        "required": True,
                    },
                ]
            )
    if not isinstance(task_specs, list) or not task_specs:
        raise RoundTripSessionError("task_specs must be a non-empty array")
    for index, spec in enumerate(task_specs):
        if not isinstance(spec, dict):
            raise RoundTripSessionError(f"task_specs[{index}] must be an object")
        task_id = _unique_id(state, _text(spec.get("task_id"), f"task_specs[{index}].task_id"), "task")
        journey_scope = spec.get("journey_scope")
        if journey_scope not in SOLVER.OFFER_SCOPES:
            raise RoundTripSessionError(f"task_specs[{index}].journey_scope is unsupported")
        if journey_scope == "surface" and not isinstance(spec.get("query_shape", {}).get("pool_constraints"), dict):
            raise RoundTripSessionError(f"task_specs[{index}] surface query requires pool_constraints")
        new_task = _task(
                task_id,
                hypothesis_id,
                "complete_journey",
                route_shape,
                spec.get("research_kind", "priced_offer"),
                _text(spec.get("question"), f"task_specs[{index}].question"),
                spec.get("query_shape"),
                journey_scope=journey_scope,
                direction=spec.get("direction"),
                required=spec.get("required", True),
            )
        new_task["research_order"] = index + 1
        state["research_tasks"].append(new_task)
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def activate_route_hypothesis(
    raw_state: Any, raw_spec: Any, reason: str
) -> dict[str, Any]:
    """Register and select one complete-journey hypothesis."""

    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    if state.get("active_hypothesis") is not None:
        raise RoundTripSessionError(
            "route activation requires no active hypothesis"
        )
    if not isinstance(raw_spec, dict):
        raise RoundTripSessionError("route activation requires a hypothesis spec")
    hypothesis_id = _text(raw_spec.get("hypothesis_id"), "hypothesis_id")
    if hypothesis_id not in state["hypothesis_tree"]["nodes"]:
        state = validate_session(add_route_hypothesis(state, raw_spec))
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id)
    if not isinstance(node, dict) or node.get("status") != "candidate":
        raise RoundTripSessionError(
            "route activation requires a candidate complete-journey hypothesis"
        )
    actionable = [
        task
        for task in state["research_tasks"]
        if task.get("hypothesis_id") == hypothesis_id
        and task.get("requirement_status") == "open"
    ]
    joint = [
        task
        for task in actionable
        if task.get("journey_scope") == "joint_round_trip"
    ]
    selected_task = min(
        joint or actionable,
        key=lambda task: (
            int(task.get("research_order", 10**9)),
            str(task.get("task_id")),
        ),
        default=None,
    )
    if selected_task is None:
        raise RoundTripSessionError("route hypothesis has no actionable research task")
    return record_hypothesis_decision(
        state,
        "select",
        task_id=str(selected_task["task_id"]),
        reason=reason,
    )


def _supporting_portfolio_exists(state: dict[str, Any], hypothesis_id: str) -> bool:
    tasks = [
        task
        for task in state["research_tasks"]
        if task["hypothesis_id"] == hypothesis_id
    ]
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    strict = bool(
        (node.get("advisory_basis") or {}).get("requires_all_payable_tasks")
    )
    if not strict:
        task_ids = {task["task_id"] for task in tasks}
        return any(
            task_ids & set(portfolio.get("evidence_task_ids", []))
            for portfolio in state["result"]["portfolios"]
        )
    required_task_ids = {
        task["task_id"]
        for task in tasks
        if task.get("required", True)
        and task.get("research_kind") == "priced_offer"
    }
    if not required_task_ids or any(
        task.get("requirement_status") != "satisfied"
        for task in tasks
        if task.get("task_id") in required_task_ids
    ):
        return False
    offer_task_ids = {
        str(offer.get("offer_id")): str(offer.get("research_task_id"))
        for offer in state.get("offers", [])
        if offer.get("offer_id") and offer.get("research_task_id")
    }
    for portfolio in state["result"]["portfolios"]:
        evidence_sets = [set(portfolio.get("evidence_task_ids", []))]
        for variant in portfolio.get("evidence_variants", []):
            if not isinstance(variant, dict):
                continue
            task_ids = set(variant.get("evidence_task_ids", []))
            if not task_ids:
                task_ids = {
                    offer_task_ids[str(offer_id)]
                    for offer_id in variant.get("offer_ids", [])
                    if str(offer_id) in offer_task_ids
                }
            evidence_sets.append(task_ids)
        if any(required_task_ids <= task_ids for task_ids in evidence_sets):
            return True
    return False


def assess_price_refresh_resolution(
    raw_state: Any, hypothesis_id: str
) -> dict[str, Any]:
    """Assess an exact refresh without mutating the persisted Session."""

    state = validate_session(raw_state)
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    advisory_basis = node.get("advisory_basis") or {}
    if advisory_basis.get("price_refresh") is not True:
        return {"disposition": "not_applicable", "reason": "not a price refresh"}
    tasks = [
        task
        for task in state["research_tasks"]
        if task.get("hypothesis_id") == hypothesis_id
        and task.get("research_kind") == "priced_offer"
        and task.get("required", True)
    ]
    if any(task.get("requirement_status") == "open" for task in tasks):
        return {
            "disposition": "needs_work",
            "reason": "required refresh Work Items remain open",
        }
    target_ids = set(advisory_basis.get("refresh_offer_ids") or [])
    replacement_target_ids = {
        str(offer.get("refresh_of_offer_id"))
        for offer in state.get("offers", [])
        if offer.get("hypothesis_id") == hypothesis_id
        and offer.get("refresh_of_offer_id")
    }
    missing_ids = target_ids - replacement_target_ids
    exhausted_target_ids = {
        str(target.get("offer_id"))
        for task in tasks
        if task.get("requirement_status") == "exhausted"
        for target in task.get("query_shape", {})
        .get("price_refresh", {})
        .get("refresh_targets", [])
        if isinstance(target, dict) and target.get("offer_id")
    }
    bundle_map = {
        str(bundle.get("bundle_id")): bundle
        for bundle in state.get("evidence_bundles", [])
        if isinstance(bundle, dict) and bundle.get("bundle_id")
    }
    closed_listing_target_ids = {
        str(target.get("offer_id"))
        for task in tasks
        if task.get("requirement_status") == "satisfied"
        and any(
            isinstance(observation.get("seller_listing"), dict)
            and isinstance(
                observation["seller_listing"].get("terminal_witness"), dict
            )
            and observation["seller_listing"]["terminal_witness"].get(
                "next_page_available"
            )
            is False
            for bundle_id in task.get("evidence_bundle_ids", [])
            for observation in bundle_map.get(str(bundle_id), {}).get(
                "observations", []
            )
            if isinstance(observation, dict)
        )
        for target in task.get("query_shape", {})
        .get("price_refresh", {})
        .get("refresh_targets", [])
        if isinstance(target, dict) and target.get("offer_id")
    }
    unavailable_target_ids = exhausted_target_ids | closed_listing_target_ids
    if missing_ids - unavailable_target_ids:
        return {
            "disposition": "blocked",
            "reason": "refresh targets lack current replacement or exact unavailability Evidence",
            "missing_offer_ids": sorted(missing_ids),
        }
    try:
        candidate = PROJECTION.retire_offers(
            state,
            sorted(target_ids),
            "Exact refresh resolution atomically retired stale target Offers.",
        )
        _recompute(candidate)
    except (PROJECTION.SolverProjectionError, RoundTripSessionError) as exc:
        return {"disposition": "blocked", "reason": str(exc)}
    if candidate["result"].get("best_portfolio") is None:
        return {
            "disposition": "blocked",
            "reason": "refresh retirement leaves no complete current Portfolio",
            "missing_offer_ids": sorted(missing_ids),
        }
    if missing_ids:
        return {
            "disposition": "unavailable_fallback",
            "reason": "unavailable targets can retire while a current complete fallback remains",
            "missing_offer_ids": sorted(missing_ids),
        }
    return {
        "disposition": "ready_to_commit",
        "reason": (
            "all exact refresh targets preserve their Ticketing Structures and "
            "the refreshed projection retains a complete current Portfolio"
        ),
    }


def record_hypothesis_decision(raw_state: Any, action: str, *, task_id: str | None = None, hypothesis_id: str | None = None, reason: str) -> dict[str, Any]:
    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    if action == "select":
        if state["active_hypothesis"] is not None:
            raise RoundTripSessionError("active hypothesis must be closed or paused before selecting another")
        if task_id is None:
            raise RoundTripSessionError("select requires task_id")
        task = _task_by_id(state, task_id)
        hypothesis_id = task["hypothesis_id"]
        node = state["hypothesis_tree"]["nodes"][hypothesis_id]
        if node["status"] != "candidate" or task.get("requirement_status") != "open":
            raise RoundTripSessionError("selected hypothesis/task is not actionable")
        node["status"] = "selected"
        node["selected_at_round"] = state["search_round"]
        node["was_selected"] = True
        for item in state["research_tasks"]:
            if item["hypothesis_id"] == hypothesis_id and item["status"] == "candidate":
                item["status"] = "pending"
        state["active_hypothesis"] = {"hypothesis_id": hypothesis_id, "selected_task_id": task_id, "reason": reason}
    elif action == "pause":
        active = state["active_hypothesis"]
        if active is None:
            raise RoundTripSessionError("pause requires an active hypothesis")
        hypothesis_id = active["hypothesis_id"]
        state["hypothesis_tree"]["nodes"][hypothesis_id]["status"] = "candidate"
        state["active_hypothesis"] = None
    elif action in TERMINAL_HYPOTHESIS_STATUSES:
        active = state["active_hypothesis"]
        if active is None:
            raise RoundTripSessionError(f"{action} requires an active hypothesis")
        hypothesis_id = active["hypothesis_id"]
        node = state["hypothesis_tree"]["nodes"][hypothesis_id]
        if (
            action != "complete"
            and (node.get("advisory_basis") or {}).get("price_refresh") is True
        ):
            raise RoundTripSessionError(
                "price refresh cannot be closed without committing replacements "
                "or an exact unavailable fallback"
            )
        if action == "complete":
            if node["hypothesis_kind"] == "gateway_discovery":
                side = "origin" if hypothesis_id == ORIGIN_GATEWAY_HYPOTHESIS else "destination"
                scope = state["region_scopes"][side]
                if scope.get("status") not in {"not_required", "defined", "legacy_complete"}:
                    raise RoundTripSessionError(
                        "gateway hypothesis complete requires an AI-defined region scope"
                    )
                admitted = [
                    candidate
                    for candidate in state["gateway_candidates"][side]
                    if candidate.get("evidence_status")
                    in {"ai_proposed", "research_observed", "exact_anchor"}
                ]
                if not admitted:
                    raise RoundTripSessionError(
                        "gateway hypothesis complete requires a defined or exact candidate"
                    )
            else:
                advisory_basis = node.get("advisory_basis") or {}
                price_refresh = advisory_basis.get("price_refresh") is True
                if advisory_basis.get("requires_all_payable_tasks") and not price_refresh:
                    unsatisfied_required = [
                        task["task_id"]
                        for task in state["research_tasks"]
                        if task["hypothesis_id"] == hypothesis_id
                        and task.get("required", True)
                        and task.get("research_kind") == "priced_offer"
                        and task.get("requirement_status") != "satisfied"
                    ]
                    if unsatisfied_required:
                        raise RoundTripSessionError(
                            "complete journey hypothesis has unsatisfied required "
                            f"Evidence {unsatisfied_required}"
                        )
                unfinished_required = [
                    task["task_id"]
                    for task in state["research_tasks"]
                    if task["hypothesis_id"] == hypothesis_id
                    and task.get("required", True)
                    and task.get("requirement_status") == "open"
                ]
                if unfinished_required:
                    raise RoundTripSessionError(
                        f"complete journey hypothesis has unfinished required research {unfinished_required}"
                    )
                if price_refresh:
                    resolution = assess_price_refresh_resolution(
                        state, hypothesis_id
                    )
                    if resolution.get("disposition") not in {
                        "ready_to_commit",
                        "unavailable_fallback",
                    }:
                        raise RoundTripSessionError(
                            "price refresh cannot commit: "
                            + str(resolution.get("reason") or "unresolved")
                        )
                    refresh_offer_ids = set(
                        advisory_basis.get("refresh_offer_ids") or []
                    )
                    try:
                        state = PROJECTION.retire_offers(
                            state,
                            sorted(refresh_offer_ids),
                            (
                                "The exact refresh atomically replaced or resolved "
                                "unavailable stale Offers."
                            ),
                        )
                        _recompute(state)
                        node = state["hypothesis_tree"]["nodes"][hypothesis_id]
                    except PROJECTION.SolverProjectionError as exc:
                        raise RoundTripSessionError(str(exc)) from exc
                    if state["result"].get("best_portfolio") is None:
                        raise RoundTripSessionError(
                            "price refresh retirement removed every complete Portfolio"
                        )
                elif not _supporting_portfolio_exists(state, hypothesis_id):
                    raise RoundTripSessionError(
                        "complete journey hypothesis requires a supporting complete portfolio"
                    )
        node["status"] = action
        node["terminal_reason"] = reason
        node["terminal_at_round"] = state["search_round"]
        for task in state["research_tasks"]:
            if task["hypothesis_id"] == hypothesis_id and task.get("requirement_status") == "open":
                _set_requirement_state(task, "superseded")
                task["status"] = "superseded"
                task["terminal_reason"] = reason
        state["active_hypothesis"] = None
        if action == "complete" and node.get("hypothesis_kind") == "complete_journey":
            for side in ("origin", "destination"):
                gateway_id = f"hypothesis-gateway-{side}"
                gateway_node = state["hypothesis_tree"]["nodes"].get(gateway_id)
                gateway_tasks = [
                    task
                    for task in state["research_tasks"]
                    if task.get("hypothesis_id") == gateway_id
                    and task.get("required", True)
                ]
                if (
                    isinstance(gateway_node, dict)
                    and gateway_node.get("status") == "candidate"
                    and gateway_tasks
                    and not any(
                        task.get("requirement_status") == "open"
                        for task in gateway_tasks
                    )
                    and any(
                        candidate.get("evidence_status")
                        in {"research_observed", "exact_anchor"}
                        for candidate in state["gateway_candidates"][side]
                    )
                ):
                    gateway_node["status"] = "complete"
                    gateway_node["terminal_reason"] = (
                        "A verified Journey Alternative now anchors the bounded "
                        "Gateway search."
                    )
                    gateway_node["terminal_at_round"] = state["search_round"]
    else:
        raise RoundTripSessionError("unsupported hypothesis action")
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    _recompute(state)
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def _merge_unique(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    result = copy.deepcopy(existing)
    index = {item[identity]: item for item in result}
    for item in incoming:
        key = item[identity]
        if key in index and index[key] != item:
            raise RoundTripSessionError(f"conflicting {identity} {key}")
        if key not in index:
            result.append(copy.deepcopy(item))
            index[key] = result[-1]
    return result


def _publish_region_pool_fare_screen_if_ready(
    state: dict[str, Any], owner_side: str
) -> None:
    ready_statuses = {"defined", "not_required", "legacy_complete"}
    scopes = state.get("region_scopes", {})
    if any(scopes.get(side, {}).get("status") not in ready_statuses for side in ("origin", "destination")):
        return
    origin_records = scopes["origin"].get("candidate_records", [])
    destination_records = scopes["destination"].get("candidate_records", [])
    if not origin_records or not destination_records:
        return
    if (
        len(origin_records) == 1
        and len(destination_records) == 1
        and scopes["origin"].get("status") == "not_required"
        and scopes["destination"].get("status") == "not_required"
    ):
        return
    hypothesis_id = f"hypothesis-gateway-{owner_side}"
    root_spec = AI_EXPLORATION.region_pool_fare_screen_spec(
        scopes["origin"], scopes["destination"], state["scenario"]
    )
    root_spec["query_shape"]["minimum_savings_ratio"] = float(
        state.get("research_credits", {}).get(
            "price_challenge_threshold", 0.05
        )
    )
    travel_windows = state["scenario"]["intent"].get("travel_windows") or [
        {
            "travel_window_id": "travel-window-primary",
            "outbound_departure_window": root_spec["query_shape"][
                "outbound_departure_window"
            ],
            "return_departure_window": root_spec["query_shape"][
                "return_departure_window"
            ],
            "minimum_stay_nights": root_spec["query_shape"].get(
                "minimum_stay_nights"
            ),
            "maximum_stay_nights": root_spec["query_shape"].get(
                "maximum_stay_nights"
            ),
        }
    ]
    for travel_window in travel_windows:
        query = copy.deepcopy(root_spec["query_shape"])
        query.update(
            {
                "travel_window_id": travel_window["travel_window_id"],
                "outbound_departure_window": copy.deepcopy(
                    travel_window["outbound_departure_window"]
                ),
                "return_departure_window": copy.deepcopy(
                    travel_window["return_departure_window"]
                ),
                "minimum_stay_nights": travel_window.get(
                    "minimum_stay_nights"
                ),
                "maximum_stay_nights": travel_window.get(
                    "maximum_stay_nights"
                ),
            }
        )
        try:
            plan = SCOPE_COMPILER.compile_discovery_scope(
                query,
                destination_search_geography=state["scenario"]["intent"]
                .get("destination_pool", {})
                .get("search_geography"),
            )
        except SCOPE_COMPILER.ScopeCompilerError as exc:
            raise RoundTripSessionError(str(exc)) from exc
        _append_compiled_scope_tasks(state, hypothesis_id, plan)


def _append_compiled_scope_tasks(
    state: dict[str, Any], hypothesis_id: str, plan: dict[str, Any]
) -> None:
    existing = {str(task.get("task_id")) for task in state["research_tasks"]}
    active = state.get("active_hypothesis")
    for sequence, scope_slice in enumerate(plan["slices"], start=1):
        spec = SCOPE_COMPILER.slice_task_spec(scope_slice, sequence)
        if spec["task_id"] in existing:
            continue
        task = _task(
            spec["task_id"],
            hypothesis_id,
            "gateway_discovery",
            "regional_airport_pool_fare_screen",
            spec["research_kind"],
            spec["question"],
            spec["query_shape"],
        )
        task["scope_plan_id"] = plan["scope_plan_id"]
        task["scope_slice_id"] = scope_slice["scope_slice_id"]
        task["scope_partition"] = _scope_partition(task)
        task["scope_execution"] = {
            "status": "unsearched",
            "provider_scopes": [],
        }
        if isinstance(active, dict) and active.get("hypothesis_id") == hypothesis_id:
            task["status"] = "pending" if sequence == 1 else "candidate"
        state["research_tasks"].append(task)
        existing.add(spec["task_id"])


def _publish_region_pool_fallback(
    state: dict[str, Any], parent_task: dict[str, Any]
) -> None:
    """Lazily expand one unsupported Provider-native probe into rectangles."""

    query = copy.deepcopy(parent_task.get("query_shape", {}))
    for field in (
        "scope_plan_id",
        "scope_slice_id",
        "scope_strategy",
        "destination_search_geography",
        "required_view",
        "required_browser_capabilities",
        "date_rows",
    ):
        query.pop(field, None)
    query["trip_type"] = "region_pool_fare_screen"
    try:
        plan = SCOPE_COMPILER.compile_region_pool_scope(query)
    except SCOPE_COMPILER.ScopeCompilerError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    _append_compiled_scope_tasks(
        state,
        str(parent_task["hypothesis_id"]),
        plan,
    )


def _apply_region_scope_proposal(
    state: dict[str, Any], task: dict[str, Any], proposal: dict[str, Any]
) -> None:
    side = str(task["query_shape"]["side"])
    try:
        normalized = AI_EXPLORATION.normalize_region_scope_proposal(
            proposal, state["scenario"], side
        )
    except AI_EXPLORATION.AIExplorationError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    old_scope = state["region_scopes"][side]
    normalized["proposal_history"] = [
        *copy.deepcopy(old_scope.get("proposal_history", [])),
        {
            "at": _now(),
            "candidate_node_ids": [
                item["node"]["node_id"] for item in normalized["candidate_records"]
            ],
            "excluded_seed_node_ids": [
                item["node_id"] for item in normalized["excluded_seed_nodes"]
            ],
            "scope_reason": normalized["scope_reason"],
        },
    ]
    state["region_scopes"][side] = normalized
    node_map = {
        _node_id(node, "scenario.nodes[]"): node for node in state["scenario"]["nodes"]
    }
    ai_candidates = []
    for record in normalized["candidate_records"]:
        node = record["node"]
        node_id = node["node_id"]
        if node_id in node_map:
            try:
                node = IDENTITY.reconcile_node(node_map[node_id], node)
            except IDENTITY.CanonicalValueError as exc:
                raise RoundTripSessionError(str(exc)) from exc
            record["node"] = copy.deepcopy(node)
        else:
            state["scenario"]["nodes"].append(copy.deepcopy(node))
            node_map[node_id] = node
        ai_candidates.append(
            {
                "candidate_id": record["candidate_id"],
                "side": side,
                "node": copy.deepcopy(node),
                "signal": {
                    "kind": "ai_region_scope",
                    "roles": list(record["roles"]),
                    "inclusion_reason": record["inclusion_reason"],
                },
                "evidence_status": "ai_proposed",
            }
        )
    normalized["candidate_nodes"] = [
        copy.deepcopy(record["node"])
        for record in normalized["candidate_records"]
    ]
    state["gateway_candidates"][side] = _merge_unique(
        state["gateway_candidates"][side], ai_candidates, "candidate_id"
    )
    _publish_region_pool_fare_screen_if_ready(state, side)


def _derive_offer_sets(offers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    offer_sets: dict[str, dict[str, Any]] = {}
    for offer in offers:
        itinerary_id = offer.get("itinerary_id")
        if not itinerary_id:
            continue
        offer_set = offer_sets.setdefault(
            itinerary_id, {"itinerary_id": itinerary_id, "offers": []}
        )
        offer_set["offers"].append(
            {
                "offer_id": offer["offer_id"],
                "provider_offer_id": offer["provider_offer_id"],
                "source_id": offer["source_id"],
                "provider": offer["provider"],
                "seller": offer["seller"],
                "observed_at": offer["observed_at"],
                "offer_snapshot_id": offer.get("offer_snapshot_id"),
            }
        )
    return offer_sets


def _recompute(state: dict[str, Any]) -> None:
    scenario = state["scenario"]
    solver_intent = copy.deepcopy(scenario["intent"])
    for side in ("origin", "destination"):
        endpoint_ids = sorted(
            {
                str(candidate.get("node", {}).get("node_id"))
                for candidate in state.get("gateway_candidates", {}).get(side, [])
                if candidate.get("node", {}).get("node_id")
                and candidate.get("candidate_scope")
                not in {"portfolio_frontier", "price_challenge"}
                and candidate.get("evidence_status")
                in {"research_observed", "exact_anchor"}
            }
        )
        if endpoint_ids:
            solver_intent[f"{side}_endpoint_nodes"] = endpoint_ids
    try:
        projection = PROJECTION.build_projection(state)
    except PROJECTION.SolverProjectionError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    state["solver_projection"] = {
        key: copy.deepcopy(value)
        for key, value in projection.items()
        if key not in {"offers", "movements"}
    }
    bundle = {
        "schema_version": SOLVER.VERSION,
        "base_currency": scenario["base_currency"],
        "intent": solver_intent,
        "nodes": scenario["nodes"],
        "transport_entitlements": scenario.get("transport_entitlements", []),
        "fx_snapshot": scenario.get("fx_snapshot"),
        "evaluation_at": _now(),
        "movements": projection["movements"],
        "offers": projection["offers"],
        "internal_surface_transfers": copy.deepcopy(
            state.get("internal_surface_transfers", [])
        ),
    }
    try:
        state["result"] = SOLVER.solve_bundle(bundle)
    except SOLVER.RoundTripError as exc:
        raise RoundTripSessionError(str(exc)) from exc


def _refresh_time_sensitive_state(state: dict[str, Any]) -> bool:
    """Refresh solver projections whose validity depends on evaluation time."""

    before = copy.deepcopy(state)
    for _ in range(4):
        previous = copy.deepcopy(state)
        _recompute(state)
        if state == previous:
            break
    else:
        raise RoundTripSessionError(
            "time-sensitive solver projection did not reach a fixed point"
        )
    if state == before:
        return False
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return True


def _scope_partition(task: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable Provider-visible partition owned by one Work Item."""

    query = task.get("query_shape", {})
    partition_id = str(
        task.get("scope_slice_id")
        or query.get("scope_slice_id")
        or task.get("task_id")
        or ""
    )
    return {
        "partition_id": partition_id,
        "task_id": task.get("task_id"),
        "scope_plan_id": task.get("scope_plan_id")
        or query.get("scope_plan_id"),
        "scope_strategy": query.get("scope_strategy", "airport_rectangle"),
        "travel_window_id": query.get("travel_window_id"),
        "origin_gateway_node_ids": copy.deepcopy(
            query.get("origin_gateway_node_ids", [])
        ),
        "destination_gateway_node_ids": copy.deepcopy(
            query.get("destination_gateway_node_ids", [])
        ),
        "outbound_departure_window": copy.deepcopy(
            query.get("outbound_departure_window")
        ),
        "return_departure_window": copy.deepcopy(
            query.get("return_departure_window")
        ),
        "date_rows": copy.deepcopy(query.get("date_rows", [])),
        "minimum_stay_nights": query.get("minimum_stay_nights"),
        "maximum_stay_nights": query.get("maximum_stay_nights"),
    }


def _scope_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Project semantic scope separately from executed and unproved partitions."""

    executed = []
    unsearched = []
    unsupported = []
    for task in state.get("research_tasks", []):
        if task.get("research_kind") != "region_pool_fare_screen":
            continue
        partition = copy.deepcopy(
            task.get("scope_partition")
            if isinstance(task.get("scope_partition"), dict)
            else _scope_partition(task)
        )
        execution = (
            task.get("scope_execution")
            if isinstance(task.get("scope_execution"), dict)
            else {"status": "unsearched", "provider_scopes": []}
        )
        status = str(execution.get("status") or "unsearched")
        value = {
            **partition,
            "execution_status": status,
            "requirement_status": task.get("requirement_status"),
            "evidence_disposition": task.get("evidence_disposition"),
        }
        if status == "executed":
            value["provider_scopes"] = copy.deepcopy(
                execution.get("provider_scopes", [])
            )
            value["evidence_bundle_ids"] = copy.deepcopy(
                task.get("evidence_bundle_ids", [])
            )
            executed.append(value)
        elif status == "fallback_required":
            unsupported.append(value)
        else:
            unsearched.append(value)

    coverage = {
        side: copy.deepcopy(
            state.get("region_scopes", {}).get(side, {}).get("coverage", {})
        )
        for side in ("origin", "destination")
    }
    return {
        "semantic_self_access_gateway_pool": {
            side: [
                copy.deepcopy(record.get("node"))
                for record in state.get("region_scopes", {})
                .get(side, {})
                .get("candidate_records", [])
                if isinstance(record, dict)
            ]
            for side in ("origin", "destination")
        },
        "requested_travel_windows": copy.deepcopy(
            state.get("scenario", {}).get("intent", {}).get(
                "travel_windows", []
            )
        ),
        "gateway_coverage": coverage,
        "provider_visible_partitions": executed,
        "unsearched_partitions": unsearched,
        "unsupported_provider_partitions": unsupported,
        "unproved_gateway_node_ids": {
            side: sorted(
                node_id
                for node_id, status in coverage[side].items()
                if status not in TERMINAL_GATEWAY_COVERAGE
            )
            for side in ("origin", "destination")
        },
    }


def _scope_window_dates(raw: Any, field: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RoundTripSessionError(f"{field} must be an object")
    try:
        start = datetime.fromisoformat(
            str(raw.get("start")).replace("Z", "+00:00")
        ).date()
        end = datetime.fromisoformat(
            str(raw.get("end")).replace("Z", "+00:00")
        ).date()
    except ValueError as exc:
        raise RoundTripSessionError(f"{field} must contain ISO dates") from exc
    if start > end:
        raise RoundTripSessionError(f"{field}.start must not be after end")
    return {"start": start.isoformat(), "end": end.isoformat()}


def _pool_scope_pairs(
    task: dict[str, Any], provider_scope: dict[str, Any]
) -> set[tuple[str, str]]:
    query_shape = task.get("query_shape", {})
    required_origins = set(query_shape.get("origin_gateway_node_ids", []))
    required_destinations = set(
        query_shape.get("destination_gateway_node_ids", [])
    )
    origins = set(provider_scope.get("origin_gateway_node_ids", []))
    destinations = set(provider_scope.get("destination_gateway_node_ids", []))
    if not origins or not destinations:
        raise RoundTripSessionError(
            "region pool Provider scope requires non-empty airport pools"
        )
    if not origins <= required_origins or not destinations <= required_destinations:
        raise RoundTripSessionError(
            "region pool Provider scope exceeds the requested airport pools"
        )
    raw_outbound = _scope_window_dates(
        query_shape.get("outbound_departure_window"),
        "query_shape.outbound_departure_window",
    )
    raw_return = _scope_window_dates(
        query_shape.get("return_departure_window"),
        "query_shape.return_departure_window",
    )
    observed_outbound = _scope_window_dates(
        provider_scope.get("outbound_departure_window"),
        "provider_scope.outbound_departure_window",
    )
    observed_return = _scope_window_dates(
        provider_scope.get("return_departure_window"),
        "provider_scope.return_departure_window",
    )
    minimum_stay = query_shape.get("minimum_stay_nights")
    maximum_stay = query_shape.get("maximum_stay_nights")
    outbound_start = datetime.fromisoformat(raw_outbound["start"]).date()
    outbound_end = datetime.fromisoformat(raw_outbound["end"]).date()
    return_start = datetime.fromisoformat(raw_return["start"]).date()
    return_end = datetime.fromisoformat(raw_return["end"]).date()
    if minimum_stay is not None:
        outbound_end = min(
            outbound_end, return_end - timedelta(days=int(minimum_stay))
        )
        return_start = max(
            return_start, outbound_start + timedelta(days=int(minimum_stay))
        )
    if maximum_stay is not None:
        outbound_start = max(
            outbound_start, return_start - timedelta(days=int(maximum_stay))
        )
        return_end = min(
            return_end, outbound_end + timedelta(days=int(maximum_stay))
        )
    feasible_outbound = {
        "start": outbound_start.isoformat(),
        "end": outbound_end.isoformat(),
    }
    feasible_return = {
        "start": return_start.isoformat(),
        "end": return_end.isoformat(),
    }
    if not (
        (observed_outbound == raw_outbound and observed_return == raw_return)
        or (
            observed_outbound == feasible_outbound
            and observed_return == feasible_return
        )
    ):
        raise RoundTripSessionError(
            "region pool Provider scope must preserve the complete feasible date windows"
        )
    for field in ("minimum_stay_nights", "maximum_stay_nights"):
        if provider_scope.get(field) != query_shape.get(field):
            raise RoundTripSessionError(
                "region pool Provider scope must preserve the complete stay range"
            )
    if provider_scope.get("coverage_semantics") != "provider_ranked_window":
        raise RoundTripSessionError(
            "region pool Provider scope requires provider_ranked_window semantics"
        )
    if provider_scope.get("price_selection_method") not in {
        "lowest_price_view",
        "price_ascending",
        "all_visible_compared",
    }:
        raise RoundTripSessionError(
            "region pool Provider scope requires Browser-visible lowest-price ordering"
        )
    disposition = provider_scope.get("scope_disposition")
    if disposition not in {"winner", "no_market_result"}:
        raise RoundTripSessionError(
            "region pool Provider scope has unsupported disposition"
        )
    signal_ids = provider_scope.get("temporal_signal_ids", [])
    if disposition == "winner" and not signal_ids:
        raise RoundTripSessionError(
            "region pool winner scope requires a local minimum signal"
        )
    if disposition == "no_market_result" and signal_ids:
        raise RoundTripSessionError(
            "region pool no-result scope cannot carry minimum signals"
        )
    excluded_pairs = {
        (
            _text(
                item.get("origin_gateway_node_id"),
                "query_shape.excluded_endpoint_cells[].origin_gateway_node_id",
            ),
            _text(
                item.get("destination_gateway_node_id"),
                "query_shape.excluded_endpoint_cells[].destination_gateway_node_id",
            ),
        )
        for item in query_shape.get("excluded_endpoint_cells", [])
        if isinstance(item, dict)
    }
    scope_pairs = {
        (origin, destination)
        for origin in origins
        for destination in destinations
    }
    if scope_pairs & excluded_pairs:
        raise RoundTripSessionError(
            "regional challenger Provider scope includes an excluded endpoint cell"
        )
    return scope_pairs


def _money_in_base_currency(
    state: dict[str, Any], money: dict[str, Any]
) -> float | None:
    amount = money.get("amount")
    currency = str(money.get("currency") or "").upper()
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    base_currency = str(state["scenario"]["base_currency"]).upper()
    if currency == base_currency:
        return round(float(amount), 2)
    rate = (
        state["scenario"].get("fx_snapshot", {}).get("rates", {}).get(currency)
    )
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return round(float(amount) * float(rate), 2)


def _frontier_screened_hub_scope(
    signal: dict[str, Any],
    *,
    min_hub_cells: int,
    max_hub_cells: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_cells = signal.get("screened_hub_cells")
    if raw_cells is None:
        legacy_node_ids = signal.get("screened_hub_node_ids")
        if not isinstance(legacy_node_ids, list):
            raise RoundTripSessionError(
                "portfolio frontier signal requires screened_hub_cells"
            )
        raw_cells = [
            {
                "hub_cell_id": f"legacy-node-{node_id}",
                "airport_node_ids": [node_id],
            }
            for node_id in legacy_node_ids
        ]
    if (
        not isinstance(raw_cells, list)
        or len(raw_cells) < min_hub_cells
        or len(raw_cells) > max_hub_cells
    ):
        raise RoundTripSessionError(
            "portfolio frontier signal must identify its bounded screened hub cells"
        )
    cells = []
    cell_ids = set()
    airport_ids = []
    airport_owners: dict[str, str] = {}
    for index, raw_cell in enumerate(raw_cells):
        if not isinstance(raw_cell, dict):
            raise RoundTripSessionError(
                f"portfolio frontier screened_hub_cells[{index}] must be an object"
            )
        cell_id = _text(
            raw_cell.get("hub_cell_id"),
            f"frontier_candidate.signal.screened_hub_cells[{index}].hub_cell_id",
        )
        if cell_id in cell_ids:
            raise RoundTripSessionError(
                "portfolio frontier screened hub cell IDs must be unique"
            )
        cell_ids.add(cell_id)
        raw_airport_ids = raw_cell.get("airport_node_ids")
        if not isinstance(raw_airport_ids, list) or not raw_airport_ids:
            raise RoundTripSessionError(
                "portfolio frontier screened hub cells require airport_node_ids"
            )
        normalized_airport_ids = []
        for airport_index, raw_airport_id in enumerate(raw_airport_ids):
            airport_id = _text(
                raw_airport_id,
                "frontier_candidate.signal.screened_hub_cells"
                f"[{index}].airport_node_ids[{airport_index}]",
            )
            if airport_id in normalized_airport_ids:
                raise RoundTripSessionError(
                    "portfolio frontier hub cell airport IDs must be unique"
                )
            previous_owner = airport_owners.get(airport_id)
            if previous_owner is not None:
                raise RoundTripSessionError(
                    "portfolio frontier airport node cannot belong to multiple hub cells"
                )
            airport_owners[airport_id] = cell_id
            normalized_airport_ids.append(airport_id)
            airport_ids.append(airport_id)
        cells.append(
            {
                "hub_cell_id": cell_id,
                "airport_node_ids": normalized_airport_ids,
            }
        )
    return cells, airport_ids


def _frontier_candidate(
    state: dict[str, Any],
    task: dict[str, Any],
    raw_candidate: dict[str, Any],
) -> dict[str, Any]:
    query = task.get("query_shape", {})
    candidate = copy.deepcopy(raw_candidate)
    if candidate.get("side") != "origin":
        raise RoundTripSessionError("portfolio frontier accepts origin hub candidates")
    node = candidate.get("node")
    if not isinstance(node, dict) or node.get("node_type") != "airport":
        raise RoundTripSessionError("portfolio frontier candidate must be an airport")
    node_id = _node_id(node, "frontier_candidate.node")
    excluded_ids = set(query.get("excluded_gateway_node_ids", []))
    if node_id in excluded_ids:
        raise RoundTripSessionError(
            "portfolio frontier candidate must be outside the admitted gateway pool"
        )
    signal = candidate.get("signal")
    if not isinstance(signal, dict) or signal.get("kind") not in {
        "portfolio_frontier_fare",
        "price_challenge_fare",
    }:
        raise RoundTripSessionError(
            "portfolio frontier candidate requires portfolio_frontier_fare signal"
        )
    legacy_frontier = task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND
    if legacy_frontier:
        screened_cells, screened_ids = _frontier_screened_hub_scope(
            signal,
            min_hub_cells=int(query.get("min_hub_cells", 1)),
            max_hub_cells=int(query.get("max_hub_cells", 10**9)),
        )
        if node_id not in screened_ids:
            raise RoundTripSessionError(
                "legacy portfolio frontier signal must identify its screened hub pool"
            )
        signal["screened_hub_cells"] = screened_cells
        signal["screened_hub_node_ids"] = screened_ids
        if set(screened_ids) & excluded_ids:
            raise RoundTripSessionError(
                "legacy portfolio frontier screened hub pool overlaps admitted gateways"
            )
    else:
        screened_ids = [node_id]
        for obsolete_field in (
            "hub_cell_id",
            "screened_hub_cells",
            "screened_hub_node_ids",
        ):
            signal.pop(obsolete_field, None)
    outbound_hub_id = _text(
        signal.get("outbound_origin_gateway_node_id", node_id),
        "frontier_candidate.signal.outbound_origin_gateway_node_id",
    )
    outbound_destination_id = _text(
        signal.get(
            "outbound_destination_gateway_node_id",
            signal.get("opposite_gateway_node_id"),
        ),
        "frontier_candidate.signal.outbound_destination_gateway_node_id",
    )
    return_destination_id = _text(
        signal.get(
            "return_origin_gateway_node_id",
            signal.get("opposite_gateway_node_id"),
        ),
        "frontier_candidate.signal.return_origin_gateway_node_id",
    )
    return_hub_id = _text(
        signal.get(
            "return_destination_gateway_node_id",
            signal.get("return_hub_node_id", node_id),
        ),
        "frontier_candidate.signal.return_destination_gateway_node_id",
    )
    if outbound_hub_id != node_id:
        raise RoundTripSessionError(
            "portfolio frontier candidate node must equal its outbound remote hub"
        )
    destination_ids = set(query.get("destination_gateway_node_ids", []))
    if not {outbound_destination_id, return_destination_id} <= destination_ids:
        raise RoundTripSessionError(
            "portfolio frontier destination endpoints exceed the admitted pool"
        )
    if return_hub_id in excluded_ids:
        raise RoundTripSessionError(
            "price challenge return hub must remain outside the Self-access Gateway Pool"
        )
    if legacy_frontier and return_hub_id not in set(screened_ids):
        raise RoundTripSessionError(
            "legacy portfolio frontier return hub must belong to its screened hub pool"
        )
    return_hub_node = None
    if return_hub_id != node_id:
        try:
            return_hub_node = AI_EXPLORATION._node(
                signal.get("return_hub_node"),
                "frontier_candidate.signal.return_hub_node",
                airport_only=True,
            )
        except AI_EXPLORATION.AIExplorationError as exc:
            raise RoundTripSessionError(str(exc)) from exc
        if return_hub_node["node_id"] != return_hub_id:
            raise RoundTripSessionError(
                "portfolio frontier return hub node does not match its endpoint ID"
            )
    outbound_date, destination_arrival_date, return_date = _round_trip_signal_dates(
        signal, "frontier_candidate.signal"
    )
    _date_in_window(
        outbound_date,
        query["outbound_departure_window"],
        "frontier_candidate.signal.outbound_date",
    )
    _date_in_window(
        return_date,
        query["return_departure_window"],
        "frontier_candidate.signal.return_date",
    )
    stay_nights = (
        datetime.fromisoformat(return_date).date()
        - datetime.fromisoformat(destination_arrival_date).date()
    ).days
    minimum_stay = query.get("minimum_stay_nights")
    if minimum_stay is not None and stay_nights < int(minimum_stay):
        raise RoundTripSessionError(
            "portfolio frontier signal is below minimum_stay_nights"
        )
    maximum_stay = query.get("maximum_stay_nights")
    if maximum_stay is not None and stay_nights > int(maximum_stay):
        raise RoundTripSessionError(
            "portfolio frontier signal exceeds maximum_stay_nights"
        )
    fare = signal.get("dated_fare_amount")
    if not isinstance(fare, dict):
        raise RoundTripSessionError(
            "portfolio frontier signal requires a dated fare amount"
        )
    fare_in_base = _money_in_base_currency(state, fare)
    positioning_fare = signal.get("positioning_airfare_lower_bound")
    positioning_in_base = (
        _money_in_base_currency(state, positioning_fare)
        if isinstance(positioning_fare, dict)
        else 0.0
        if task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND
        else None
    )
    incumbent = _money_in_base_currency(
        state,
        query.get(
            "incumbent_mandatory_air_ticket_total",
            query.get("incumbent_complete_cost", {}),
        ),
    )
    if fare_in_base is None or incumbent is None:
        raise RoundTripSessionError(
            "portfolio frontier fare and incumbent must be Base Currency comparable"
        )
    optimistic_mandatory_air_ticket_total = (
        round(float(fare_in_base) + float(positioning_in_base), 2)
        if positioning_in_base is not None
        else None
    )
    improvement_ratio = float(query.get("improvement_ratio", 0.03))
    threshold = incumbent * (1.0 - improvement_ratio)
    signal.pop("portfolio_challenger", None)
    signal.pop("outbound_date", None)
    signal.pop("return_date", None)
    signal["temporal_signal_version"] = "2.0"
    signal["itinerary_dates"] = {
        "outbound_departure_local_date": outbound_date,
        "outbound_destination_arrival_local_date": destination_arrival_date,
        "return_departure_local_date": return_date,
    }
    signal["optimistic_mandatory_air_ticket_total"] = (
        optimistic_mandatory_air_ticket_total
    )
    if task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND:
        signal["complete_cost_lower_bound"] = fare_in_base
    signal["frontier_improvement_ratio"] = improvement_ratio
    self_transfer_allowed = (
        state["scenario"]["intent"].get("self_transfer_policy") != "never"
    )
    competitive = (
        optimistic_mandatory_air_ticket_total is not None
        and optimistic_mandatory_air_ticket_total < threshold
        and self_transfer_allowed
    )
    disposition = (
        "competitive"
        if competitive
        else "constraint_infeasible"
        if not self_transfer_allowed
        else "positioning_airfare_bound_missing"
        if positioning_in_base is None
        else "bound_dominated"
    )
    signal["price_challenge_disposition"] = disposition
    if task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND:
        signal["frontier_disposition"] = disposition
    signal.update(
        {
            "outbound_origin_gateway_node_id": outbound_hub_id,
            "outbound_destination_gateway_node_id": outbound_destination_id,
            "return_origin_gateway_node_id": return_destination_id,
            "return_destination_gateway_node_id": return_hub_id,
        }
    )
    if return_hub_node is not None:
        signal["return_hub_node"] = return_hub_node
    if competitive:
        signal_id = _text(
            signal.get("signal_id"), "frontier_candidate.signal.signal_id"
        )
        token = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:12]
        local_origin_ids = list(query["admitted_origin_gateway_node_ids"])
        closed_remote_pair = (
            outbound_hub_id == return_hub_id
            and outbound_destination_id == return_destination_id
        )
        long_haul_scope = (
            "joint_round_trip" if closed_remote_pair else "atomic_itinerary"
        )
        long_haul_trip_type = (
            "round_trip" if closed_remote_pair else "frontier_open_jaw"
        )
        positioning_plan = {
            "signal_id": signal_id,
            "local_origin_gateway_node_ids": local_origin_ids,
            "outbound_hub_node_id": outbound_hub_id,
            "return_hub_node_id": return_hub_id,
            "dynamic_after_task_id": f"task-frontier-{token}-long-haul",
            "mode": "joint_round_trip" if closed_remote_pair else "split_one_ways",
            "published_with_challenger": False,
        }
        task_specs = [
            {
                "task_id": f"task-frontier-{token}-long-haul",
                "research_kind": "priced_offer",
                "journey_scope": long_haul_scope,
                "question": (
                    "Verify the exact payable remote-hub long-haul itinerary selected "
                    "by the bounded frontier screen. Preserve its complete outbound "
                    "and return endpoint geometry and one indivisible total."
                ),
                "query_shape": {
                    "trip_type": long_haul_trip_type,
                    "origins": [outbound_hub_id],
                    "destinations": [outbound_destination_id],
                    "outbound_departure_window": copy.deepcopy(
                        query["outbound_departure_window"]
                    ),
                    "return_departure_window": copy.deepcopy(
                        query["return_departure_window"]
                    ),
                    "frontier_endpoints": {
                        "outbound_origin_gateway_node_id": outbound_hub_id,
                        "outbound_destination_gateway_node_id": outbound_destination_id,
                        "return_origin_gateway_node_id": return_destination_id,
                        "return_destination_gateway_node_id": return_hub_id,
                    },
                    "date_search": {
                        "mode": "exact_dates",
                        "outbound": {
                            "mode": "exact_dates",
                            "exact_departure_dates": [outbound_date],
                        },
                        "return": {
                            "mode": "exact_dates",
                            "exact_departure_dates": [return_date],
                        },
                        "atomic_total_required": True,
                        "rule": (
                            "Observe the complete open-jaw or closed round-trip total "
                            "as one indivisible atomic amount."
                        ),
                    },
                    "frontier_signal_id": signal_id,
                },
                "required": True,
            }
        ]
        signal["portfolio_challenger"] = {
            "signal_id": signal_id,
            "route_shape": (
                "nested_round_trips"
                if closed_remote_pair
                else "mixed_atomic_portfolio"
            ),
            "origin_gateway_nodes": local_origin_ids,
            "destination_gateway_nodes": list(
                dict.fromkeys(
                    [outbound_destination_id, return_destination_id]
                )
            ),
            "geographic_cell": str(signal.get("hub_cell_id") or node_id),
            "reason": (
                "A bounded remote-hub screen exposed enough airfare headroom to "
                "justify exact long-haul and positioning-ticket verification."
            ),
            "optimistic_mandatory_air_ticket_total": (
                optimistic_mandatory_air_ticket_total
            ),
            "frontier_improvement_ratio": improvement_ratio,
            "requires_all_payable_tasks": True,
            "frontier_positioning": positioning_plan,
            "research_sequence": [
                "verify_remote_hub_long_haul_atomic_offer",
                "publish_positioning_work_after_exact_times",
                "solve_complete_frontier_portfolio",
            ],
            "task_specs": task_specs,
        }
    candidate["signal"] = signal
    candidate["candidate_scope"] = (
        "portfolio_frontier"
        if task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND
        else "price_challenge"
    )
    candidate["research_task_id"] = task.get("task_id")
    candidate["evidence_status"] = "research_observed"
    return candidate


def _provisional_transfer_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Permit admission before required external transfer checks are executed."""

    movements = sorted(
        (
            item
            for item in evidence.get("movements", [])
            if isinstance(item, dict) and item.get("mode") == "flight"
        ),
        key=lambda item: (str(item.get("departure")), str(item.get("arrival"))),
    )
    observed_at = next(
        (
            str(offer.get("observed_at"))
            for offer in evidence.get("offers", [])
            if isinstance(offer, dict) and offer.get("observed_at")
        ),
        _now(),
    )
    return [
        {
            "from_node": str(previous.get("to_node")),
            "to_node": str(following.get("from_node")),
            "approximate_duration_minutes": 1,
            "feasibility": "fallback_plausible",
            "evidence_url": "https://example.invalid/internal-surface-transfer-pending",
            "observed_at": observed_at,
        }
        for previous, following in zip(movements, movements[1:])
        if previous.get("to_node")
        and following.get("from_node")
        and previous.get("to_node") != following.get("from_node")
    ]


def _frontier_longhaul_requirement_satisfied(
    state: dict[str, Any],
    task: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    query = task.get("query_shape", {})
    endpoints = query.get("frontier_endpoints")
    if not isinstance(endpoints, dict):
        return False
    scenario = state["scenario"]
    known_node_ids = {
        _node_id(node, "scenario.nodes[]") for node in scenario["nodes"]
    }
    evidence_nodes = [
        copy.deepcopy(node)
        for node in evidence.get("nodes", [])
        if _node_id(node, "evidence.nodes[]") not in known_node_ids
    ]
    try:
        context = SOLVER._validate_bundle(
            {
                "schema_version": SOLVER.VERSION,
                "base_currency": scenario["base_currency"],
                "intent": scenario["intent"],
                "nodes": [*scenario["nodes"], *evidence_nodes],
                "transport_entitlements": scenario.get(
                    "transport_entitlements", []
                ),
                "fx_snapshot": scenario.get("fx_snapshot"),
                "evaluation_at": _now(),
                "movements": evidence.get("movements", []),
                "offers": evidence.get("offers", []),
                "internal_surface_transfers": _provisional_transfer_checks(
                    evidence
                ),
            }
        )
        offers = list(context["offers"].values())
        if len(offers) != 1:
            return False
        offer = offers[0]
        movement_offer = {
            movement_id: offer["offer_id"]
            for movement_id in offer["_used_movement_ids"]
        }
        movements = context["movements"]
        outbound_window = SOLVER._window(
            query.get("outbound_departure_window"),
            "frontier_longhaul.outbound_departure_window",
        )
        return_window = SOLVER._window(
            query.get("return_departure_window"),
            "frontier_longhaul.return_departure_window",
        )
        date_search = query.get("date_search", {})
        outbound_dates = set(
            date_search.get("outbound", {}).get("exact_departure_dates", [])
        )
        return_dates = set(
            date_search.get("return", {}).get("exact_departure_dates", [])
        )
        all_movement_ids = set(movement_offer)
        for outbound in SOLVER._paths(
            all_movement_ids,
            movements,
            movement_offer,
            {str(endpoints["outbound_origin_gateway_node_id"])},
            {str(endpoints["outbound_destination_gateway_node_id"])},
            outbound_window,
            context,
        ):
            if (
                outbound_dates
                and movements[outbound[0]]["_departure"].date().isoformat()
                not in outbound_dates
            ):
                continue
            remaining = all_movement_ids - set(outbound)
            outbound_arrival = movements[outbound[-1]]["_arrival"]
            for returning in SOLVER._paths(
                remaining,
                movements,
                movement_offer,
                {str(endpoints["return_origin_gateway_node_id"])},
                {str(endpoints["return_destination_gateway_node_id"])},
                return_window,
                context,
                outbound_arrival,
            ):
                if set(outbound) | set(returning) != all_movement_ids:
                    continue
                if (
                    return_dates
                    and movements[returning[0]]["_departure"].date().isoformat()
                    not in return_dates
                ):
                    continue
                stay_nights = (
                    movements[returning[0]]["_departure"].date()
                    - movements[outbound[-1]]["_arrival"].date()
                ).days
                minimum_stay = scenario["intent"].get("minimum_stay_nights")
                maximum_stay = scenario["intent"].get("maximum_stay_nights")
                if minimum_stay is not None and stay_nights < int(minimum_stay):
                    continue
                if maximum_stay is not None and stay_nights > int(maximum_stay):
                    continue
                return True
    except (KeyError, RoundTripSessionError, SOLVER.RoundTripError):
        return False
    return False


def _payable_offer_matches_task(
    state: dict[str, Any], task: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    """Require every payable flight variant to match the published geometry."""

    query = task.get("query_shape", {})
    origin_ids = set(query.get("origins", []))
    destination_ids = set(query.get("destinations", []))
    if not origin_ids or not destination_ids:
        return False
    screen_candidate = query.get("screen_candidate")
    if isinstance(screen_candidate, dict):
        outbound_starts = {
            str(screen_candidate.get("outbound_origin_gateway_node_id"))
        }
        outbound_goals = {
            str(screen_candidate.get("outbound_destination_gateway_node_id"))
        }
        return_starts = {
            str(screen_candidate.get("return_origin_gateway_node_id"))
        }
        return_goals = {
            str(screen_candidate.get("return_destination_gateway_node_id"))
        }
    else:
        outbound_starts = origin_ids
        outbound_goals = destination_ids
        return_starts = destination_ids
        return_goals = origin_ids
    scenario = state["scenario"]
    known_node_ids = {
        _node_id(node, "scenario.nodes[]") for node in scenario["nodes"]
    }
    evidence_nodes = [
        copy.deepcopy(node)
        for node in evidence.get("nodes", [])
        if _node_id(node, "evidence.nodes[]") not in known_node_ids
    ]
    try:
        validation_intent = copy.deepcopy(scenario["intent"])
        validation_intent["baggage_policy"] = {
            "required_items": [],
            "paid_baggage_allowed": True,
        }
        validation_intent["budget"] = None
        validation_intent["minimum_stay_nights"] = None
        validation_intent["maximum_stay_nights"] = None
        validation_intent["maximum_outbound_duration_minutes"] = None
        validation_intent["maximum_return_duration_minutes"] = None
        validation_offers = []
        for raw_offer in evidence.get("offers", []):
            offer = copy.deepcopy(raw_offer)
            if task.get("journey_scope") == "one_way":
                offer["coverage_start_nodes"] = sorted(origin_ids)
                offer["coverage_end_nodes"] = sorted(destination_ids)
                offer["coverage_departure_window"] = copy.deepcopy(
                    query.get("departure_window")
                )
            validation_offers.append(offer)
        context = SOLVER._validate_bundle(
            {
                "schema_version": SOLVER.VERSION,
                "base_currency": scenario["base_currency"],
                "intent": validation_intent,
                "nodes": [*scenario["nodes"], *evidence_nodes],
                "transport_entitlements": scenario.get(
                    "transport_entitlements", []
                ),
                "fx_snapshot": scenario.get("fx_snapshot"),
                "evaluation_at": _now(),
                "movements": evidence.get("movements", []),
                "offers": validation_offers,
                "internal_surface_transfers": _provisional_transfer_checks(
                    evidence
                ),
            }
        )
        movements = context["movements"]
        outbound_window = SOLVER._window(
            query.get(
                "outbound_departure_window",
                query.get("departure_window"),
            ),
            "payable_task.outbound_departure_window",
        )
        journey_scope = task.get("journey_scope")
        for offer in context["offers"].values():
            used_ids = set(offer["_used_movement_ids"])
            movement_offer = {
                movement_id: offer["offer_id"] for movement_id in used_ids
            }
            if journey_scope == "one_way":
                exact_dates = list(
                    query.get("date_search", {}).get(
                        "exact_departure_dates", []
                    )
                )
                schedule_context = query.get("schedule_context", {})
                if not any(
                    set(path) == used_ids
                    and not TASK_COMPATIBILITY.path_schedule_issues(
                        movements,
                        path,
                        departure_window=query.get("departure_window", {}),
                        exact_departure_dates=exact_dates,
                        schedule_context=schedule_context,
                    )
                    for path in SOLVER._paths(
                        used_ids,
                        movements,
                        movement_offer,
                        origin_ids,
                        destination_ids,
                        outbound_window,
                        context,
                    )
                ):
                    return False
                continue
            return_window = SOLVER._window(
                query.get("return_departure_window"),
                "payable_task.return_departure_window",
            )
            complete = False
            date_search = query.get("date_search", {})
            outbound_exact_dates = list(
                date_search.get("outbound", {}).get(
                    "exact_departure_dates", []
                )
            )
            return_exact_dates = list(
                date_search.get("return", {}).get(
                    "exact_departure_dates", []
                )
            )
            schedule_context = query.get("schedule_context", {})
            for outbound in SOLVER._paths(
                used_ids,
                movements,
                movement_offer,
                outbound_starts,
                outbound_goals,
                outbound_window,
                context,
            ):
                if TASK_COMPATIBILITY.path_schedule_issues(
                    movements,
                    outbound,
                    departure_window=query.get(
                        "outbound_departure_window", {}
                    ),
                    exact_departure_dates=outbound_exact_dates,
                    schedule_context=(
                        schedule_context.get("outbound", {})
                        if isinstance(schedule_context, dict)
                        else {}
                    ),
                ):
                    continue
                remaining = used_ids - set(outbound)
                outbound_arrival = movements[outbound[-1]]["_arrival"]
                for returning in SOLVER._paths(
                    remaining,
                    movements,
                    movement_offer,
                    return_starts,
                    return_goals,
                    return_window,
                    context,
                    outbound_arrival,
                ):
                    if set(outbound) | set(returning) != used_ids:
                        continue
                    if TASK_COMPATIBILITY.path_schedule_issues(
                        movements,
                        returning,
                        departure_window=query.get(
                            "return_departure_window", {}
                        ),
                        exact_departure_dates=return_exact_dates,
                        schedule_context=(
                            schedule_context.get("return", {})
                            if isinstance(schedule_context, dict)
                            else {}
                        ),
                    ):
                        continue
                    if set(outbound) | set(returning) == used_ids:
                        complete = True
                        break
                if complete:
                    break
            if not complete:
                return False
        return bool(context["offers"])
    except (KeyError, RoundTripSessionError, SOLVER.RoundTripError):
        return False


def _surface_requirement_satisfied(
    state: dict[str, Any],
    task: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    query = task.get("query_shape", {})
    origins = set(query.get("origins", []))
    destinations = set(query.get("destinations", []))
    if not origins or not destinations:
        return False
    window = query.get("departure_window", {})
    schedule = query.get("schedule_context", {})
    try:
        first_window = (
            SOLVER._dt(window.get("start"), "departure_window.start"),
            SOLVER._dt(window.get("end"), "departure_window.end"),
        )
        not_before = (
            SOLVER._dt(schedule["not_before"], "schedule_context.not_before")
            if schedule.get("not_before") is not None
            else None
        )
        must_arrive_by = (
            SOLVER._dt(
                schedule["must_arrive_by"],
                "schedule_context.must_arrive_by",
            )
            if schedule.get("must_arrive_by") is not None
            else None
        )
    except SOLVER.RoundTripError:
        return False

    scenario = state["scenario"]
    constraints = query.get("pool_constraints", {})
    max_time = constraints.get("max_time_minutes")
    max_cost = constraints.get("max_cost")
    annotated_offers = []
    for offer in evidence.get("offers", []):
        if not isinstance(offer, dict):
            return False
        annotated_offers.append(
            {**copy.deepcopy(offer), "pool_constraints": copy.deepcopy(constraints)}
        )
    if not annotated_offers:
        return False

    known_node_ids = {
        _node_id(node, "scenario.nodes[]") for node in scenario["nodes"]
    }
    evidence_nodes = [
        copy.deepcopy(node)
        for node in evidence.get("nodes", [])
        if _node_id(node, "evidence.nodes[]") not in known_node_ids
    ]
    try:
        context = SOLVER._validate_bundle(
            {
                "schema_version": SOLVER.VERSION,
                "base_currency": scenario["base_currency"],
                "intent": scenario["intent"],
                "nodes": [*scenario["nodes"], *evidence_nodes],
                "transport_entitlements": scenario.get(
                    "transport_entitlements", []
                ),
                "fx_snapshot": scenario.get("fx_snapshot"),
                "evaluation_at": _now(),
                "movements": evidence.get("movements", []),
                "offers": annotated_offers,
            }
        )
    except (RoundTripSessionError, SOLVER.RoundTripError):
        return False

    offers = list(context["offers"].values())
    movements = context["movements"]
    max_cost_amount = (
        float(max_cost["amount"]) if isinstance(max_cost, dict) else None
    )
    pairs = [
        (movement_id, offer["offer_id"])
        for offer in offers
        for movement_id in offer["_used_movement_ids"]
    ]
    movement_offer = dict(pairs)
    if len(movement_offer) != len(pairs):
        return False
    if (
        max_cost_amount is not None
        and sum(offer["_effective_cost"] for offer in offers) > max_cost_amount
    ):
        return False
    for path in SOLVER._paths(
        set(movement_offer),
        movements,
        movement_offer,
        origins,
        destinations,
        first_window,
        context,
        not_before,
    ):
        if set(path) != set(movement_offer):
            continue
        first = movements[path[0]]["_departure"]
        last = movements[path[-1]]["_arrival"]
        if must_arrive_by is not None and last > must_arrive_by:
            continue
        if (
            max_time is not None
            and int((last - first).total_seconds() // 60) > int(max_time)
        ):
            continue
        return True
    return False


def ingest_evidence(raw_state: Any, raw_evidence: Any) -> dict[str, Any]:
    state = validate_session(raw_state)
    if not isinstance(raw_evidence, dict) or raw_evidence.get("evidence_version") != SESSION_VERSION:
        raise RoundTripSessionError(f"evidence_version must be {SESSION_VERSION}")
    bundle_id = _text(raw_evidence.get("bundle_id"), "bundle_id")
    existing_bundle_ids = {
        item.get("bundle_id")
        for item in state["evidence_bundles"]
        if isinstance(item, dict)
    }
    if bundle_id in existing_bundle_ids:
        existing = next(
            (
                item
                for item in state["evidence_bundles"]
                if isinstance(item, dict) and item.get("bundle_id") == bundle_id
            ),
            None,
        )
        if existing == raw_evidence:
            return _compact_session(state)
        raise RoundTripSessionError(
            f"duplicate evidence bundle {bundle_id} has conflicting content"
        )
    task = _task_by_id(state, _text(raw_evidence.get("task_id"), "task_id"))
    active = state.get("active_hypothesis")
    hypothesis_node = state["hypothesis_tree"]["nodes"].get(
        task["hypothesis_id"], {}
    )
    paused_gateway_receipt = bool(
        task.get("hypothesis_kind") == "gateway_discovery"
        and hypothesis_node.get("status") == "candidate"
        and hypothesis_node.get("was_selected") is True
        and task.get("requirement_status") == "open"
    )
    if (
        active is None
        or active["hypothesis_id"] != task["hypothesis_id"]
    ) and not paused_gateway_receipt:
        raise RoundTripSessionError("evidence task must belong to the active hypothesis")
    status = raw_evidence.get("evidence_status")
    ai_proposal = (
        status == "ai_proposed"
        and task["research_kind"] == "region_scope_definition"
    )
    temporal_research = status == "research_observed" and task["research_kind"] == "priced_offer" and task.get("journey_scope") in {"joint_round_trip", "one_way"}
    pool_screen_task = task["research_kind"] == "region_pool_fare_screen"
    frontier_screen_task = task["research_kind"] in {
        FRONTIER_SCREEN_RESEARCH_KIND,
        PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
    }
    surface_task = (
        task["research_kind"] == "priced_offer"
        and task.get("journey_scope") == "surface"
    )
    transfer_task = (
        task["research_kind"] == INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND
    )
    frontier_longhaul_task = (
        task["research_kind"] == "priced_offer"
        and isinstance(
            task.get("query_shape", {}).get("frontier_endpoints"), dict
        )
    )
    pool_screen_research = (
        status == "research_observed"
        and pool_screen_task
    )
    frontier_screen_research = (
        status == "research_observed" and frontier_screen_task
    )
    payable = status == "verified_observed" and task["research_kind"] == "priced_offer"
    transfer_observed = status == "verified_observed" and transfer_task
    if status not in {"no_result", "rejected"} and not (
        ai_proposal
        or temporal_research
        or pool_screen_research
        or frontier_screen_research
        or payable
        or transfer_observed
    ):
        if task["research_kind"] == "region_scope_definition":
            expected = "ai_proposed"
        elif task["research_kind"] == "region_pool_fare_screen":
            expected = "research_observed"
        elif task["research_kind"] in {
            FRONTIER_SCREEN_RESEARCH_KIND,
            PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
        }:
            expected = "research_observed"
        elif task["research_kind"] == INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND:
            expected = "verified_observed"
        else:
            expected = "verified_observed or nonterminal temporal research_observed"
        raise RoundTripSessionError(f"task expects {expected} or scoped terminal evidence")
    ai_proposals = raw_evidence.get("ai_proposals", [])
    if ai_proposal:
        expected_type = "region_scope"
        if (
            not isinstance(ai_proposals, list)
            or len(ai_proposals) != 1
            or ai_proposals[0].get("proposal_type") != expected_type
        ):
            raise RoundTripSessionError(
                f"{task['research_kind']} requires exactly one {expected_type} proposal"
            )
        if raw_evidence.get("coverage_complete") is not True:
            raise RoundTripSessionError("AI proposal task must be coverage_complete=true")
    elif ai_proposals:
        raise RoundTripSessionError("ai_proposals are allowed only for AI definition tasks")
    if payable:
        expected_scope = task.get("journey_scope")
        wrong_scope = [
            offer.get("offer_id")
            for offer in raw_evidence.get("offers", [])
            if offer.get("journey_scope") != expected_scope
        ]
        if wrong_scope:
            raise RoundTripSessionError(
                f"payable offers do not match the active task journey_scope: {wrong_scope}"
            )
        if expected_scope == "one_way":
            wrong_direction = [
                offer.get("offer_id")
                for offer in raw_evidence.get("offers", [])
                if offer.get("direction") != task.get("direction")
            ]
            if wrong_direction:
                raise RoundTripSessionError(
                    f"one-way offers do not match the active task direction: {wrong_direction}"
                )
        if (
            not surface_task
            and not frontier_longhaul_task
            and not _payable_offer_matches_task(state, task, raw_evidence)
        ):
            raise RoundTripSessionError(
                "payable offers do not match the active Work Item geometry"
            )
    temporal_signals = raw_evidence.get("temporal_signals", [])
    if pool_screen_task:
        provider_scopes = raw_evidence.get("provider_scopes", [])
        if status == "research_observed" and not raw_evidence.get("observations"):
            raise RoundTripSessionError(
                "region pool fare screen requires an observed Provider result"
            )
        if status == "rejected" and provider_scopes:
            raise RoundTripSessionError(
                "rejected region pool evidence cannot cover Provider scope"
            )
        covered_pairs: set[tuple[str, str]] = set()
        for provider_scope in provider_scopes:
            covered_pairs.update(_pool_scope_pairs(task, provider_scope))
        if status in {"research_observed", "no_result"}:
            query_shape = task.get("query_shape", {})
            expected_pairs = {
                (origin, destination)
                for origin in query_shape.get("origin_gateway_node_ids", [])
                for destination in query_shape.get(
                    "destination_gateway_node_ids", []
                )
            }
            expected_pairs -= {
                (
                    str(item.get("origin_gateway_node_id")),
                    str(item.get("destination_gateway_node_id")),
                )
                for item in query_shape.get("excluded_endpoint_cells", [])
                if isinstance(item, dict)
            }
            if not provider_scopes or covered_pairs != expected_pairs:
                raise RoundTripSessionError(
                    "region pool Evidence must cover the immutable Work Item partition"
                )
    if pool_screen_research:
        query_shape = task.get("query_shape", {})
        if not isinstance(temporal_signals, list) or not temporal_signals:
            raise RoundTripSessionError(
                "region pool fare screen requires dated pair signals"
            )
        if raw_evidence.get("movements") or raw_evidence.get("offers") or raw_evidence.get("gateway_candidates"):
            raise RoundTripSessionError(
                "region pool fare screen cannot create gateways, movements, or offers"
            )
        origin_ids = set(query_shape.get("origin_gateway_node_ids", []))
        destination_ids = set(query_shape.get("destination_gateway_node_ids", []))
        outbound_window = query_shape.get("outbound_departure_window", {})
        return_window = query_shape.get("return_departure_window", {})
        outbound_start = datetime.fromisoformat(
            str(outbound_window.get("start")).replace("Z", "+00:00")
        ).date()
        outbound_end = datetime.fromisoformat(
            str(outbound_window.get("end")).replace("Z", "+00:00")
        ).date()
        return_start = datetime.fromisoformat(
            str(return_window.get("start")).replace("Z", "+00:00")
        ).date()
        return_end = datetime.fromisoformat(
            str(return_window.get("end")).replace("Z", "+00:00")
        ).date()
        minimum_stay = query_shape.get("minimum_stay_nights")
        maximum_stay = query_shape.get("maximum_stay_nights")
        excluded_endpoint_pairs = {
            (
                str(item.get("origin_gateway_node_id")),
                str(item.get("destination_gateway_node_id")),
            )
            for item in query_shape.get("excluded_endpoint_cells", [])
            if isinstance(item, dict)
        }
        for signal in temporal_signals:
            if signal.get("signal_kind") != "joint_date_pair":
                raise RoundTripSessionError(
                    "region pool fare screen accepts only joint_date_pair signals"
                )
            if (
                signal.get("outbound_origin_gateway_node_id") not in origin_ids
                or signal.get("return_destination_gateway_node_id") not in origin_ids
                or signal.get("outbound_destination_gateway_node_id") not in destination_ids
                or signal.get("return_origin_gateway_node_id") not in destination_ids
            ):
                raise RoundTripSessionError(
                    "region pool fare signal exceeds the submitted airport pools"
                )
            signal_endpoint_pairs = {
                (
                    str(signal.get("outbound_origin_gateway_node_id")),
                    str(signal.get("outbound_destination_gateway_node_id")),
                ),
                (
                    str(signal.get("return_destination_gateway_node_id")),
                    str(signal.get("return_origin_gateway_node_id")),
                ),
            }
            if signal_endpoint_pairs & excluded_endpoint_pairs:
                raise RoundTripSessionError(
                    "regional challenger fare signal repeats an excluded endpoint cell"
                )
            (
                outbound_date_raw,
                destination_arrival_date_raw,
                return_date_raw,
            ) = _round_trip_signal_dates(signal, "region_pool_fare_signal")
            outbound_date = datetime.fromisoformat(outbound_date_raw).date()
            destination_arrival_date = datetime.fromisoformat(
                destination_arrival_date_raw
            ).date()
            return_date = datetime.fromisoformat(return_date_raw).date()
            if not (outbound_start <= outbound_date <= outbound_end):
                raise RoundTripSessionError(
                    "region pool fare signal outbound date is outside the submitted window"
                )
            if not (return_start <= return_date <= return_end):
                raise RoundTripSessionError(
                    "region pool fare signal return date is outside the submitted window"
                )
            stay_nights = (return_date - destination_arrival_date).days
            if minimum_stay is not None and stay_nights < int(minimum_stay):
                raise RoundTripSessionError(
                    "region pool fare signal is below minimum_stay_nights"
                )
            if maximum_stay is not None and stay_nights > int(maximum_stay):
                raise RoundTripSessionError(
                    "region pool fare signal exceeds maximum_stay_nights"
                )
    if frontier_screen_research:
        candidates = raw_evidence.get("gateway_candidates", [])
        if not isinstance(candidates, list) or not candidates:
            raise RoundTripSessionError(
                "price challenge screen requires at least one candidate"
            )
        if task.get("research_kind") == FRONTIER_SCREEN_RESEARCH_KIND:
            hub_cell_ids = [
                str((candidate.get("signal") or {}).get("hub_cell_id") or "")
                for candidate in candidates
                if isinstance(candidate, dict)
            ]
            if (
                any(not hub_cell_id for hub_cell_id in hub_cell_ids)
                or len(set(hub_cell_ids)) != len(hub_cell_ids)
            ):
                raise RoundTripSessionError(
                    "legacy portfolio frontier candidates must represent distinct hub cells"
                )
        if (
            raw_evidence.get("movements")
            or raw_evidence.get("offers")
            or raw_evidence.get("temporal_signals")
            or raw_evidence.get("ai_proposals")
        ):
            raise RoundTripSessionError(
                "portfolio frontier screen accepts only research gateway candidates"
            )
    transfer_values = raw_evidence.get("internal_surface_transfers", [])
    transfer_infeasible = False
    if transfer_observed:
        if not isinstance(transfer_values, list) or len(transfer_values) != 1:
            raise RoundTripSessionError(
                "Internal Surface Transfer Evidence requires exactly one route check"
            )
        transfer = transfer_values[0]
        query = task.get("query_shape", {})
        if (
            transfer.get("from_node") != query.get("from_node")
            or transfer.get("to_node") != query.get("to_node")
        ):
            raise RoundTripSessionError(
                "Internal Surface Transfer Evidence endpoints do not match the Work Item"
            )
        transfer_infeasible = transfer.get("feasibility") == "infeasible"
        required_minutes = (
            int(
                state["scenario"]["intent"]["mode_transition_buffers_minutes"].get(
                    "flight_to_surface", 60
                )
            )
            + int(transfer.get("approximate_duration_minutes", 0))
            + int(
                state["scenario"]["intent"]["mode_transition_buffers_minutes"].get(
                    "surface_to_flight", 120
                )
            )
        )
        if required_minutes > int(query.get("available_minutes", 0)):
            transfer_infeasible = True
            transfer["feasibility"] = "infeasible"
    if temporal_research:
        if raw_evidence.get("coverage_complete") is not False:
            raise RoundTripSessionError("temporal research evidence must keep the priced task pending with coverage_complete=false")
        if not isinstance(temporal_signals, list) or not temporal_signals:
            raise RoundTripSessionError("temporal research evidence requires temporal_signals")
        if raw_evidence.get("movements") or raw_evidence.get("offers") or raw_evidence.get("gateway_candidates"):
            raise RoundTripSessionError("temporal research evidence cannot create gateways, movements, or offers")
        expected_kind = "joint_date_pair" if task.get("journey_scope") == "joint_round_trip" else "directional_date"
        if any(signal.get("signal_kind") != expected_kind for signal in temporal_signals):
            raise RoundTripSessionError(f"task expects {expected_kind} temporal signals")
        if expected_kind == "directional_date" and any(signal.get("direction") != task.get("direction") for signal in temporal_signals):
            raise RoundTripSessionError("directional temporal signal must match the priced task direction")
    elif temporal_signals and not pool_screen_research:
        raise RoundTripSessionError("temporal_signals are allowed only as nonterminal research evidence for a priced flight task")
    next_state = copy.deepcopy(state)
    next_task = _task_by_id(next_state, task["task_id"])
    next_task["attempt_count"] += 1
    next_task["evidence_bundle_ids"].append(bundle_id)
    native_pool_probe = bool(
        pool_screen_task
        and next_task.get("query_shape", {}).get("scope_strategy")
        == "provider_native_first"
    )
    if pool_screen_task:
        admitted_provider_scopes = copy.deepcopy(
            raw_evidence.get("provider_scopes", [])
        )
        if admitted_provider_scopes:
            next_task["scope_execution"] = {
                "status": "executed",
                "provider_scopes": admitted_provider_scopes,
                "evidence_bundle_id": bundle_id,
            }
            for provider_scope in admitted_provider_scopes:
                for side, field in (
                    ("origin", "origin_gateway_node_ids"),
                    ("destination", "destination_gateway_node_ids"),
                ):
                    coverage = (
                        next_state.get("region_scopes", {})
                        .get(side, {})
                        .get("coverage", {})
                    )
                    for node_id in provider_scope.get(field, []):
                        if (
                            node_id in coverage
                            and coverage[node_id] != "research_observed"
                        ):
                            coverage[node_id] = "provider_scope_executed"
        else:
            next_task["scope_execution"] = {
                "status": (
                    "fallback_required"
                    if native_pool_probe and status == "rejected"
                    else "unproven"
                ),
                "provider_scopes": [],
                "evidence_bundle_id": bundle_id,
            }
    # A region-pool Work Item asks the Provider for one visible local minimum.
    # Its minimal Capture closes only the immutable partition whose complete
    # execution scope the Product Adapter attached above. Repeating that same
    # query cannot add coverage and therefore must never create another charge.
    surface_complete = (
        _surface_requirement_satisfied(next_state, next_task, raw_evidence)
        if surface_task and status == "verified_observed"
        else False
    )
    frontier_longhaul_complete = (
        _frontier_longhaul_requirement_satisfied(
            next_state, next_task, raw_evidence
        )
        if frontier_longhaul_task and status == "verified_observed"
        else False
    )
    coverage_complete = (
        status in {"research_observed", "no_result", "rejected"}
        if pool_screen_task
        else frontier_longhaul_complete
        if frontier_longhaul_task
        else surface_complete
        if surface_task
        else raw_evidence.get("coverage_complete", True)
    )
    if coverage_complete:
        terminal_without_winner = bool(
            pool_screen_task and status in {"no_result", "rejected"}
        ) or transfer_infeasible
        _set_requirement_state(
            next_task,
            "exhausted"
            if terminal_without_winner
            or status in {"no_result", "rejected"}
            else "satisfied",
            evidence_disposition=status,
        )
        if terminal_without_winner:
            next_task["status"] = (
                "infeasible" if transfer_infeasible else "no_result"
            )
        elif pool_screen_task:
            next_task["status"] = "research_observed"
        else:
            next_task["status"] = status
    else:
        _set_requirement_state(
            next_task, "open", evidence_disposition=status
        )
        next_task["status"] = "pending"
    next_state["evidence_bundles"].append(copy.deepcopy(raw_evidence))
    if status == "ai_proposed":
        proposal = ai_proposals[0]["proposal"]
        _apply_region_scope_proposal(next_state, next_task, proposal)
    elif status == "research_observed":
        if temporal_research or pool_screen_research:
            annotated_signals = [
                {**copy.deepcopy(signal), "research_task_id": task["task_id"], "hypothesis_id": task["hypothesis_id"]}
                for signal in temporal_signals
            ]
            next_state["temporal_signals"] = _merge_unique(next_state.get("temporal_signals", []), annotated_signals, "signal_id")
            if temporal_research:
                next_temporal = next_task["temporal_state"]
                for signal in annotated_signals:
                    if signal["signal_id"] not in next_temporal["signal_refs"]:
                        next_temporal["signal_refs"].append(signal["signal_id"])
                next_temporal["phase"] = "surface_discovery"
            elif pool_screen_research:
                observed_by_side = {
                    "origin": {
                        str(signal.get("outbound_origin_gateway_node_id"))
                        for signal in annotated_signals
                    }
                    | {
                        str(signal.get("return_destination_gateway_node_id"))
                        for signal in annotated_signals
                    },
                    "destination": {
                        str(signal.get("outbound_destination_gateway_node_id"))
                        for signal in annotated_signals
                    }
                    | {
                        str(signal.get("return_origin_gateway_node_id"))
                        for signal in annotated_signals
                    },
                }
                for side in ("origin", "destination"):
                    for candidate in next_state["gateway_candidates"][side]:
                        if (
                            candidate.get("evidence_status") == "ai_proposed"
                            and str(candidate.get("node", {}).get("node_id"))
                            in observed_by_side[side]
                        ):
                            candidate["evidence_status"] = "research_observed"
                            candidate["signal"] = {
                                "kind": "region_pool_screen_covered"
                            }
                    coverage = next_state.get("region_scopes", {}).get(side, {}).get("coverage", {})
                    for node_id in observed_by_side[side]:
                        if node_id in coverage:
                            coverage[node_id] = "research_observed"
            if pool_screen_research and annotated_signals:
                gateway_node = next_state["hypothesis_tree"]["nodes"].get(
                    task["hypothesis_id"]
                )
                if isinstance(gateway_node, dict):
                    gateway_node["status"] = "candidate"
                    gateway_node["terminal_reason"] = None
                next_state["active_hypothesis"] = None
        elif frontier_screen_research:
            normalized_candidates = [
                _frontier_candidate(next_state, next_task, candidate)
                for candidate in raw_evidence.get("gateway_candidates", [])
            ]
            node_map = {
                _node_id(node, "scenario.nodes[]"): node
                for node in next_state["scenario"]["nodes"]
            }
            for candidate in normalized_candidates:
                candidate_nodes = [("node", candidate["node"])]
                return_hub_node = candidate.get("signal", {}).get(
                    "return_hub_node"
                )
                if isinstance(return_hub_node, dict):
                    candidate_nodes.append(("return_hub_node", return_hub_node))
                for role, node in candidate_nodes:
                    node_id = _node_id(node, "frontier_candidate.node")
                    if node_id in node_map:
                        try:
                            node = IDENTITY.reconcile_node(node_map[node_id], node)
                        except IDENTITY.CanonicalValueError as exc:
                            raise RoundTripSessionError(str(exc)) from exc
                    else:
                        next_state["scenario"]["nodes"].append(copy.deepcopy(node))
                        node_map[node_id] = node
                    if role == "node":
                        candidate["node"] = copy.deepcopy(node)
                    else:
                        candidate["signal"]["return_hub_node"] = copy.deepcopy(node)
            next_state["gateway_candidates"]["origin"] = _merge_unique(
                next_state["gateway_candidates"]["origin"],
                normalized_candidates,
                "candidate_id",
            )
    elif transfer_observed:
        next_state["internal_surface_transfers"] = [
            *next_state.get("internal_surface_transfers", []),
            *[
                copy.deepcopy(value)
                for value in transfer_values
                if value not in next_state.get("internal_surface_transfers", [])
            ],
        ]
    elif status == "verified_observed" and not (
        (surface_task and not surface_complete)
        or (frontier_longhaul_task and not frontier_longhaul_complete)
    ):
        node_map = {_node_id(node, "scenario.nodes[]"): node for node in next_state["scenario"]["nodes"]}
        for node in raw_evidence.get("nodes", []):
            node_id = _node_id(node, "evidence.nodes[]")
            if node_id in node_map:
                try:
                    IDENTITY.reconcile_node(node_map[node_id], node)
                except IDENTITY.CanonicalValueError as exc:
                    raise RoundTripSessionError(str(exc)) from exc
            else:
                next_state["scenario"]["nodes"].append(copy.deepcopy(node))
                node_map[node_id] = node
        movement_node_ids = {node for movement in raw_evidence.get("movements", []) for node in (movement.get("from_node"), movement.get("to_node"))}
        if not movement_node_ids <= set(node_map):
            raise RoundTripSessionError("every movement endpoint must have a RegionNode in scenario or evidence")
        next_state["movements"] = _merge_unique(next_state["movements"], raw_evidence.get("movements", []), "movement_id")
        annotated = []
        movement_map = {movement["movement_id"]: movement for movement in next_state["movements"]}
        for offer in raw_evidence.get("offers", []):
            value = {
                **offer,
                "research_task_id": task["task_id"],
                "hypothesis_id": task["hypothesis_id"],
                "evidence_bundle_id": bundle_id,
                "evidence_admitted_at": raw_evidence.get("admitted_at"),
            }
            if offer.get("journey_scope") == "surface":
                value["surface_role"] = task["query_shape"]["surface_role"]
                value["pool_constraints"] = copy.deepcopy(
                    task["query_shape"]["pool_constraints"]
                )
            geometry = [
                {
                    key: movement_map[movement_id].get(key)
                    for key in (
                        "from_node",
                        "to_node",
                        "departure",
                        "arrival",
                        "mode",
                        "operator",
                        "service_id",
                        "flight_identity",
                        "transfer_from_previous",
                    )
                }
                for movement_id in sorted(
                    offer["movement_ids"],
                    key=lambda item: (
                        str(movement_map[item].get("departure") or ""),
                        str(movement_map[item].get("arrival") or ""),
                        str(item),
                    ),
                )
            ]
            value["itinerary_id"] = "itinerary-" + hashlib.sha256(
                json.dumps(geometry, sort_keys=True).encode("utf-8")
            ).hexdigest()[:20]
            snapshot_basis = {
                key: value.get(key)
                for key in (
                    "itinerary_id",
                    "provider_offer_id",
                    "source_id",
                    "provider",
                    "seller",
                    "url",
                    "observed_at",
                    "price",
                    "mandatory_baggage_cost",
                    "required_accommodation_cost",
                    "included_baggage",
                    "ticket_context",
                    "expires_at",
                )
            }
            value["offer_snapshot_id"] = "snapshot-" + hashlib.sha256(
                json.dumps(snapshot_basis, sort_keys=True).encode("utf-8")
            ).hexdigest()[:20]
            refresh_target_id = value.get("refresh_of_offer_id")
            if refresh_target_id is not None:
                refresh_targets = {
                    str(target.get("offer_id")): target
                    for target in (
                        task.get("query_shape", {})
                        .get("price_refresh", {})
                        .get("refresh_targets", [])
                    )
                    if isinstance(target, dict) and target.get("offer_id")
                }
                if str(refresh_target_id) not in refresh_targets:
                    raise RoundTripSessionError(
                        "refresh Offer targets an unpublished Offer"
                    )
                old_offer = next(
                    (
                        item
                        for item in state.get("offers", [])
                        if item.get("offer_id") == refresh_target_id
                    ),
                    None,
                )
                if not isinstance(old_offer, dict):
                    raise RoundTripSessionError(
                        "refresh Offer target is absent from the Evidence ledger"
                    )
                if (
                    old_offer.get("itinerary_id") != value["itinerary_id"]
                    or old_offer.get("journey_scope") != value.get("journey_scope")
                    or old_offer.get("direction") != value.get("direction")
                ):
                    raise RoundTripSessionError(
                        "refresh Offer does not preserve itinerary geometry"
                    )
                if IDENTITY.offer_ticketing_structure_id(
                    old_offer, movement_map
                ) != IDENTITY.offer_ticketing_structure_id(value, movement_map):
                    raise RoundTripSessionError(
                        "refresh Offer does not preserve its Ticketing Structure"
                    )
                identity_fields = ("source_id", "provider", "seller")
                mismatched_identity = [
                    field
                    for field in identity_fields
                    if str(old_offer.get(field) or "").strip().casefold()
                    != str(value.get(field) or "").strip().casefold()
                ]
                if mismatched_identity:
                    raise RoundTripSessionError(
                        "refresh Offer must preserve target Seller identity: "
                        + ", ".join(mismatched_identity)
                    )
                try:
                    old_observed_at = offset_timestamp(
                        str(old_offer.get("observed_at")),
                        "old_offer.observed_at",
                    )
                    new_observed_at = offset_timestamp(
                        str(value.get("observed_at")),
                        "refresh_offer.observed_at",
                    )
                except ValueError as exc:
                    raise RoundTripSessionError(str(exc)) from exc
                if new_observed_at <= old_observed_at:
                    raise RoundTripSessionError(
                        "refresh Offer must be newer than its target Offer"
                    )
                admitted_at = raw_evidence.get("admitted_at")
                if admitted_at is not None:
                    try:
                        admission_time = offset_timestamp(
                            str(admitted_at), "evidence.admitted_at"
                        )
                        deadline = offer_price_deadline(
                            str(value.get("observed_at")),
                            str(value["expires_at"])
                            if value.get("expires_at") is not None
                            else None,
                        )
                    except ValueError as exc:
                        raise RoundTripSessionError(str(exc)) from exc
                    if deadline <= admission_time:
                        raise RoundTripSessionError(
                            "price refresh Evidence is already stale at Receipt admission"
                        )
            if offer.get("journey_scope") == "one_way":
                value["coverage_start_nodes"] = list(task["query_shape"]["origins"])
                value["coverage_end_nodes"] = list(task["query_shape"]["destinations"])
                value["coverage_departure_window"] = copy.deepcopy(
                    task["query_shape"].get(
                        "coverage_departure_window",
                        task["query_shape"].get("departure_window"),
                    )
                )
            if offer.get("journey_scope") == "surface":
                value["surface_role"] = task["query_shape"]["surface_role"]
            annotated.append(value)
        next_state["offers"] = _merge_unique(next_state["offers"], annotated, "offer_id")
        hypothesis_node = next_state["hypothesis_tree"]["nodes"].get(
            task["hypothesis_id"], {}
        )
        price_refresh = bool(
            (hypothesis_node.get("advisory_basis") or {}).get("price_refresh")
        )
        if not price_refresh:
            _publish_frontier_positioning_tasks(next_state, task["hypothesis_id"])
            _publish_exact_surface_tasks(next_state, task["hypothesis_id"])
        if next_task.get("temporal_state", {}).get("phase") != "not_applicable" and coverage_complete:
            next_task["temporal_state"]["phase"] = "terminal"
    if status in {"no_result", "rejected"} and coverage_complete and next_task.get("temporal_state", {}).get("phase") != "not_applicable":
        next_task["temporal_state"]["phase"] = "terminal"
    if native_pool_probe and status == "rejected":
        _publish_region_pool_fallback(next_state, next_task)
    next_state["search_round"] += 1
    _recompute(next_state)
    _invalidate_finalization(next_state)
    _commit_product_mutation(next_state)
    return next_state


def resolve_region_endpoints(
    raw_state: Any,
    endpoint_policies: dict[str, str],
    hypothesis_id: str,
    reason: str,
) -> dict[str, Any]:
    """Reinterpret an explicitly regional request without inventing surface tickets."""

    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    if not isinstance(endpoint_policies, dict):
        raise RoundTripSessionError("endpoint_policies must be an object")
    if hypothesis_id not in state["hypothesis_tree"]["nodes"]:
        raise RoundTripSessionError("region endpoint resolution requires a known hypothesis")
    node = state["hypothesis_tree"]["nodes"][hypothesis_id]
    if node.get("hypothesis_kind") != "complete_journey":
        raise RoundTripSessionError("region endpoint resolution requires a complete-journey hypothesis")
    for side in ("origin", "destination"):
        policy = endpoint_policies.get(side)
        if policy not in {"anchor_required", "any_admitted_gateway"}:
            raise RoundTripSessionError(f"unsupported {side} endpoint policy")
        pool = state["scenario"]["intent"][f"{side}_pool"]
        if pool.get("policy") == "exact" and policy != "anchor_required":
            raise RoundTripSessionError(f"exact {side} pool requires anchor_required")
        pool["endpoint_policy"] = policy
    for task in state["research_tasks"]:
        if task.get("journey_scope") != "surface":
            continue
        role = str(task.get("query_shape", {}).get("surface_role") or "")
        side = "origin" if role.startswith("origin-") else "destination" if role.startswith("destination-") else None
        if side and endpoint_policies.get(side) == "any_admitted_gateway":
            _set_requirement_state(task, "superseded")
            task["status"] = "superseded"
            task["terminal_reason"] = "regional endpoint is satisfied by an admitted gateway"
    node["status"] = "selected"
    node["terminal_reason"] = None
    node["was_selected"] = True
    state["active_hypothesis"] = {
        "hypothesis_id": hypothesis_id,
        "selected_task_id": None,
        "reason": reason,
    }
    _recompute(state)
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def dispose_inactive_hypothesis(
    raw_state: Any, hypothesis_id: str, disposition: str, reason: str
) -> dict[str, Any]:
    """Close a previously selected and paused hypothesis without browser work."""

    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    if state.get("active_hypothesis") is not None:
        raise RoundTripSessionError("inactive disposition requires no active hypothesis")
    if disposition not in {"hard_dead", "deferred_low_value"}:
        raise RoundTripSessionError("inactive disposition must be hard_dead or deferred_low_value")
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id)
    if not isinstance(node, dict) or node.get("status") != "candidate":
        raise RoundTripSessionError("inactive disposition requires a candidate hypothesis")
    was_selected = bool(node.get("was_selected"))
    if not was_selected:
        raise RoundTripSessionError("inactive disposition requires a previously selected hypothesis")
    node["status"] = disposition
    node["terminal_reason"] = reason
    node["terminal_at_round"] = state["search_round"]
    for task in state["research_tasks"]:
        if task.get("hypothesis_id") == hypothesis_id and task.get("requirement_status") == "open":
            _set_requirement_state(task, "superseded")
            task["status"] = "superseded"
            task["terminal_reason"] = reason
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    _recompute(state)
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def stop_bounded_research(
    raw_state: Any,
    reason: str,
    disposition: str = "threshold_stop",
) -> dict[str, Any]:
    """Close unfinished research when no affordable challenge remains."""

    state = validate_session(raw_state)
    if state.get("result", {}).get("best_portfolio") is None:
        raise RoundTripSessionError(
            "bounded research cannot complete without a verified winner"
        )
    terminal_reason = _text(reason, "bounded research stop reason")
    if disposition not in {"budget_stop", "threshold_stop"}:
        raise RoundTripSessionError("bounded research disposition is unsupported")
    active_id = state.get("active_hypothesis_id")
    if active_id:
        node = state["hypothesis_tree"]["nodes"].get(active_id)
        if isinstance(node, dict):
            node["status"] = "deferred_low_value"
            node["terminal_reason"] = terminal_reason
        unsearched_partitions = state["research_credits"].setdefault(
            "unsearched_partitions", []
        )
        for task in state.get("research_tasks", []):
            if (
                task.get("hypothesis_id") == active_id
                and task.get("requirement_status") == "open"
            ):
                task_id = str(task.get("task_id") or "")
                if task_id and task_id not in unsearched_partitions:
                    unsearched_partitions.append(task_id)
                _set_requirement_state(task, "superseded")
                task["status"] = "superseded"
                task["terminal_reason"] = terminal_reason
        state["active_hypothesis"] = None
    state["research_credits"]["stop_reason"] = terminal_reason
    state["research_credits"]["stop_disposition"] = disposition
    _synchronize_canonical_state(state)
    _rebuild_compatibility_projections(state)
    _recompute(state)
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def record_solver_projection_pins(
    raw_state: Any, offer_ids: list[str], reason: str
) -> dict[str, Any]:
    """Persist an explicit AI override for the bounded solver projection."""

    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    try:
        state = PROJECTION.set_pins(state, offer_ids, reason)
        _recompute(state)
    except PROJECTION.SolverProjectionError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    _invalidate_finalization(state)
    _commit_product_mutation(state)
    return state


def _finalization_audit(state: dict[str, Any]) -> dict[str, Any]:
    if "active_hypothesis" not in state:
        state = validate_session(state)
    blockers = []
    bounded_stop = bool(
        state.get("result", {}).get("best_portfolio") is not None
        and state.get("research_credits", {}).get("stop_reason")
    )
    projection = state.get("solver_projection", {})
    unresolved_projection = (
        projection.get("unresolved_competitive_alternative_offer_ids")
        or projection.get("unresolved_capacity_exclusion_offer_ids", [])
    )
    if unresolved_projection:
        blockers.append(
            "solver projection excludes unresolved competitive alternatives: "
            + ", ".join(sorted(str(item) for item in unresolved_projection))
        )
    if state["result"].get("best_portfolio") is None:
        blockers.append("no complete portfolio")
    if state["active_hypothesis"] is not None:
        blockers.append("active hypothesis remains")
    nodes = state["hypothesis_tree"]["nodes"]
    for gateway_id in (ORIGIN_GATEWAY_HYPOTHESIS, DESTINATION_GATEWAY_HYPOTHESIS):
        if nodes[gateway_id]["status"] not in TERMINAL_HYPOTHESIS_STATUSES:
            blockers.append(f"{gateway_id} lacks terminal disposition")
    for side in ("origin", "destination"):
        scope = state.get("region_scopes", {}).get(side, {})
        if scope.get("status") not in {"not_required", "defined", "legacy_complete"}:
            blockers.append(f"{side} region scope lacks AI definition")
        unresolved = sorted(
            node_id
            for node_id, status in scope.get("coverage", {}).items()
            if status not in TERMINAL_GATEWAY_COVERAGE
        )
        if unresolved and not bounded_stop:
            blockers.append(f"{side} region scope has unresearched gateways: {unresolved}")
    unsearched_scope_partitions = [
        str(partition.get("partition_id"))
        for partition in _scope_projection(state)["unsearched_partitions"]
        if partition.get("partition_id")
    ]
    if unsearched_scope_partitions and not bounded_stop:
        blockers.append(
            "Provider-visible scope has unsearched partitions: "
            + ", ".join(sorted(unsearched_scope_partitions))
        )
    selected = {
        hypothesis_id
        for hypothesis_id, node in state.get("hypotheses", {}).items()
        if node.get("was_selected")
    }
    unfinished = sorted(hid for hid in selected if nodes[hid]["status"] not in TERMINAL_HYPOTHESIS_STATUSES)
    if unfinished:
        blockers.append(f"selected hypotheses unfinished: {unfinished}")
    if not any(node["hypothesis_kind"] == "complete_journey" and node["status"] == "complete" for node in nodes.values()):
        blockers.append("no complete journey hypothesis")
    resolved_frontier_signal_ids = {
        (node.get("advisory_basis") or {}).get("signal_id")
        for node in nodes.values()
        if node.get("status") in TERMINAL_HYPOTHESIS_STATUSES
    }
    unresolved_frontier_signal_ids = sorted(
        str(signal.get("portfolio_challenger", {}).get("signal_id"))
        for side in state.get("gateway_candidates", {}).values()
        for candidate in side
        for signal in [candidate.get("signal", {})]
        if candidate.get("candidate_scope") in {
            "portfolio_frontier",
            "price_challenge",
        }
        and isinstance(signal.get("portfolio_challenger"), dict)
        and signal["portfolio_challenger"].get("signal_id")
        not in resolved_frontier_signal_ids
    )
    if unresolved_frontier_signal_ids and not bounded_stop:
        blockers.append(
            "competitive portfolio frontier signals remain unresolved: "
            + ", ".join(unresolved_frontier_signal_ids)
        )
    if state["result"].get("best_portfolio") is not None:
        evaluation_time = datetime.fromisoformat(
            _now().replace("Z", "+00:00")
        )
        stale_offer_ids = []
        for entry in _candidate_slate(state).get("entries", [])[:1]:
            for offer in entry.get("selected_offers", []):
                try:
                    deadline = offer_price_deadline(
                        str(offer.get("observed_at")),
                        str(offer["expires_at"])
                        if offer.get("expires_at") is not None
                        else None,
                    )
                except (TypeError, ValueError):
                    deadline = evaluation_time
                if deadline <= evaluation_time:
                    stale_offer_ids.append(str(offer.get("offer_id")))
        if stale_offer_ids:
            blockers.append(
                "rank-one Journey Alternative contains stale selected Offers: "
                + ", ".join(sorted(set(stale_offer_ids)))
            )
    pending = [
        task["task_id"]
        for task in state["research_tasks"]
        if task.get("requirement_status") == "open"
        and task.get("hypothesis_id") == state.get("active_hypothesis_id")
    ]
    if pending:
        blockers.append(f"unexplained pending tasks: {pending}")
    incumbent = (
        (state.get("result", {}).get("best_portfolio") or {})
        .get("total_mandatory_cost", {})
        .get("amount")
    )
    if (
        isinstance(incumbent, (int, float))
        and not isinstance(incumbent, bool)
        and int(state.get("research_credits", {}).get("remaining", 0)) >= 2
    ):
        screen_tasks = {
            str(task.get("task_id")): task
            for task in state.get("research_tasks", [])
            if task.get("research_kind") == "region_pool_fare_screen"
        }
        screen_task_ids = set(screen_tasks)
        resolved_signal_ids = {
            (node.get("advisory_basis") or {}).get("screen_signal_id")
            for node in nodes.values()
            if node.get("hypothesis_kind") == "complete_journey"
            and node.get("status") in {"complete", "hard_dead"}
        }
        base_currency = state.get("scenario", {}).get("base_currency")
        rates = (
            state.get("scenario", {}).get("fx_snapshot", {}).get("rates", {})
        )
        competitive = []
        for signal in state.get("temporal_signals", []):
            if (
                signal.get("research_task_id") not in screen_task_ids
                or signal.get("signal_kind") != "joint_date_pair"
                or signal.get("signal_id") in resolved_signal_ids
            ):
                continue
            money = signal.get("atomic_total", {})
            amount = money.get("amount")
            currency = money.get("currency")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                continue
            normalized = (
                float(amount)
                if currency == base_currency
                else float(amount) * float(rates[currency])
                if currency in rates
                else None
            )
            minimum_savings_ratio = float(
                screen_tasks[str(signal.get("research_task_id"))]
                .get("query_shape", {})
                .get("minimum_savings_ratio", 0.0)
            )
            if (
                normalized is None
                or normalized
                < float(incumbent) * (1.0 - minimum_savings_ratio)
            ):
                competitive.append(str(signal.get("signal_id")))
        if competitive:
            blockers.append(
                "unverified regional fare-screen candidates may beat the incumbent: "
                + ", ".join(sorted(competitive))
            )
    return {"can_finalize": not blockers, "blockers": blockers}


def _portfolio_comparison_total(portfolio: dict[str, Any]) -> dict[str, Any] | None:
    mandatory_total = portfolio.get("mandatory_air_ticket_total")
    if isinstance(mandatory_total, dict):
        return copy.deepcopy(mandatory_total)
    cost_scope = portfolio.get("cost_scope")
    if isinstance(cost_scope, dict) and isinstance(
        cost_scope.get("comparison_total"), dict
    ):
        return copy.deepcopy(cost_scope["comparison_total"])
    money = portfolio.get("total_mandatory_cost")
    return copy.deepcopy(money) if isinstance(money, dict) else None


def _offer_comparison_cost_in_base_currency(
    state: dict[str, Any], offer: dict[str, Any]
) -> float | None:
    movement_map = {
        str(item.get("movement_id")): item
        for item in state.get("movements", [])
        if isinstance(item, dict) and item.get("movement_id")
    }
    if not any(
        movement_map.get(str(movement_id), {}).get("mode") == "flight"
        for movement_id in offer.get("movement_ids", [])
    ):
        return 0.0
    values = [
        (
            _money_in_base_currency(state, offer[field])
            if isinstance(offer.get(field), dict)
            else 0.0
        )
        for field in ("price", "mandatory_baggage_cost")
    ]
    if any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values), 2)


def _seller_variants_for_portfolio(
    state: dict[str, Any],
    portfolio: dict[str, Any],
    offer_map: dict[str, dict[str, Any]],
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    """Return current Seller Variants nested under one Ticketing Structure."""

    selected_ids = [str(item) for item in portfolio.get("offer_ids", [])]

    base_total = (_portfolio_comparison_total(portfolio) or {}).get("amount")

    def variant(
        offer_ids: list[str], portfolio_id: str, total_override: float | None
    ) -> dict[str, Any]:
        offers = [offer_map[item] for item in offer_ids if item in offer_map]
        return {
            "portfolio_id": portfolio_id,
            "comparison_total": {
                "amount": total_override,
                "currency": state["scenario"]["base_currency"],
            },
            "offer_ids": offer_ids,
            "sellers": list(
                dict.fromkeys(
                    str(offer.get("seller"))
                    for offer in offers
                    if offer.get("seller")
                )
            ),
            "booking_urls": list(
                dict.fromkeys(
                    str(offer.get("url"))
                    for offer in offers
                    if offer.get("url")
                )
            ),
        }

    result = [
        variant(
            selected_ids,
            str(portfolio.get("portfolio_id")),
            float(base_total) if isinstance(base_total, (int, float)) else None,
        )
    ]
    represented = state.get("solver_projection", {}).get(
        "represented_offer_ids", {}
    )
    for selected_id in selected_ids:
        for alternative_id in represented.get(selected_id, []):
            alternative_id = str(alternative_id)
            if alternative_id == selected_id or alternative_id not in offer_map:
                continue
            alternative = offer_map[alternative_id]
            try:
                if offer_price_deadline(
                    str(alternative.get("observed_at")),
                    str(alternative["expires_at"])
                    if alternative.get("expires_at") is not None
                    else None,
                ) <= evaluated_at:
                    continue
            except (TypeError, ValueError):
                continue
            offer_ids = [
                alternative_id if item == selected_id else item
                for item in selected_ids
            ]
            selected_cost = _offer_comparison_cost_in_base_currency(
                state, offer_map[selected_id]
            )
            alternative_cost = _offer_comparison_cost_in_base_currency(
                state, alternative
            )
            alternative_total = (
                round(
                    float(base_total)
                    - float(selected_cost)
                    + float(alternative_cost),
                    2,
                )
                if isinstance(base_total, (int, float))
                and selected_cost is not None
                and alternative_cost is not None
                else None
            )
            result.append(
                variant(
                    offer_ids,
                    f"{portfolio.get('portfolio_id')}:seller:{alternative_id}",
                    alternative_total,
                )
            )
    result.sort(
        key=lambda item: (
            float(
                (item.get("comparison_total") or {}).get("amount")
                if isinstance(
                    (item.get("comparison_total") or {}).get("amount"),
                    (int, float),
                )
                else float("inf")
            ),
            str(item.get("portfolio_id")),
        )
    )
    return result


def _candidate_slate(state: dict[str, Any]) -> dict[str, Any]:
    """Project distinct complete Journey Alternatives already found."""

    offer_map = {
        str(offer.get("offer_id")): offer
        for offer in state.get("offers", [])
        if isinstance(offer, dict) and offer.get("offer_id")
    }
    movement_map = {
        str(movement.get("movement_id")): movement
        for movement in state.get("movements", [])
        if isinstance(movement, dict) and movement.get("movement_id")
    }
    grouped: dict[str, dict[str, Any]] = {}
    ordered_group_ids: list[str] = []
    for portfolio in state.get("result", {}).get("portfolios", []):
        if not isinstance(portfolio, dict):
            continue
        ordered_movement_ids = [
            *portfolio.get("outbound_movement_ids", []),
            *portfolio.get("return_movement_ids", []),
        ]
        evaluated_at = datetime.fromisoformat(_now().replace("Z", "+00:00"))
        route_alternative_id = IDENTITY.journey_alternative_id(
            portfolio, offer_map, movement_map
        )
        ticketing_structure_id = IDENTITY.portfolio_ticketing_structure_id(
            portfolio, offer_map, movement_map
        )
        seller_variants = _seller_variants_for_portfolio(
            state, portfolio, offer_map, evaluated_at
        )
        seller_variant = seller_variants[0]
        structure = {
            "ticketing_structure_id": ticketing_structure_id,
            "portfolio_id": portfolio.get("portfolio_id"),
            "portfolio_kind": portfolio.get("portfolio_kind"),
            "comparison_total": _portfolio_comparison_total(portfolio),
            "offer_ids": copy.deepcopy(portfolio.get("offer_ids", [])),
            "sellers": copy.deepcopy(seller_variant["sellers"]),
            "self_transfer_count": portfolio.get("self_transfer_count"),
            "travel_window_ids": copy.deepcopy(
                portfolio.get("travel_window_ids", [])
            ),
            "seller_variants": seller_variants,
        }
        group = grouped.get(route_alternative_id)
        if group is None:
            ordered_group_ids.append(route_alternative_id)
            selected_offers = [
                copy.deepcopy(offer_map[str(offer_id)])
                for offer_id in portfolio.get("offer_ids", [])
                if str(offer_id) in offer_map
            ]
            selected_movements = [
                copy.deepcopy(movement_map[str(movement_id)])
                for movement_id in ordered_movement_ids
                if str(movement_id) in movement_map
            ]
            observed_deadlines = []
            for selected_offer in selected_offers:
                try:
                    deadline = offer_price_deadline(
                        str(selected_offer.get("observed_at")),
                        str(selected_offer["expires_at"])
                        if selected_offer.get("expires_at") is not None
                        else None,
                    )
                except (TypeError, ValueError):
                    continue
                observed_deadlines.append(deadline)
            price_valid_until = (
                min(observed_deadlines).isoformat()
                if observed_deadlines
                else None
            )
            group = {
                "route_alternative_id": route_alternative_id,
                "portfolio_id": portfolio.get("portfolio_id"),
                "portfolio_kind": portfolio.get("portfolio_kind"),
                "comparison_total": _portfolio_comparison_total(portfolio),
                "mandatory_air_ticket_total": _portfolio_comparison_total(
                    portfolio
                ),
                "cost_scope": copy.deepcopy(portfolio.get("cost_scope")),
                "outbound": {
                    "departure": portfolio.get("departure"),
                    "arrival": portfolio.get("outbound_arrival"),
                    "movement_ids": copy.deepcopy(
                        portfolio.get("outbound_movement_ids", [])
                    ),
                },
                "return": {
                    "departure": portfolio.get("return_departure"),
                    "arrival": portfolio.get("arrival"),
                    "movement_ids": copy.deepcopy(
                        portfolio.get("return_movement_ids", [])
                    ),
                },
                "total_elapsed_minutes": portfolio.get("total_elapsed_minutes"),
                "self_transfer_count": portfolio.get("self_transfer_count"),
                "overnight_connection_count": portfolio.get(
                    "overnight_connection_count"
                ),
                "travel_window_ids": copy.deepcopy(
                    portfolio.get("travel_window_ids", [])
                ),
                "selected_offers": selected_offers,
                "selected_movements": selected_movements,
                "internal_surface_transfers": copy.deepcopy(
                    portfolio.get("internal_surface_transfers", [])
                ),
                "evidence_freshness": {
                    "status": (
                        "current"
                        if observed_deadlines
                        and min(observed_deadlines) > evaluated_at
                        else "stale"
                    ),
                    "price_valid_until": price_valid_until,
                    "evaluated_at": evaluated_at.isoformat(),
                },
                "ticketing_structures": [structure],
            }
            grouped[route_alternative_id] = group
        else:
            matching_structure = next(
                (
                    item
                    for item in group["ticketing_structures"]
                    if item.get("ticketing_structure_id")
                    == ticketing_structure_id
                ),
                None,
            )
            if matching_structure is None:
                group["ticketing_structures"].append(structure)
            else:
                existing_variant_ids = {
                    item.get("portfolio_id")
                    for item in matching_structure.get("seller_variants", [])
                }
                matching_structure.setdefault("seller_variants", []).extend(
                    item
                    for item in seller_variants
                    if item.get("portfolio_id") not in existing_variant_ids
                )

    entries = [grouped[key] for key in ordered_group_ids]
    rank_one_amount = None
    for rank, entry in enumerate(entries, start=1):
        entry["rank"] = rank
        money = entry.get("comparison_total") or {}
        amount = money.get("amount")
        if rank_one_amount is None and isinstance(amount, (int, float)):
            rank_one_amount = float(amount)
        if rank_one_amount is not None and isinstance(amount, (int, float)):
            entry["price_delta_from_rank_1"] = {
                "amount": round(float(amount) - rank_one_amount, 2),
                "currency": money.get("currency"),
            }
    projection = state.get("solver_projection", {})
    projection_complete = projection.get("status") == "complete_ledger"
    return {
        "candidate_slate_version": "2.0",
        "status": "bounded_complete",
        "ranking_method": "mandatory_air_ticket_total_ascending",
        "ranking_scope": (
            "complete_evidence_ledger"
            if projection_complete
            else "bounded_solver_projection"
        ),
        "entries": entries,
    }


def _unresolved_low_price_signal_ids(
    state: dict[str, Any], best_portfolio: dict[str, Any] | None
) -> list[str]:
    incumbent_amount = (
        (best_portfolio or {}).get("total_mandatory_cost", {}).get("amount")
        if isinstance(best_portfolio, dict)
        else None
    )
    challenge_ceiling = (
        float(incumbent_amount)
        * (1.0 - float(state["research_credits"]["price_challenge_threshold"]))
        if isinstance(incumbent_amount, (int, float))
        and not isinstance(incumbent_amount, bool)
        else None
    )
    resolved_signal_ids = {
        str((node.get("advisory_basis") or {}).get("screen_signal_id"))
        for node in state.get("hypothesis_tree", {}).get("nodes", {}).values()
        if isinstance(node, dict)
        and node.get("status") in {"complete", "hard_dead", "deferred_low_value"}
        and (node.get("advisory_basis") or {}).get("screen_signal_id")
    }
    unresolved = []
    for signal in state.get("temporal_signals", []):
        signal_id = signal.get("signal_id")
        if (
            not signal_id
            or signal.get("signal_kind") != "joint_date_pair"
            or str(signal_id) in resolved_signal_ids
        ):
            continue
        normalized = _money_in_base_currency(
            state, signal.get("atomic_total", {})
        )
        if (
            challenge_ceiling is None
            or normalized is None
            or normalized < challenge_ceiling
        ):
            unresolved.append(str(signal_id))
    return unresolved


def _unsearched_partition_ids(state: dict[str, Any]) -> list[str]:
    """Return Provider partitions and other requirements that remain unsearched."""

    scope = _scope_projection(state)
    result = [
        str(partition.get("partition_id"))
        for partition in scope["unsearched_partitions"]
        if partition.get("partition_id")
    ]
    result.extend(
        str(task_id)
        for task_id in state.get("research_credits", {}).get(
            "unsearched_partitions", []
        )
        if str(task_id) and str(task_id) not in result
    )
    for task in state.get("research_tasks", []):
        if not isinstance(task, dict) or not task.get("required", True):
            continue
        if task.get("research_kind") == "region_pool_fare_screen":
            continue
        requirement_status = task.get("requirement_status")
        attempt_count = int(task.get("attempt_count", 0))
        if requirement_status == "open" or (
            requirement_status == "superseded" and attempt_count == 0
        ):
            task_id = str(task.get("task_id") or "")
            if task_id and task_id not in result:
                result.append(task_id)
    return result


def export_result(raw_state: Any, *, search_status: str, reason: str | None = None) -> dict[str, Any]:
    state = validate_session(raw_state)
    audit = _finalization_audit(state)
    if search_status == "final" and not audit["can_finalize"]:
        raise RoundTripSessionError(f"cannot finalize: {audit['blockers']}")
    best_portfolio = copy.deepcopy(state["result"].get("best_portfolio"))
    if isinstance(best_portfolio, dict):
        comparison_total = _portfolio_comparison_total(best_portfolio)
        mandatory_total = best_portfolio.get("total_mandatory_cost")
        if comparison_total != mandatory_total:
            raise RoundTripSessionError(
                "best Portfolio comparison_total conflicts with mandatory total"
            )
    candidate_slate = _candidate_slate(state)
    scope_projection = _scope_projection(state)
    selected_movements = []
    price_observations = []
    if isinstance(best_portfolio, dict):
        offer_by_id = {
            offer["offer_id"]: offer
            for offer in state.get("offers", [])
            if isinstance(offer, dict) and offer.get("offer_id")
        }
        ranked_offer_ids = list(
            dict.fromkeys(
                str(offer_id)
                for entry in candidate_slate["entries"][:1]
                for offer_id in (
                    selected.get("offer_id")
                    for selected in entry.get("selected_offers", [])
                )
                if offer_id
            )
        )
        for offer_id in ranked_offer_ids:
            offer = offer_by_id.get(offer_id)
            if not isinstance(offer, dict) or not offer.get("observed_at"):
                continue
            price_observations.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in {
                        "offer_id": offer_id,
                        "observed_at": offer.get("observed_at"),
                        "expires_at": offer.get("expires_at"),
                    }.items()
                    if value is not None
                }
            )
        movement_by_id = {
            movement["movement_id"]: movement
            for movement in state.get("movements", [])
            if isinstance(movement, dict) and movement.get("movement_id")
        }
        for direction, field in (
            ("outbound", "outbound_movement_ids"),
            ("return", "return_movement_ids"),
        ):
            for movement_id in best_portfolio.get(field, []):
                movement = movement_by_id.get(movement_id)
                if movement is None:
                    continue
                selected_movements.append(
                    {
                        key: copy.deepcopy(value)
                        for key, value in {
                            "movement_id": movement.get("movement_id"),
                            "direction": direction,
                            "from_node": movement.get("from_node"),
                            "to_node": movement.get("to_node"),
                            "departure": movement.get("departure"),
                            "arrival": movement.get("arrival"),
                            "mode": movement.get("mode"),
                            "operator": movement.get("operator"),
                            "service_id": movement.get("service_id"),
                            "distance_km": movement.get("distance_km"),
                            "transfer_from_previous": movement.get(
                                "transfer_from_previous"
                            ),
                        }.items()
                        if value is not None
                    }
                )
    return {
        "result_version": SESSION_VERSION,
        "session_id": state["session_id"],
        "trip_scope": TRIP_SCOPE,
        "search_status": search_status,
        "incomplete": search_status != "final",
        "reason": reason,
        "best_portfolio": best_portfolio,
        "candidate_slate": candidate_slate,
        "cost_scope": (
            copy.deepcopy(best_portfolio.get("cost_scope"))
            if isinstance(best_portfolio, dict)
            else None
        ),
        "price_observations": price_observations,
        "journey_movements": selected_movements,
        "portfolios": copy.deepcopy(state["result"].get("portfolios", [])),
        "portfolio_kind_counts": copy.deepcopy(state["result"].get("portfolio_kind_counts", {})),
        "gateway_candidates": copy.deepcopy(state["gateway_candidates"]),
        "region_scopes": copy.deepcopy(state.get("region_scopes", {})),
        "temporal_research": {
            "signal_count": len(state.get("temporal_signals", [])),
            "signals": copy.deepcopy(state.get("temporal_signals", [])),
            "ai_selections": [
                {"task_id": task["task_id"], **copy.deepcopy(selection)}
                for task in state["research_tasks"]
                for selection in task.get("temporal_state", {}).get("selection_history", [])
            ],
            "calendar_signals_promoted_to_payable_offers": 0,
        },
        "offer_sets": _derive_offer_sets(state["offers"]),
        "solver_projection": copy.deepcopy(state.get("solver_projection", {})),
        "hypothesis_outcomes": _derive_hypothesis_outcomes(state),
        "finalization_audit": audit,
        "search_stopping": {
            "disposition": (
                state["research_credits"].get("stop_disposition")
                or (
                    "budget_stop"
                    if int(state["research_credits"].get("remaining", 0)) < 1
                    else "threshold_stop"
                )
            ),
            "reason": (
                state["research_credits"].get("stop_reason")
                or "No unresearched candidate clears the active Price Challenge Threshold."
            ),
            "search_effort": state["research_credits"]["search_effort"],
            "research_credit_budget": state["research_credits"][
                "research_credit_budget"
            ],
            "research_credits_used": state["research_credits"]["used"],
            "research_credits_remaining": state["research_credits"]["remaining"],
            "price_refresh_credits_used": state["research_credits"].get(
                "price_refresh_used", 0
            ),
            "price_challenge_threshold": state["research_credits"][
                "price_challenge_threshold"
            ],
        },
        "searched_scope": {
            "self_access_gateway_pool": copy.deepcopy(
                scope_projection["semantic_self_access_gateway_pool"]
            ),
            "requested_travel_windows": copy.deepcopy(
                scope_projection["requested_travel_windows"]
            ),
            "gateway_coverage": copy.deepcopy(
                scope_projection["gateway_coverage"]
            ),
            "provider_visible_partitions": copy.deepcopy(
                scope_projection["provider_visible_partitions"]
            ),
            "unsupported_provider_partitions": copy.deepcopy(
                scope_projection["unsupported_provider_partitions"]
            ),
            "search_effort": state["research_credits"]["search_effort"],
            "research_credit_budget": state["research_credits"][
                "research_credit_budget"
            ],
        },
        "unsearched_scope": {
            "claim": "The winner is bounded by the disclosed scope and Research Credit budget, not a global minimum.",
            "unsearched_partitions": _unsearched_partition_ids(state),
            "partitions": copy.deepcopy(
                scope_projection["unsearched_partitions"]
            ),
            "unproved_gateway_node_ids": copy.deepcopy(
                scope_projection["unproved_gateway_node_ids"]
            ),
            "unresolved_low_price_signals": _unresolved_low_price_signal_ids(
                state, best_portfolio
            ),
        },
    }


def finalize_session(raw_state: Any, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = validate_session(raw_state)
    _refresh_time_sensitive_state(state)
    state["finalization_status"] = "final"
    _commit_product_mutation(state)
    result = export_result(state, search_status="final", reason=_text(reason, "reason"))
    return state, result


def reopen_for_price_refresh(raw_state: Any) -> dict[str, Any]:
    """Reopen one finalized Session without reopening discovery scope."""

    state = validate_session(raw_state)
    if state.get("result", {}).get("best_portfolio") is None:
        raise RoundTripSessionError(
            "price refresh requires a verified rank-one Journey Alternative"
        )
    if state.get("finalization_status") != "final":
        return state
    state["finalization_status"] = "open"
    state.pop("finalization_reason", None)
    _commit_product_mutation(state)
    return state


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_atomic(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    state = validate_session(state)
    return {
        "session_id": state["session_id"],
        "search_round": state["search_round"],
        "active_hypothesis": state["active_hypothesis"],
        "next_candidate_id": state["hypothesis_tree"]["next_candidate_id"],
        "gateway_candidate_counts": {side: len(items) for side, items in state["gateway_candidates"].items()},
        "region_scope_status": {
            side: {
                "status": scope.get("status"),
                "candidate_count": len(scope.get("candidate_records", [])),
                "unresearched_node_ids": sorted(
                    node_id
                    for node_id, status in scope.get("coverage", {}).items()
                    if status == "pending"
                ),
            }
            for side, scope in state.get("region_scopes", {}).items()
        },
        "best_portfolio": state["result"].get("best_portfolio"),
        "finalization_status": state["finalization_status"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--scenario", required=True)
    init.add_argument("--state", required=True)
    add = sub.add_parser("add-hypothesis")
    add.add_argument("--state", required=True)
    add.add_argument("--spec", required=True)
    decision = sub.add_parser("hypothesis")
    decision.add_argument("--state", required=True)
    decision.add_argument("--action", required=True, choices=["select", "complete", "hard_dead", "deferred_low_value", "pause"])
    decision.add_argument("--task-id")
    decision.add_argument("--reason", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--evidence", required=True)
    temporal = sub.add_parser("temporal-select")
    temporal.add_argument("--state", required=True)
    temporal.add_argument("--task-id", required=True)
    temporal.add_argument("--spec", required=True)
    temporal.add_argument("--reason", required=True)
    projection = sub.add_parser("projection-pin")
    projection.add_argument("--state", required=True)
    projection.add_argument("--offer-id", action="append", default=[])
    projection.add_argument("--reason", required=True)
    draft = sub.add_parser("export-draft")
    draft.add_argument("--state", required=True)
    draft.add_argument("--output", required=True)
    final = sub.add_parser("finalize-result")
    final.add_argument("--state", required=True)
    final.add_argument("--output", required=True)
    final.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            state = initialize_session(read_json(args.scenario))
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "add-hypothesis":
            state = add_route_hypothesis(read_json(args.state), read_json(args.spec))
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "hypothesis":
            state = record_hypothesis_decision(read_json(args.state), args.action, task_id=args.task_id, reason=args.reason)
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "ingest":
            state = ingest_evidence(read_json(args.state), read_json(args.evidence))
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "temporal-select":
            state = record_temporal_selection(read_json(args.state), args.task_id, read_json(args.spec), args.reason)
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "projection-pin":
            state = record_solver_projection_pins(
                read_json(args.state), args.offer_id, args.reason
            )
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False))
        elif args.command == "export-draft":
            result = export_result(read_json(args.state), search_status="draft")
            write_json_atomic(args.output, result)
            print(json.dumps({"search_status": "draft", "output": _display_path(args.output)}))
        else:
            state, result = finalize_session(read_json(args.state), args.reason)
            write_json_atomic(args.state, state)
            write_json_atomic(args.output, result)
            print(json.dumps({"search_status": "final", "output": _display_path(args.output)}))
        return 0
    except (OSError, json.JSONDecodeError, RoundTripSessionError) as exc:
        print(f"round_trip_session: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
