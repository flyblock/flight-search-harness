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


SESSION_VERSION = "0.4.0"
TRIP_SCOPE = "round_trip"
ROOT_HYPOTHESIS = "hypothesis-root"
ORIGIN_GATEWAY_HYPOTHESIS = "hypothesis-gateway-origin"
DESTINATION_GATEWAY_HYPOTHESIS = "hypothesis-gateway-destination"
STRUCTURAL_DISCOVERY_HYPOTHESIS = "hypothesis-structural-challenger-discovery"
TERMINAL_HYPOTHESIS_STATUSES = {"complete", "hard_dead", "deferred_low_value"}
REQUIREMENT_STATUSES = {"open", "satisfied", "exhausted", "superseded"}


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


def _load_portfolio_exploration():
    path = Path(__file__).with_name("round_trip_portfolio_exploration.py")
    spec = importlib.util.spec_from_file_location(
        "round_trip_portfolio_exploration", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PORTFOLIO_EXPLORATION = _load_portfolio_exploration()


def _load_ai_exploration():
    path = Path(__file__).with_name("round_trip_ai_exploration.py")
    spec = importlib.util.spec_from_file_location("round_trip_ai_exploration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


AI_EXPLORATION = _load_ai_exploration()


class RoundTripSessionError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoundTripSessionError(f"{field} must be a non-empty string")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    return {
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


def _gateway_task(side: str, scenario: dict[str, Any]) -> dict[str, Any]:
    intent = scenario["intent"]
    pool = intent[f"{side}_pool"]
    anchor_ids = intent[f"{side}_anchor_nodes"]
    return _task(
        f"task-gateway-{side}-g1",
        f"hypothesis-gateway-{side}",
        "gateway_discovery",
        f"{side}_regional_gateway_pool",
        "gateway_discovery",
        f"Which materially useful gateways are reachable from the declared {side} city/region anchor within its pool constraints?",
        {
            "trip_type": "gateway_discovery",
            "side": side,
            "anchor_node_ids": anchor_ids,
            "pool": copy.deepcopy(pool),
            "travel_windows": {
                "outbound": intent["outbound_departure_window"],
                "return": intent["return_departure_window"],
            },
            "date_flexibility": copy.deepcopy(intent["date_flexibility"]),
        },
    )


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


def _initial_control_plane(scenario: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, Any]]:
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
    nodes[STRUCTURAL_DISCOVERY_HYPOTHESIS] = _tree_node(
        STRUCTURAL_DISCOVERY_HYPOTHESIS,
        "structural_challenger_discovery",
        "ai_structural_corridor_scope",
        3,
        "dormant",
    )
    tree = {
        "tree_version": SESSION_VERSION,
        "root_hypothesis_id": ROOT_HYPOTHESIS,
        "nodes": nodes,
        "next_candidate_id": next((hid for hid in (ORIGIN_GATEWAY_HYPOTHESIS, DESTINATION_GATEWAY_HYPOTHESIS) if nodes[hid]["status"] == "candidate"), None),
    }
    return (
        tasks,
        tree,
        gateway_candidates,
        region_scopes,
        AI_EXPLORATION.initial_structural_exploration(),
    )


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

    dispositions = copy.deepcopy(state.get("challenger_dispositions", {}))
    structural = state.get("structural_exploration", {})
    structural_history = {
        str(item.get("proposal_id")): item
        for item in structural.get("disposition_history", [])
        if isinstance(item, dict) and item.get("proposal_id")
    }
    for proposal_id, status in structural.get("dispositions", {}).items():
        key = f"structural:{proposal_id}"
        existing = dispositions.get(key, {})
        history = structural_history.get(str(proposal_id), {})
        dispositions[key] = {
            "challenger_kind": "structural",
            "challenger_id": str(proposal_id),
            "status": status,
            "reason": history.get("reason", existing.get("reason")),
            "hypothesis_id": history.get(
                "hypothesis_id", existing.get("hypothesis_id")
            ),
            "at": history.get("at", existing.get("at")),
        }
    for challenger_id, disposition in state.get(
        "portfolio_challenger_dispositions", {}
    ).items():
        key = f"portfolio:{challenger_id}"
        value = copy.deepcopy(disposition)
        value.update(
            {
                "challenger_kind": "portfolio",
                "challenger_id": str(challenger_id),
            }
        )
        dispositions[key] = value
    state["challenger_dispositions"] = dispositions
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

    dispositions = state.get("challenger_dispositions", {})
    structural = state.get("structural_exploration", {})
    if isinstance(structural, dict):
        projected_structural = structural.get("dispositions", {})
        proposal_ids = {
            str(item.get("proposal_id"))
            for item in structural.get("proposals", [])
            if isinstance(item, dict) and item.get("proposal_id")
        }
        structural["dispositions"] = {
            proposal_id: dispositions.get(
                f"structural:{proposal_id}",
                {"status": projected_structural.get(proposal_id, "pending")},
            ).get("status", projected_structural.get(proposal_id, "pending"))
            for proposal_id in sorted(proposal_ids | set(projected_structural))
        }
        structural["disposition_history"] = [
            {
                "proposal_id": value.get("challenger_id"),
                "hypothesis_id": value.get("hypothesis_id"),
                "disposition": value.get("status"),
                "reason": value.get("reason"),
                "at": value.get("at"),
            }
            for key, value in sorted(dispositions.items())
            if key.startswith("structural:") and value.get("status") != "pending"
        ]
    state["portfolio_challenger_dispositions"] = {
        str(value.get("challenger_id")): {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"challenger_kind", "challenger_id"}
        }
        for key, value in sorted(dispositions.items())
        if key.startswith("portfolio:")
    }
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
        "portfolio_challenger_dispositions",
        "portfolio_exploration_archive",
        "portfolio_exploration_view",
    ):
        state.pop(field, None)
    for task in state.get("research_tasks", []):
        task.pop("status", None)
    structural = state.get("structural_exploration")
    if isinstance(structural, dict):
        structural.pop("dispositions", None)
        structural.pop("disposition_history", None)
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


def _refresh_portfolio_exploration(state: dict[str, Any]) -> None:
    try:
        archive = PORTFOLIO_EXPLORATION.build_archive(state)
        view = PORTFOLIO_EXPLORATION.build_view(state, archive)
    except PORTFOLIO_EXPLORATION.PortfolioExplorationError as exc:
        raise RoundTripSessionError(f"portfolio exploration failed: {exc}") from exc
    state["portfolio_exploration_archive"] = archive
    state["portfolio_exploration_view"] = view


def initialize_session(raw_scenario: Any) -> dict[str, Any]:
    scenario = _validate_scenario(raw_scenario)
    tasks, tree, gateway_candidates, region_scopes, structural_exploration = (
        _initial_control_plane(scenario)
    )
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
        "structural_exploration": structural_exploration,
        "temporal_signals": [],
        "evidence_bundles": [],
        "movements": [],
        "offers": [],
        "solver_projection": {
            "projection_version": PROJECTION.PROJECTION_VERSION,
            "status": "complete_ledger",
            "ledger_offer_count": 0,
            "solver_offer_limit": scenario["intent"]["max_solver_offers"],
            "pinned_offer_ids": [],
            "selected_offer_ids": [],
            "excluded_offer_ids": [],
            "selection_reasons": {},
            "coverage_claim": "solver result is complete only within selected_offer_ids; excluded ledger evidence remains persisted",
        },
        "portfolio_challenger_dispositions": {},
        "portfolio_exploration_archive": {},
        "portfolio_exploration_view": {},
        "result": _empty_result(scenario["base_currency"]),
        "stop_assessment": None,
        "finalization_status": "open",
    }
    _synchronize_canonical_state(state)
    _rebuild_compatibility_projections(state)
    _refresh_portfolio_exploration(state)
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
    state.setdefault("solver_projection", {"pinned_offer_ids": []})
    state.setdefault("portfolio_challenger_dispositions", {})
    missing_portfolio_views = (
        "portfolio_exploration_archive" not in state
        or "portfolio_exploration_view" not in state
    )
    state.setdefault("portfolio_exploration_archive", {})
    state.setdefault("portfolio_exploration_view", {})
    if "hypothesis_tree" not in state:
        state.setdefault("active_hypothesis_id", None)
        state.setdefault("active_requirement_id", None)
        state.setdefault("active_hypothesis_reason", None)
        state.setdefault("challenger_dispositions", {})
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
    if "structural_exploration" not in state:
        migrated = True
        state["structural_exploration"] = AI_EXPLORATION.initial_structural_exploration()
        if state.get("finalization_status") == "final":
            state["structural_exploration"].update(
                {
                    "status": "legacy_complete",
                    "no_material_challenger_reason": "Migrated finalized Session predates the structural exploration contract.",
                }
            )
    if STRUCTURAL_DISCOVERY_HYPOTHESIS not in state["hypothesis_tree"]["nodes"]:
        migrated = True
        structural_node = _tree_node(
            STRUCTURAL_DISCOVERY_HYPOTHESIS,
            "structural_challenger_discovery",
            "ai_structural_corridor_scope",
            3,
            "complete" if state.get("finalization_status") == "final" else "dormant",
        )
        state["hypothesis_tree"]["nodes"][STRUCTURAL_DISCOVERY_HYPOTHESIS] = structural_node
    for task in state["research_tasks"]:
        task.setdefault(
            "temporal_state",
            _new_temporal_state(task.get("research_kind"), task.get("journey_scope"), task.get("query_shape", {})),
        )
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
        challenge_task_id = f"task-region-scope-challenge-{side}-g1"
        if (
            state.get("finalization_status") != "final"
            and pool.get("policy") != "exact"
            and scope.get("status") == "defined"
            and not scope.get("challenge_history")
            and not any(
                task.get("task_id") == challenge_task_id
                for task in state["research_tasks"]
            )
        ):
            migrated = True
            scope["status"] = "defined_pending_challenge"
            _publish_region_scope_challenge_task(state, side, scope)
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    if "hypotheses" not in state:
        _synchronize_canonical_state(state)
    else:
        state.setdefault("active_hypothesis_id", None)
        state.setdefault("active_requirement_id", None)
        state.setdefault("active_hypothesis_reason", None)
        state.setdefault("challenger_dispositions", {})
        _rebuild_compatibility_projections(state)
    for field in (
        "query_ledger",
        "offer_sets",
        "portfolio_challenger_history",
        "finalization_history",
    ):
        state.pop(field, None)
    if migrated or missing_portfolio_views:
        _refresh_portfolio_exploration(state)
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
        "challenger_dispositions",
        "region_scopes",
        "structural_exploration",
        "portfolio_exploration_archive",
        "portfolio_exploration_view",
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


def derive_session_views(raw_state: Any) -> dict[str, Any]:
    """Rebuild disposable portfolio projections without changing source state."""

    state = validate_session(raw_state)
    _refresh_portfolio_exploration(state)
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
    state["stop_assessment"] = None
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
                ("origin-access-outbound", intent["origin_anchor_nodes"], origin_gateways, intent["origin_pool"], "max_access_time_minutes", "outbound"),
                ("origin-egress-return", origin_gateways, intent["origin_anchor_nodes"], intent["origin_pool"], "max_egress_time_minutes", "return"),
            ]
        )
    if intent["destination_pool"].get("endpoint_policy", "anchor_required") == "anchor_required":
        legs.extend(
            [
                ("destination-egress-outbound", destination_gateways, intent["destination_anchor_nodes"], intent["destination_pool"], "max_egress_time_minutes", "outbound"),
                ("destination-access-return", intent["destination_anchor_nodes"], destination_gateways, intent["destination_pool"], "max_access_time_minutes", "return"),
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
                        "exact_departure_dates": [schedule[suffix]["service_date"]],
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
    buffer_minutes = int(intent["mode_transition_buffers_minutes"].get("surface_to_flight", 120))

    def access_window(flight: dict[str, Any], pool: dict[str, Any]) -> dict[str, str]:
        departure = datetime.fromisoformat(flight["departure"].replace("Z", "+00:00"))
        deadline = departure - timedelta(minutes=buffer_minutes)
        start = deadline - timedelta(minutes=int(pool["max_access_time_minutes"]))
        return {
            "departure_window": {"start": start.isoformat(), "end": deadline.isoformat()},
            "must_arrive_by": deadline.isoformat(),
            "service_date": start.date().isoformat(),
            "flight_movement_id": flight["movement_id"],
        }

    def egress_window(flight: dict[str, Any], pool: dict[str, Any]) -> dict[str, str]:
        start = datetime.fromisoformat(flight["arrival"].replace("Z", "+00:00"))
        end = start + timedelta(minutes=int(pool["max_egress_time_minutes"]))
        return {
            "departure_window": {"start": start.isoformat(), "end": end.isoformat()},
            "not_before": start.isoformat(),
            "service_date": start.date().isoformat(),
            "flight_movement_id": flight["movement_id"],
        }

    return {
        "origin-access-outbound": access_window(outbound_first, intent["origin_pool"]),
        "destination-egress-outbound": egress_window(outbound_last, intent["destination_pool"]),
        "destination-access-return": access_window(return_first, intent["destination_pool"]),
        "origin-egress-return": egress_window(return_last, intent["origin_pool"]),
    }


def _publish_exact_surface_tasks(state: dict[str, Any], hypothesis_id: str) -> None:
    existing = {task["task_id"] for task in state["research_tasks"]}
    schedule = _exact_surface_schedule(state, hypothesis_id)
    if schedule is None:
        return
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id, {})
    gateways = node.get("gateway_nodes", {})
    specs = _surface_task_specs(
        state,
        hypothesis_id,
        list(gateways.get("origin", [])),
        list(gateways.get("destination", [])),
        schedule,
    )
    for index, spec in enumerate(specs, 1):
        if spec["task_id"] in existing:
            continue
        task = _task(
            spec["task_id"],
            hypothesis_id,
            "complete_journey",
            node.get("route_shape"),
            spec["research_kind"],
            spec["question"],
            spec["query_shape"],
            journey_scope=spec["journey_scope"],
            required=spec.get("required", True),
        )
        active = state.get("active_hypothesis")
        task["status"] = (
            "pending"
            if isinstance(active, dict) and active.get("hypothesis_id") == hypothesis_id
            else "candidate"
        )
        task["research_order"] = 100 + index
        state["research_tasks"].append(task)


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
        task_specs = [
            {
                "task_id": f"task-{hypothesis_id.removeprefix('hypothesis-')}-joint",
                "research_kind": "priced_offer",
                "journey_scope": "joint_round_trip",
                "question": "Research multiple useful joint date-pair signals in the allowed windows, then verify payable atomic round-trip quotes selected by the AI for this gateway hypothesis.",
                "query_shape": {"trip_type": "round_trip", "origins": origin_gateways, "destinations": destination_gateways, "outbound_departure_window": intent["outbound_departure_window"], "return_departure_window": intent["return_departure_window"], "date_search": _joint_date_search(intent)},
                "required": True,
            },
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
    state["stop_assessment"] = None
    _refresh_portfolio_exploration(state)
    _commit_product_mutation(state)
    return state


def activate_portfolio_challenger(
    raw_state: Any, challenger_id: str, reason: str
) -> dict[str, Any]:
    """Lazily register and select one AI-chosen complete-portfolio challenger."""

    state = validate_session(raw_state)
    reason = _text(reason, "reason")
    if state.get("active_hypothesis") is not None:
        raise RoundTripSessionError(
            "portfolio challenger activation requires no active hypothesis"
        )
    try:
        challenger = PORTFOLIO_EXPLORATION.challenger_by_id(
            state["portfolio_exploration_archive"], challenger_id
        )
    except PORTFOLIO_EXPLORATION.PortfolioExplorationError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    hypothesis_id = str(challenger["hypothesis_id"])
    if challenger["registration_state"] == "suggested":
        state = validate_session(
            add_route_hypothesis(state, challenger["hypothesis_spec"])
        )
    node = state["hypothesis_tree"]["nodes"].get(hypothesis_id)
    if not isinstance(node, dict) or node.get("status") != "candidate":
        raise RoundTripSessionError(
            "portfolio challenger must identify a candidate complete-journey hypothesis"
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
        raise RoundTripSessionError(
            "portfolio challenger has no actionable research task"
        )
    state = validate_session(
        record_hypothesis_decision(
            state,
            "select",
            task_id=str(selected_task["task_id"]),
            reason=reason,
        )
    )
    state.setdefault("portfolio_challenger_dispositions", {})[challenger_id] = {
        "status": "activated",
        "reason": reason,
        "hypothesis_id": hypothesis_id,
        "search_round": state["search_round"],
        "at": _now(),
    }
    proposal_id = (challenger.get("advisory_basis") or {}).get("proposal_id")
    if proposal_id in state.get("structural_exploration", {}).get(
        "dispositions", {}
    ):
        state["structural_exploration"]["dispositions"][proposal_id] = "activated"
    _refresh_portfolio_exploration(state)
    _commit_product_mutation(state)
    return state


def dismiss_portfolio_challenger(
    raw_state: Any, challenger_id: str, reason: str
) -> dict[str, Any]:
    """Persist an explicit disposition for one recommended portfolio challenger."""

    state = validate_session(raw_state)
    challenger_id = _text(challenger_id, "challenger_id")
    reason = _text(reason, "reason")
    try:
        challenger = PORTFOLIO_EXPLORATION.challenger_by_id(
            state["portfolio_exploration_archive"], challenger_id
        )
    except PORTFOLIO_EXPLORATION.PortfolioExplorationError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    dispositions = state.setdefault("portfolio_challenger_dispositions", {})
    existing = dispositions.get(challenger_id)
    if isinstance(existing, dict) and existing.get("status") == "dismissed":
        return _compact_session(state)
    if challenger.get("registration_state") != "suggested":
        raise RoundTripSessionError(
            "only an unregistered suggested portfolio challenger may be dismissed"
        )
    dispositions[challenger_id] = {
        "status": "dismissed",
        "reason": reason,
        "hypothesis_id": challenger.get("hypothesis_id"),
        "search_round": state["search_round"],
        "at": _now(),
    }
    _invalidate_finalization(state)
    state["stop_assessment"] = None
    _refresh_portfolio_exploration(state)
    _commit_product_mutation(state)
    return state


def unresolved_recommended_challenger_ids(raw_state: Any) -> list[str]:
    state = validate_session(raw_state)
    archive = state.get("portfolio_exploration_archive", {})
    view = state.get("portfolio_exploration_view", {})
    incumbent_id = (state.get("result", {}).get("best_portfolio") or {}).get(
        "portfolio_id"
    )
    dispositions = state.get("portfolio_challenger_dispositions", {})
    unresolved = []
    for challenger_id in view.get("recommended_challenger_ids", []):
        challenger = PORTFOLIO_EXPLORATION.challenger_by_id(archive, challenger_id)
        if incumbent_id and incumbent_id in challenger.get("portfolio_ids", []):
            continue
        if challenger.get("portfolio_ids"):
            continue
        equivalent_complete = any(
            other.get("portfolio_ids")
            and other.get("route_shape") == challenger.get("route_shape")
            and set(other.get("origin_gateway_nodes", []))
            == set(challenger.get("origin_gateway_nodes", []))
            and set(other.get("destination_gateway_nodes", []))
            == set(challenger.get("destination_gateway_nodes", []))
            for other in archive.get("challengers", [])
            if other.get("challenger_id") != challenger_id
        )
        if equivalent_complete:
            continue
        disposition = dispositions.get(challenger_id, {})
        status = disposition.get("status") if isinstance(disposition, dict) else None
        if status == "dismissed":
            continue
        if status == "activated":
            hypothesis = state["hypothesis_tree"]["nodes"].get(
                challenger.get("hypothesis_id"), {}
            )
            if hypothesis.get("status") in TERMINAL_HYPOTHESIS_STATUSES:
                continue
        unresolved.append(str(challenger_id))
    return sorted(unresolved)


def dismiss_structural_challenger(
    raw_state: Any, proposal_id: str, reason: str
) -> dict[str, Any]:
    """Persist an explicit AI rejection; silent structural omission is forbidden."""

    state = validate_session(raw_state)
    proposal_id = _text(proposal_id, "proposal_id")
    reason = _text(reason, "reason")
    dispositions = state.get("structural_exploration", {}).get("dispositions", {})
    if proposal_id not in dispositions:
        raise RoundTripSessionError(f"unknown structural proposal {proposal_id}")
    if dispositions[proposal_id] != "pending":
        return _compact_session(state)
    dispositions[proposal_id] = "dismissed"
    proposal = next(
        item
        for item in state["structural_exploration"]["proposals"]
        if item["proposal_id"] == proposal_id
    )
    state["structural_exploration"].setdefault("disposition_history", []).append(
        {
            "proposal_id": proposal_id,
            "hypothesis_id": proposal["hypothesis_id"],
            "disposition": "dismissed",
            "reason": reason,
            "at": _now(),
        }
    )
    _invalidate_finalization(state)
    state["stop_assessment"] = None
    _refresh_portfolio_exploration(state)
    _commit_product_mutation(state)
    return state


def _supporting_portfolio_exists(state: dict[str, Any], hypothesis_id: str) -> bool:
    task_ids = {task["task_id"] for task in state["research_tasks"] if task["hypothesis_id"] == hypothesis_id}
    return any(task_ids & set(portfolio.get("evidence_task_ids", [])) for portfolio in state["result"]["portfolios"])


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
        if action == "complete":
            if node["hypothesis_kind"] == "gateway_discovery":
                side = "origin" if hypothesis_id == ORIGIN_GATEWAY_HYPOTHESIS else "destination"
                scope = state["region_scopes"][side]
                if scope.get("status") not in {"not_required", "defined", "legacy_complete"}:
                    raise RoundTripSessionError(
                        "gateway hypothesis complete requires an AI-defined region scope"
                    )
                unresolved = sorted(
                    node_id
                    for node_id, coverage in scope.get("coverage", {}).items()
                    if coverage == "pending"
                )
                if unresolved:
                    raise RoundTripSessionError(
                        f"gateway hypothesis has unresearched scoped candidates: {unresolved}"
                    )
                admitted = [
                    candidate
                    for candidate in state["gateway_candidates"][side]
                    if candidate.get("evidence_status")
                    in {"research_observed", "exact_anchor"}
                ]
                if not admitted:
                    raise RoundTripSessionError(
                        "gateway hypothesis complete requires an observed or exact candidate"
                    )
            elif node["hypothesis_kind"] == "structural_challenger_discovery":
                unfinished_required = [
                    task["task_id"]
                    for task in state["research_tasks"]
                    if task["hypothesis_id"] == hypothesis_id
                    and task.get("required", True)
                    and task.get("requirement_status") == "open"
                ]
                if unfinished_required:
                    raise RoundTripSessionError(
                        f"structural discovery has unfinished research {unfinished_required}"
                    )
                if state.get("structural_exploration", {}).get("status") not in {
                    "proposed",
                    "complete_no_proposal",
                    "legacy_complete",
                }:
                    raise RoundTripSessionError(
                        "structural discovery complete requires an AI proposal outcome"
                    )
            else:
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
                if not _supporting_portfolio_exists(state, hypothesis_id):
                    raise RoundTripSessionError("complete journey hypothesis requires a supporting complete portfolio")
        node["status"] = action
        node["terminal_reason"] = reason
        node["terminal_at_round"] = state["search_round"]
        for task in state["research_tasks"]:
            if task["hypothesis_id"] == hypothesis_id and task.get("requirement_status") == "open":
                _set_requirement_state(task, "superseded")
                task["status"] = "superseded"
                task["terminal_reason"] = reason
        state["active_hypothesis"] = None
    else:
        raise RoundTripSessionError("unsupported hypothesis action")
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)
    _invalidate_finalization(state)
    state["stop_assessment"] = None
    _refresh_portfolio_exploration(state)
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


def _publish_region_gateway_tasks(
    state: dict[str, Any], side: str, scope: dict[str, Any]
) -> None:
    hypothesis_id = f"hypothesis-gateway-{side}"
    existing = {str(task.get("task_id")) for task in state["research_tasks"]}
    active = state.get("active_hypothesis")
    for spec in AI_EXPLORATION.gateway_task_specs(scope, state["scenario"]):
        if spec["task_id"] in existing:
            continue
        task = _task(
            spec["task_id"],
            hypothesis_id,
            "gateway_discovery",
            f"{side}_regional_gateway_pool",
            spec["research_kind"],
            spec["question"],
            spec["query_shape"],
        )
        if isinstance(active, dict) and active.get("hypothesis_id") == hypothesis_id:
            task["status"] = "pending"
        state["research_tasks"].append(task)
        existing.add(task["task_id"])


def _publish_region_scope_challenge_task(
    state: dict[str, Any], side: str, scope: dict[str, Any]
) -> None:
    spec = AI_EXPLORATION.region_scope_challenge_task_spec(
        side, state["scenario"], scope
    )
    if any(task.get("task_id") == spec["task_id"] for task in state["research_tasks"]):
        return
    hypothesis_id = f"hypothesis-gateway-{side}"
    task = _task(
        spec["task_id"],
        hypothesis_id,
        "gateway_discovery",
        f"{side}_regional_gateway_pool",
        spec["research_kind"],
        spec["question"],
        spec["query_shape"],
    )
    active = state.get("active_hypothesis")
    if isinstance(active, dict) and active.get("hypothesis_id") == hypothesis_id:
        task["status"] = "pending"
    state["research_tasks"].append(task)


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
        if node_id in node_map and node_map[node_id] != node:
            raise RoundTripSessionError(f"conflicting AI region node {node_id}")
        if node_id not in node_map:
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
    state["gateway_candidates"][side] = _merge_unique(
        state["gateway_candidates"][side], ai_candidates, "candidate_id"
    )
    _publish_region_scope_challenge_task(state, side, normalized)


def _apply_region_scope_challenge(
    state: dict[str, Any], task: dict[str, Any], proposal: dict[str, Any]
) -> None:
    side = str(task["query_shape"]["side"])
    current = state["region_scopes"][side]
    try:
        challenge = AI_EXPLORATION.normalize_region_scope_challenge(
            proposal, state["scenario"], side, current
        )
        merged = AI_EXPLORATION.merge_region_scope_challenge(current, challenge)
    except AI_EXPLORATION.AIExplorationError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    state["region_scopes"][side] = merged
    node_map = {
        _node_id(node, "scenario.nodes[]"): node for node in state["scenario"]["nodes"]
    }
    additions = []
    for record in challenge["additional_candidates"]:
        node = record["node"]
        node_id = node["node_id"]
        if node_id in node_map and node_map[node_id] != node:
            raise RoundTripSessionError(f"conflicting challenged region node {node_id}")
        if node_id not in node_map:
            state["scenario"]["nodes"].append(copy.deepcopy(node))
            node_map[node_id] = node
        additions.append(
            {
                "candidate_id": record["candidate_id"],
                "side": side,
                "node": copy.deepcopy(node),
                "signal": {
                    "kind": "ai_region_scope_challenge",
                    "roles": list(record["roles"]),
                    "inclusion_reason": record["inclusion_reason"],
                },
                "evidence_status": "ai_proposed",
            }
        )
    state["gateway_candidates"][side] = _merge_unique(
        state["gateway_candidates"][side], additions, "candidate_id"
    )
    _publish_region_gateway_tasks(state, side, merged)


def _apply_structural_proposal(
    state: dict[str, Any], proposal: dict[str, Any]
) -> None:
    try:
        normalized = AI_EXPLORATION.normalize_structural_proposal(proposal, state)
    except AI_EXPLORATION.AIExplorationError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    previous = state.get("structural_exploration", {})
    normalized["proposal_history"] = [
        *copy.deepcopy(previous.get("proposal_history", [])),
        {
            "at": _now(),
            "proposal_ids": [item["proposal_id"] for item in normalized["proposals"]],
            "no_material_challenger_reason": normalized.get(
                "no_material_challenger_reason"
            ),
        },
    ]
    node_map = {
        _node_id(node, "scenario.nodes[]"): node for node in state["scenario"]["nodes"]
    }
    for item in normalized["proposals"]:
        for node in item.get("intermediate_nodes", []):
            node_id = node["node_id"]
            if node_id in node_map and node_map[node_id] != node:
                raise RoundTripSessionError(
                    f"conflicting structural intermediate node {node_id}"
                )
            if node_id not in node_map:
                state["scenario"]["nodes"].append(copy.deepcopy(node))
                node_map[node_id] = node
    state["structural_exploration"] = normalized


def _ensure_structural_exploration_task(state: dict[str, Any]) -> None:
    exploration = state.get("structural_exploration", {})
    if (
        state.get("result", {}).get("best_portfolio") is None
        or exploration.get("status") != "waiting_for_incumbent"
    ):
        return
    if any(
        task.get("hypothesis_id") == STRUCTURAL_DISCOVERY_HYPOTHESIS
        for task in state["research_tasks"]
    ):
        return
    exploration["minimum_proposals"] = AI_EXPLORATION.structural_minimum_proposals(
        state
    )
    spec = AI_EXPLORATION.structural_task_spec(state)
    task = _task(
        spec["task_id"],
        STRUCTURAL_DISCOVERY_HYPOTHESIS,
        "structural_challenger_discovery",
        "ai_structural_corridor_scope",
        spec["research_kind"],
        spec["question"],
        spec["query_shape"],
    )
    state["research_tasks"].append(task)
    state["hypothesis_tree"]["nodes"][STRUCTURAL_DISCOVERY_HYPOTHESIS][
        "status"
    ] = "candidate"
    exploration["status"] = "awaiting_ai"
    state["hypothesis_tree"]["next_candidate_id"] = _next_hypothesis(state)


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
        pool = solver_intent[f"{side}_pool"]
        if pool.get("endpoint_policy", "anchor_required") == "any_admitted_gateway":
            endpoint_ids = sorted(
                {
                    str(candidate.get("node", {}).get("node_id"))
                    for candidate in state.get("gateway_candidates", {}).get(side, [])
                    if candidate.get("node", {}).get("node_id")
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
    }
    try:
        state["result"] = SOLVER.solve_bundle(bundle)
    except SOLVER.RoundTripError as exc:
        raise RoundTripSessionError(str(exc)) from exc
    _ensure_structural_exploration_task(state)
    _refresh_portfolio_exploration(state)


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
    state["stop_assessment"] = None
    _commit_product_mutation(state)
    return True


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
    if active is None or active["hypothesis_id"] != task["hypothesis_id"]:
        raise RoundTripSessionError("evidence task must belong to the active hypothesis")
    status = raw_evidence.get("evidence_status")
    ai_proposal = status == "ai_proposed" and task["research_kind"] in {
        "region_scope_definition",
        "region_scope_challenge",
        "structural_challenger_definition",
    }
    temporal_research = status == "research_observed" and task["research_kind"] == "priced_offer" and task.get("journey_scope") in {"joint_round_trip", "one_way"}
    gateway_research = status == "research_observed" and task["research_kind"] == "gateway_discovery"
    payable = status == "verified_observed" and task["research_kind"] == "priced_offer"
    if status not in {"no_result", "rejected"} and not (ai_proposal or temporal_research or gateway_research or payable):
        if task["research_kind"] in {"region_scope_definition", "region_scope_challenge", "structural_challenger_definition"}:
            expected = "ai_proposed"
        elif task["research_kind"] == "gateway_discovery":
            expected = "research_observed"
        else:
            expected = "verified_observed or nonterminal temporal research_observed"
        raise RoundTripSessionError(f"task expects {expected} or scoped terminal evidence")
    ai_proposals = raw_evidence.get("ai_proposals", [])
    if ai_proposal:
        expected_type = (
            "region_scope"
            if task["research_kind"] == "region_scope_definition"
            else "region_scope_challenge"
            if task["research_kind"] == "region_scope_challenge"
            else "structural_challengers"
        )
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
    if gateway_research:
        expected_side = task.get("query_shape", {}).get("side")
        wrong_side = [
            candidate.get("candidate_id")
            for candidate in raw_evidence.get("gateway_candidates", [])
            if candidate.get("side") != expected_side
        ]
        if wrong_side:
            raise RoundTripSessionError(
                f"gateway evidence exceeds the active task side: {wrong_side}"
            )
        required_node_id = task.get("query_shape", {}).get(
            "required_candidate_node_id"
        )
        observed_node_ids = {
            candidate.get("node", {}).get("node_id")
            for candidate in raw_evidence.get("gateway_candidates", [])
        }
        if required_node_id and observed_node_ids != {required_node_id}:
            raise RoundTripSessionError(
                "gateway evidence must match the task's required candidate node"
            )
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
    temporal_signals = raw_evidence.get("temporal_signals", [])
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
    elif temporal_signals:
        raise RoundTripSessionError("temporal_signals are allowed only as nonterminal research evidence for a priced flight task")
    next_state = copy.deepcopy(state)
    next_task = _task_by_id(next_state, task["task_id"])
    next_task["attempt_count"] += 1
    next_task["evidence_bundle_ids"].append(bundle_id)
    coverage_complete = raw_evidence.get("coverage_complete", True)
    if coverage_complete:
        _set_requirement_state(
            next_task,
            "exhausted" if status in {"no_result", "rejected"} else "satisfied",
            evidence_disposition=status,
        )
        next_task["status"] = status
    else:
        _set_requirement_state(
            next_task, "open", evidence_disposition=status
        )
        next_task["status"] = "pending"
    next_state["evidence_bundles"].append(copy.deepcopy(raw_evidence))
    if status == "ai_proposed":
        proposal = ai_proposals[0]["proposal"]
        if task["research_kind"] == "region_scope_definition":
            _apply_region_scope_proposal(next_state, next_task, proposal)
        elif task["research_kind"] == "region_scope_challenge":
            _apply_region_scope_challenge(next_state, next_task, proposal)
        else:
            _apply_structural_proposal(next_state, proposal)
    elif status == "research_observed":
        if temporal_research:
            annotated_signals = [
                {**copy.deepcopy(signal), "research_task_id": task["task_id"], "hypothesis_id": task["hypothesis_id"]}
                for signal in temporal_signals
            ]
            next_state["temporal_signals"] = _merge_unique(next_state.get("temporal_signals", []), annotated_signals, "signal_id")
            next_temporal = next_task["temporal_state"]
            for signal in annotated_signals:
                if signal["signal_id"] not in next_temporal["signal_refs"]:
                    next_temporal["signal_refs"].append(signal["signal_id"])
            next_temporal["phase"] = "surface_discovery"
        else:
            for candidate in raw_evidence.get("gateway_candidates", []):
                side = candidate["side"]
                next_state["gateway_candidates"][side] = _merge_unique(next_state["gateway_candidates"][side], [{**candidate, "evidence_status": status}], "candidate_id")
                node_id = str(candidate["node"]["node_id"])
                if node_id in next_state.get("region_scopes", {}).get(side, {}).get("coverage", {}):
                    next_state["region_scopes"][side]["coverage"][node_id] = status
            incoming_nodes = raw_evidence.get("nodes", [])
            node_map = {_node_id(node, "scenario.nodes[]"): node for node in next_state["scenario"]["nodes"]}
            for node in incoming_nodes:
                node_id = _node_id(node, "evidence.nodes[]")
                if node_id in node_map and node_map[node_id] != node:
                    raise RoundTripSessionError(f"conflicting node {node_id}")
                if node_id not in node_map:
                    next_state["scenario"]["nodes"].append(copy.deepcopy(node))
                    node_map[node_id] = node
    elif status == "verified_observed":
        node_map = {_node_id(node, "scenario.nodes[]"): node for node in next_state["scenario"]["nodes"]}
        for node in raw_evidence.get("nodes", []):
            node_id = _node_id(node, "evidence.nodes[]")
            if node_id in node_map and node_map[node_id] != node:
                raise RoundTripSessionError(f"conflicting node {node_id}")
            if node_id not in node_map:
                next_state["scenario"]["nodes"].append(copy.deepcopy(node))
                node_map[node_id] = node
        movement_node_ids = {node for movement in raw_evidence.get("movements", []) for node in (movement.get("from_node"), movement.get("to_node"))}
        if not movement_node_ids <= set(node_map):
            raise RoundTripSessionError("every movement endpoint must have a RegionNode in scenario or evidence")
        next_state["movements"] = _merge_unique(next_state["movements"], raw_evidence.get("movements", []), "movement_id")
        annotated = []
        movement_map = {movement["movement_id"]: movement for movement in next_state["movements"]}
        for offer in raw_evidence.get("offers", []):
            value = {**offer, "research_task_id": task["task_id"], "hypothesis_id": task["hypothesis_id"]}
            geometry = [
                {key: movement_map[movement_id].get(key) for key in ("from_node", "to_node", "departure", "arrival", "mode", "operator", "service_id")}
                for movement_id in offer["movement_ids"]
            ]
            value["itinerary_id"] = offer.get("itinerary_id") or "itinerary-" + hashlib.sha256(json.dumps(geometry, sort_keys=True).encode("utf-8")).hexdigest()[:16]
            snapshot_basis = {key: value.get(key) for key in ("offer_id", "provider_offer_id", "source_id", "seller", "observed_at", "price", "mandatory_baggage_cost", "required_accommodation_cost", "expires_at")}
            value["offer_snapshot_id"] = offer.get("offer_snapshot_id") or "snapshot-" + hashlib.sha256(json.dumps(snapshot_basis, sort_keys=True).encode("utf-8")).hexdigest()[:16]
            if offer.get("journey_scope") == "one_way":
                value["coverage_start_nodes"] = list(task["query_shape"]["origins"])
                value["coverage_end_nodes"] = list(task["query_shape"]["destinations"])
            if offer.get("journey_scope") == "surface":
                value["surface_role"] = task["query_shape"]["surface_role"]
                value["pool_constraints"] = copy.deepcopy(task["query_shape"]["pool_constraints"])
            annotated.append(value)
        next_state["offers"] = _merge_unique(next_state["offers"], annotated, "offer_id")
        _publish_exact_surface_tasks(next_state, task["hypothesis_id"])
        if next_task.get("temporal_state", {}).get("phase") != "not_applicable" and coverage_complete:
            next_task["temporal_state"]["phase"] = "terminal"
    if status in {"no_result", "rejected"} and task["research_kind"] == "gateway_discovery":
        side = str(task.get("query_shape", {}).get("side"))
        node_id = task.get("query_shape", {}).get("required_candidate_node_id")
        if node_id in next_state.get("region_scopes", {}).get(side, {}).get("coverage", {}):
            next_state["region_scopes"][side]["coverage"][str(node_id)] = status
    if status in {"no_result", "rejected"} and coverage_complete and next_task.get("temporal_state", {}).get("phase") != "not_applicable":
        next_task["temporal_state"]["phase"] = "terminal"
    next_state["search_round"] += 1
    _recompute(next_state)
    _invalidate_finalization(next_state)
    next_state["stop_assessment"] = None
    _commit_product_mutation(next_state)
    return next_state


def record_stop_assessment(raw_state: Any, reason: str, challenger_hypothesis_ids: list[str] | None = None) -> dict[str, Any]:
    state = validate_session(raw_state)
    if state["result"].get("best_portfolio") is None:
        raise RoundTripSessionError("stop assessment requires a complete incumbent")
    structural_node = state["hypothesis_tree"]["nodes"].get(
        STRUCTURAL_DISCOVERY_HYPOTHESIS, {}
    )
    if structural_node.get("status") not in TERMINAL_HYPOTHESIS_STATUSES:
        raise RoundTripSessionError(
            "stop assessment requires terminal structural challenger discovery"
        )
    unresolved_structural = AI_EXPLORATION.unresolved_structural_proposal_ids(state)
    if unresolved_structural:
        raise RoundTripSessionError(
            f"stop assessment has unresolved structural proposals {unresolved_structural}"
        )
    unresolved_portfolio = unresolved_recommended_challenger_ids(state)
    if unresolved_portfolio:
        raise RoundTripSessionError(
            f"stop assessment has unresolved recommended portfolio challengers {unresolved_portfolio}"
        )
    challengers = challenger_hypothesis_ids or []
    nodes = state["hypothesis_tree"]["nodes"]
    unknown = [item for item in challengers if item not in nodes]
    if unknown:
        raise RoundTripSessionError(f"unknown challenger hypotheses {unknown}")
    unfinished = [item for item in challengers if nodes[item]["status"] not in TERMINAL_HYPOTHESIS_STATUSES]
    if unfinished:
        raise RoundTripSessionError(f"challenger hypotheses are unfinished {unfinished}")
    revision = _commit_product_mutation(state)
    state["stop_assessment"] = {
        "reason": _text(reason, "reason"),
        "challenger_hypothesis_ids": challengers,
        "assessed_revision": revision,
        "search_round": state["search_round"],
        "at": _now(),
    }
    return state


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
    state["stop_assessment"] = None
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
    _invalidate_finalization(state)
    state["stop_assessment"] = None
    _refresh_portfolio_exploration(state)
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
    state["stop_assessment"] = None
    _commit_product_mutation(state)
    return state


def _finalization_audit(state: dict[str, Any]) -> dict[str, Any]:
    if "active_hypothesis" not in state:
        state = validate_session(state)
    blockers = []
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
            if status == "pending"
        )
        if unresolved:
            blockers.append(f"{side} region scope has unresearched gateways: {unresolved}")
    structural_node = nodes.get(STRUCTURAL_DISCOVERY_HYPOTHESIS, {})
    if state["result"].get("best_portfolio") is not None and structural_node.get(
        "status"
    ) not in TERMINAL_HYPOTHESIS_STATUSES:
        blockers.append("structural challenger discovery lacks terminal disposition")
    unresolved_structural = AI_EXPLORATION.unresolved_structural_proposal_ids(state)
    if unresolved_structural:
        blockers.append(
            f"structural proposals lack activation or dismissal: {unresolved_structural}"
        )
    unresolved_portfolio = unresolved_recommended_challenger_ids(state)
    if unresolved_portfolio:
        blockers.append(
            f"recommended portfolio challengers lack activation or dismissal: {unresolved_portfolio}"
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
    pending = [
        task["task_id"]
        for task in state["research_tasks"]
        if task.get("requirement_status") == "open"
        and task.get("hypothesis_id") == state.get("active_hypothesis_id")
    ]
    if pending:
        blockers.append(f"unexplained pending tasks: {pending}")
    assessment = state.get("stop_assessment")
    if (
        assessment is None
        or assessment.get("assessed_revision") != state["product_revision"]
    ):
        blockers.append("fresh challenger stop assessment missing")
    return {"can_finalize": not blockers, "blockers": blockers}


def export_result(raw_state: Any, *, search_status: str, reason: str | None = None) -> dict[str, Any]:
    state = validate_session(raw_state)
    audit = _finalization_audit(state)
    if search_status == "final" and not audit["can_finalize"]:
        raise RoundTripSessionError(f"cannot finalize: {audit['blockers']}")
    return {
        "result_version": SESSION_VERSION,
        "session_id": state["session_id"],
        "trip_scope": TRIP_SCOPE,
        "search_status": search_status,
        "incomplete": search_status != "final",
        "reason": reason,
        "best_portfolio": copy.deepcopy(state["result"].get("best_portfolio")),
        "portfolios": copy.deepcopy(state["result"].get("portfolios", [])),
        "portfolio_kind_counts": copy.deepcopy(state["result"].get("portfolio_kind_counts", {})),
        "gateway_candidates": copy.deepcopy(state["gateway_candidates"]),
        "region_scopes": copy.deepcopy(state.get("region_scopes", {})),
        "structural_exploration": copy.deepcopy(
            state.get("structural_exploration", {})
        ),
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
        "portfolio_exploration": {
            "archive": copy.deepcopy(
                state.get("portfolio_exploration_archive", {})
            ),
            "research_view": copy.deepcopy(
                state.get("portfolio_exploration_view", {})
            ),
            "selection_history": [
                copy.deepcopy(value)
                for key, value in sorted(
                    state.get("challenger_dispositions", {}).items()
                )
                if key.startswith("portfolio:")
            ],
            "dispositions": copy.deepcopy(
                state.get("portfolio_challenger_dispositions", {})
            ),
        },
        "hypothesis_outcomes": _derive_hypothesis_outcomes(state),
        "stop_assessment": copy.deepcopy(state.get("stop_assessment")),
        "finalization_audit": audit,
        "unsearched_scope": "Any route, Provider, gateway, date-pair cell, or atomic offer portfolio not represented by completed evidence remains unsearched. Temporal advice does not imply date-pair exhaustion.",
    }


def finalize_session(raw_state: Any, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = validate_session(raw_state)
    _refresh_time_sensitive_state(state)
    state["finalization_status"] = "final"
    assessment = state.get("stop_assessment")
    if isinstance(assessment, dict):
        assessment["assessed_revision"] = int(state.get("product_revision", 0)) + 1
    _commit_product_mutation(state)
    result = export_result(state, search_status="final", reason=_text(reason, "reason"))
    return state, result


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
        "structural_exploration": {
            "status": state.get("structural_exploration", {}).get("status"),
            "proposal_count": len(
                state.get("structural_exploration", {}).get("proposals", [])
            ),
            "unresolved_proposal_ids": AI_EXPLORATION.unresolved_structural_proposal_ids(
                state
            ),
        },
        "best_portfolio": state["result"].get("best_portfolio"),
        "portfolio_exploration": {
            "challenger_count": state.get("portfolio_exploration_archive", {}).get(
                "challenger_count", 0
            ),
            "quality_cell_count": state.get(
                "portfolio_exploration_archive", {}
            ).get("quality_cell_count", 0),
            "actionable_count": len(
                state.get("portfolio_exploration_view", {}).get(
                    "challenger_actions", []
                )
            ),
        },
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
    activate = sub.add_parser("activate-challenger")
    activate.add_argument("--state", required=True)
    activate.add_argument("--challenger-id", required=True)
    activate.add_argument("--reason", required=True)
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
    stop = sub.add_parser("stop-assessment")
    stop.add_argument("--state", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--challenger-hypothesis-id", action="append", default=[])
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
        elif args.command == "activate-challenger":
            state = activate_portfolio_challenger(
                read_json(args.state), args.challenger_id, args.reason
            )
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
        elif args.command == "stop-assessment":
            state = record_stop_assessment(read_json(args.state), args.reason, args.challenger_hypothesis_id)
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
