from __future__ import annotations

"""Render AI-actionable task options as deterministic browser briefs.

This module never opens, controls, or reads a browser.  It translates the
candidate and recommended tasks produced by ``live_provider_session.py`` into a compact
collection contract for an AI agent that operates the browser externally.
"""

import argparse
import copy
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


BRIEF_VERSION = "0.7.1"


def _load_provider_query_shapes() -> Any:
    module_path = Path(__file__).resolve().with_name("provider_query_shapes.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_one_way_provider_query_shapes", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Provider query shapes: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROVIDER_QUERY_SHAPES = _load_provider_query_shapes()


class BrowserTaskBriefError(ValueError):
    pass


TASK_FAMILIES = {
    "baseline": "baseline",
    "conventional_reference": "baseline",
    "market": "market",
    "origin_market_scan": "market",
    "regional_market_scan": "market",
    "origin_access_market_scan": "market",
    "corridor_calendar_scan": "corridor_calendar",
    "corridor_connector_research": "bridge",
    "reverse": "reverse",
    "destination_market_scan": "reverse",
    "budgeted_predecessor": "budgeted_predecessor",
    "budgeted_predecessor_scan": "budgeted_predecessor",
    "direct_closure": "budgeted_predecessor",
    "bridge": "bridge",
    "unresolved_connector": "bridge",
    "predecessor_connector_scan": "bridge",
    "refresh": "refresh",
    "refresh_quote": "refresh",
}


PURPOSES = {
    "baseline": (
        "Establish or refresh the conventional end-to-end price baseline "
        "inside the issued origin and destination pools."
    ),
    "market": (
        "Discover low-cost outward anchors from the issued origin scope; "
        "do not assume a destination before observing an offer."
    ),
    "corridor_calendar": (
        "Search this corridor's own date-price surface inside the issued window. "
        "Do not reuse the cheapest date observed for a different corridor."
    ),
    "reverse": (
        "Search backward from the destination scope for low-cost arrival "
        "anchors that can become a confirmed suffix."
    ),
    "budgeted_predecessor": (
        "Find an observed predecessor that reaches the fixed suffix anchor "
        "within the residual price-improvement allowance."
    ),
    "bridge": (
        "Research the bounded gap between the issued endpoint scopes using only "
        "observed transport offers or schedules."
    ),
    "refresh": (
        "Re-check the targeted observed offer and return a current quote or "
        "a scoped no-result observation."
    ),
}


SOURCE_CATEGORIES = {
    "flight": [
        "flight_metasearch_or_fare_calendar",
        "airline_or_authorized_ticket_seller",
    ],
    "rail": ["rail_operator_or_official_timetable"],
    "bus": ["coach_operator_or_ticketing_platform"],
    "coach": ["coach_operator_or_ticketing_platform"],
    "ferry": ["ferry_operator_or_official_schedule"],
    "local_transit": ["official_local_transit_journey_planner"],
    "walk": ["map_or_official_wayfinding_source"],
    "rideshare": ["named_rideshare_provider"],
    "car": ["named_ground_transport_provider"],
}


CAPTURE_FIELDS = [
    {
        "field": "price_selection_method",
        "required_when": "fare-bearing flight observation",
        "rule": (
            "Record lowest_price_view, price_ascending, price_sorted_api, or "
            "all_visible_compared. A default recommended view is not "
            "lowest-price evidence."
        ),
    },
    {
        "field": "operator",
        "required_when": "non_flight",
        "rule": "Name the rail, coach, ferry, or other non-flight transport operator.",
    },
    {
        "field": "flight_identity.marketing_carrier",
        "required_when": "flight_when_available",
        "rule": "Capture the marketing carrier exactly as displayed; do not infer it.",
    },
    {
        "field": "flight_identity.operating_carrier",
        "required_when": "flight_when_available",
        "rule": "Capture the operating carrier exactly as displayed; do not infer it.",
    },
    {
        "field": "flight_identity.flight_numbers",
        "required_when": "flight_numbers_disclosed",
        "rule": "Capture all displayed flight numbers in segment order; never infer one.",
    },
    {
        "field": "flight_identity.unknown_reason",
        "required_when": "flight_numbers_not_available",
        "rule": "Use a permitted structured reason instead of guessing a flight number.",
    },
    {
        "field": "departure_and_arrival",
        "required_when": "always",
        "rule": "Capture exact local dates, times, airports or terminals, and UTC offsets when available.",
    },
    {
        "field": "price_amount_and_currency",
        "required_when": "always",
        "rule": "Capture the mandatory payable price shown for the whole observed offer; do not invent segment prices.",
    },
    {
        "field": "sales_platform",
        "required_when": "always",
        "rule": "Name the website or seller displaying the price, separately from the transport operator.",
    },
    {
        "field": "provider_offer_id",
        "required_when": "when_disclosed",
        "rule": "Capture the seller's offer identifier when visible; absence does not invalidate the observation.",
    },
    {
        "field": "itinerary.planned_exit_node",
        "required_when": "traveler_plans_to_end_at_a_ticketed_connection",
        "rule": "Capture the full ticketed segment sequence plus the planned exit connection; keep the whole offer price atomic and leave later ticketed components unused.",
    },
    {
        "field": "discovery_provenance",
        "required_when": "when_a_research_source_led_to_a_separately_verified_seller_quote",
        "rule": "Preserve the research provider, URL, observation time, purpose, and optional displayed price as research_only; never use it instead of the seller price.",
    },
    {
        "field": "source_url",
        "required_when": "always",
        "rule": "Capture the absolute HTTP(S) result or booking URL that supports the observation.",
    },
    {
        "field": "observed_at",
        "required_when": "always",
        "rule": "Record a timezone-aware ISO date-time for the browser observation.",
    },
    {
        "field": "baggage",
        "required_when": "always",
        "rule": "Record personal-item status and every mandatory baggage charge; unknown is not zero.",
    },
]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BrowserTaskBriefError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrowserTaskBriefError(f"{field} must be a non-empty string")
    return value.strip()


def _time(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrowserTaskBriefError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise BrowserTaskBriefError(f"{field} must include a UTC offset")
    return raw


def _number_or_none(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BrowserTaskBriefError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BrowserTaskBriefError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise BrowserTaskBriefError(f"{field} must be finite and non-negative")
    return result


def _family(task: dict[str, Any]) -> str:
    request_type = _text(task.get("request_type"), "task.request_type")
    try:
        return TASK_FAMILIES[request_type]
    except KeyError as exc:
        raise BrowserTaskBriefError(
            f"task.request_type {request_type!r} has no browser-brief family"
        ) from exc


def _node_ids(task: dict[str, Any], side: str) -> list[str]:
    plural = "origins" if side == "from" else "destinations"
    singular = "from_node" if side == "from" else "to_node"
    raw = task.get(plural)
    if raw is None:
        raw = [task.get(singular)] if task.get(singular) else []
    if not isinstance(raw, list):
        raise BrowserTaskBriefError(f"task.{plural} must be an array")
    values: list[str] = []
    for index, item in enumerate(raw):
        node_id = _text(item, f"task.{plural}[{index}]").upper()
        if node_id not in values:
            values.append(node_id)
    return values


def _region(profile: dict[str, Any], side: str) -> dict[str, str] | None:
    key = "origin_region" if side == "from" else "destination_region"
    raw = profile.get(key)
    if not isinstance(raw, dict):
        return None
    region_id = raw.get("region_id")
    label = raw.get("label")
    if not isinstance(region_id, str) or not region_id.strip():
        return None
    if not isinstance(label, str) or not label.strip():
        return None
    return {"region_id": region_id.strip(), "label": label.strip()}


def _known_nodes(
    node_ids: list[str], node_map: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for node_id in node_ids:
        if node_id == "ANYWHERE" or node_id not in node_map:
            continue
        node = node_map[node_id]
        item: dict[str, Any] = {"node_id": node_id}
        for field in ("node_type", "location", "country_or_region", "country_code"):
            value = node.get(field)
            if isinstance(value, str) and value.strip():
                item[field] = value.strip()
        values.append(item)
    return values


def _endpoint_scope(
    task: dict[str, Any],
    family: str,
    side: str,
    profile: dict[str, Any],
    node_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    policy = _object(task.get("endpoint_policy"), "task.endpoint_policy")
    policy_value = _text(policy.get(side), f"task.endpoint_policy.{side}")
    if policy_value not in {
        "exact_node",
        "node_pool",
        "provider_authorized_anywhere",
    }:
        raise BrowserTaskBriefError(
            f"task.endpoint_policy.{side} is unsupported: {policy_value}"
        )
    node_ids = _node_ids(task, side)
    region: dict[str, str] | None = None
    if family == "baseline":
        region = _region(profile, side)
    elif family == "market" and side == "from":
        region = _region(profile, side)
    elif family == "reverse" and side == "to":
        region = _region(profile, side)
    return {
        "policy": policy_value,
        "node_ids": node_ids,
        "known_nodes": _known_nodes(node_ids, node_map),
        "region": region,
        "allows_sourced_node_discovery": policy_value
        == "provider_authorized_anywhere",
    }


def _source_categories(modes: list[str], family: str) -> list[str]:
    values: list[str] = []
    if family == "refresh":
        values.append("original_offer_source")
    for mode in modes:
        for category in SOURCE_CATEGORIES.get(mode, [f"named_{mode}_provider"]):
            if category not in values:
                values.append(category)
    return values


def _money_context(
    task: dict[str, Any], state: dict[str, Any], family: str
) -> dict[str, Any]:
    budget = task.get("budget_context", {})
    if not isinstance(budget, dict):
        raise BrowserTaskBriefError("task.budget_context must be an object")
    config = state.get("config", {})
    profile = state.get("profile", {})
    currency = None
    if isinstance(config, dict):
        currency = config.get("base_currency")
    if currency is None and isinstance(profile, dict):
        currency = profile.get("currency")
    if currency is None:
        incumbent = state.get("active_incumbent", {})
        if isinstance(incumbent, dict):
            currency = incumbent.get("currency")
    currency = str(currency).upper() if currency else None
    remaining = _number_or_none(
        budget.get("remaining_budget"), "task.budget_context.remaining_budget"
    )
    active = remaining is not None
    return {
        "available": active,
        "remaining_amount": remaining,
        "currency": currency,
        "incumbent_total_cost": _number_or_none(
            budget.get("incumbent_total_cost"),
            "task.budget_context.incumbent_total_cost",
        ),
        "known_prefix_cost": _number_or_none(
            budget.get("known_prefix_cost"),
            "task.budget_context.known_prefix_cost",
        ),
        "known_suffix_cost": _number_or_none(
            budget.get("known_suffix_cost"),
            "task.budget_context.known_suffix_cost",
        ),
        "ceiling_source": budget.get("ceiling_source"),
        "application": (
            "reference_context_only"
            if family == "baseline"
            else "candidate_mandatory_cost_must_not_exceed_remaining_amount_to_improve_price"
            if active
            else "no_price_improvement_ceiling_was_issued"
        ),
    }


def _modes(task: dict[str, Any]) -> list[str]:
    raw = task.get("suggested_modes")
    if not isinstance(raw, list) or not raw:
        raise BrowserTaskBriefError("task.suggested_modes must be a non-empty array")
    values: list[str] = []
    for index, item in enumerate(raw):
        mode = _text(item, f"task.suggested_modes[{index}]").lower()
        if mode not in values:
            values.append(mode)
    return values


def _no_result_contract() -> dict[str, Any]:
    return {
        "evidence_status": "no_result",
        "meaning": (
            "No matching result was observed from the named provider for this "
            "task's exact endpoint, mode, and time scope at the observation time."
        ),
        "does_not_mean": (
            "This does not prove that no route exists in the wider or global market."
        ),
        "required_fields": ["provider", "source_url", "observed_at", "reason"],
        "reason_rule": (
            "Describe the bounded query attempted and the provider-visible outcome; "
            "do not turn an unsearched provider or mode into no_result."
        ),
    }


def _assigned_source(task: dict[str, Any]) -> dict[str, Any] | None:
    raw = task.get("assigned_source")
    if raw is None:
        return None
    source = _object(raw, "task.assigned_source")
    admission = _text(
        source.get("admission_policy"), "task.assigned_source.admission_policy"
    )
    research_first = "research_then" in admission or admission in {
        "topology_only",
        "topology_or_schedule_then_operator",
        "schedule_then_operator",
        "route_then_operator",
    }
    return {
        "source_id": _text(source.get("source_id"), "task.assigned_source.source_id"),
        "name": _text(source.get("name"), "task.assigned_source.name"),
        "url": _text(source.get("url"), "task.assigned_source.url"),
        "source_type": _text(
            source.get("source_type"), "task.assigned_source.source_type"
        ),
        "execution_kind": _text(
            source.get("execution_kind"), "task.assigned_source.execution_kind"
        ),
        "admission_policy": admission,
        "fulfillment_rule": (
            "This is one source-specific option. If selected, preserve it as research_only discovery_provenance, "
            "then verify the exact payable offer or schedule with the named seller or operator."
            if research_first
            else "This is one source-specific option. If selected, capture its exact offer, or preserve its handoff "
            "as discovery_provenance before capturing the named seller's payable quote."
        ),
    }


def _ordered_tree(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.get("hypothesis_tree")
    required = {
        "tree_version",
        "root_hypothesis_id",
        "nodes",
        "active_path",
        "next_candidate_id",
        "backtrack_count",
        "backtrack_history",
        "ordering_rule",
    }
    if isinstance(tree, dict) and required.issubset(tree):
        return copy.deepcopy(tree)
    root_id = "one-way-search-root"
    return {
        "tree_version": "1.0",
        "root_hypothesis_id": root_id,
        "nodes": {
            root_id: {
                "hypothesis_id": root_id,
                "parent_hypothesis_id": None,
                "child_hypothesis_ids": [],
                "sibling_order": 0,
                "status": "root",
                "task_ids": [],
                "actionable_task_ids": [],
                "pending_task_ids": [],
                "next_task_id": None,
            }
        },
        "active_path": [root_id],
        "next_candidate_id": None,
        "backtrack_count": 0,
        "backtrack_history": [],
        "ordering_rule": "legacy session has no persisted hypothesis order",
    }


def _hypothesis_node(task: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    hypothesis_id = task.get("hypothesis_id")
    tree = _ordered_tree(state)
    nodes = tree.get("nodes", {})
    node = nodes.get(str(hypothesis_id), {}) if isinstance(nodes, dict) else {}
    return node if isinstance(node, dict) else {}


def _task_is_open(task: dict[str, Any], state: dict[str, Any]) -> bool:
    if task.get("status") not in {"candidate", "pending"}:
        return False
    outcomes = state.get("hypothesis_outcomes", {})
    if not isinstance(outcomes, dict) or not task.get("hypothesis_id"):
        return True
    outcome = outcomes.get(str(task.get("hypothesis_id")))
    if not isinstance(outcome, dict) or outcome.get("status") not in {
        "complete",
        "hard_dead",
        "deferred_low_value",
    }:
        return True
    covered = outcome.get("covered_task_ids")
    if not isinstance(covered, list):
        return False
    return str(task.get("task_id")) not in {str(task_id) for task_id in covered}


def _script_recommended(task: dict[str, Any]) -> bool:
    advice = task.get("selection_advice")
    return bool(isinstance(advice, dict) and advice.get("script_recommended") is True)


def task_to_brief(
    task: dict[str, Any], state: dict[str, Any], sequence: int
) -> dict[str, Any]:
    task_status = str(task.get("status", ""))
    if task_status not in {"pending", "candidate"}:
        raise BrowserTaskBriefError(
            "task_to_brief accepts AI-actionable candidate or pending tasks only"
        )
    if not _task_is_open(task, state):
        raise BrowserTaskBriefError(
            "task belongs to a disposed hypothesis; reopen the hypothesis before execution"
        )
    family = _family(task)
    profile = state.get("profile", {})
    if not isinstance(profile, dict):
        raise BrowserTaskBriefError("state.profile must be an object")
    raw_nodes = state.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise BrowserTaskBriefError("state.nodes must be an array")
    node_map: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(raw_nodes):
        node = _object(node, f"state.nodes[{index}]")
        node_id = _text(node.get("node_id"), f"state.nodes[{index}].node_id").upper()
        node_map[node_id] = node
    modes = _modes(task)
    earliest = _time(task.get("earliest_departure"), "task.earliest_departure")
    latest = _time(task.get("latest_arrival"), "task.latest_arrival")
    if datetime.fromisoformat(latest.replace("Z", "+00:00")) <= datetime.fromisoformat(
        earliest.replace("Z", "+00:00")
    ):
        raise BrowserTaskBriefError("task time window must be increasing")
    raw_date_search = task.get("date_search", {"mode": "bounded_window"})
    if not isinstance(raw_date_search, dict):
        raise BrowserTaskBriefError("task.date_search must be an object")
    date_mode = _text(raw_date_search.get("mode", "bounded_window"), "task.date_search.mode")
    if date_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise BrowserTaskBriefError("task.date_search.mode is unsupported")
    date_search = dict(raw_date_search)
    date_search["mode"] = date_mode
    date_search["execution_rule"] = (
        "Use one provider calendar/grid query for this corridor when available, retain "
        "the issued number of distinct low-price/Pareto dates, then open exact dated "
        "results for evidence. Calendar or month-view prices are discovery hints only."
        if date_mode in {"bounded_window", "fully_flexible"}
        else "Search every issued exact departure date; do not substitute an unissued date."
    )
    corridor_context = task.get("corridor_context")
    if corridor_context is not None and not isinstance(corridor_context, dict):
        raise BrowserTaskBriefError("task.corridor_context must be an object")
    assigned_source = _assigned_source(task)
    tree_node = _hypothesis_node(task, state)
    try:
        query_scope_ladder = PROVIDER_QUERY_SHAPES.compile_scope_ladder(task, state)
    except PROVIDER_QUERY_SHAPES.ProviderQueryShapeError as exc:
        raise BrowserTaskBriefError(f"Provider query-scope compilation failed: {exc}") from exc
    return {
        "brief_version": BRIEF_VERSION,
        "task_id": _text(task.get("task_id"), "task.task_id"),
        "task_sequence": sequence,
        "task_status": task_status,
        "hypothesis": {
            "hypothesis_id": task.get("hypothesis_id"),
            "hypothesis_kind": task.get("hypothesis_kind"),
            "parent_hypothesis_id": tree_node.get(
                "parent_hypothesis_id", task.get("parent_hypothesis_id")
            ),
            "sibling_order": tree_node.get("sibling_order"),
            "tree_status": tree_node.get("status"),
            "next_task_id": tree_node.get("next_task_id"),
            "route_shape": task.get("route_shape"),
            "geographic_cell": task.get("geographic_cell"),
            "anchor_edge_ids": copy.deepcopy(task.get("anchor_edge_ids", [])),
        },
        "decision_context": {
            "script_recommended": _script_recommended(task),
            "selection_advice": copy.deepcopy(task.get("selection_advice")),
            "ai_authority": (
                "The AI may execute this option or any other candidate/pending option. "
                "Script recommendation, budget, and quota fields are advisory."
            ),
        },
        "request_type": _text(task.get("request_type"), "task.request_type"),
        "task_family": family,
        "task_purpose": PURPOSES[family],
        "search_scope": {
            "from": _endpoint_scope(task, family, "from", profile, node_map),
            "to": _endpoint_scope(task, family, "to", profile, node_map),
        },
        "query_scope_ladder": query_scope_ladder,
        "frontier_context": copy.deepcopy(task.get("frontier_context")),
        "time_window": {
            "earliest_departure": earliest,
            "latest_arrival": latest,
            "rule": "Search only inside this bounded window and preserve source-displayed local times and offsets.",
        },
        "date_search": date_search,
        "corridor_context": dict(corridor_context) if corridor_context else None,
        "candidate_modes": modes,
        "traveler_requirements": {
            "traveler_count": profile.get("traveler_count"),
            "baggage": copy.deepcopy(profile.get("baggage")),
            "overnight_allowed": profile.get("overnight_allowed"),
            "self_transfer_policy": profile.get("self_transfer_policy"),
        },
        "price_improvement": _money_context(task, state, family),
        "assigned_source": assigned_source,
        "source_suggestions": copy.deepcopy(task.get("source_suggestions", [])),
        "suggested_source_categories": _source_categories(modes, family),
        "provider_kinds": list(task.get("provider_kinds", [])),
        "must_capture": [dict(item) for item in CAPTURE_FIELDS],
        "conditional_requirements": [
            {
                "when": "candidate mode is flight",
                "at_least_one": [
                    "flight_identity.marketing_carrier",
                    "flight_identity.operating_carrier",
                ],
                "rule": "Every flight observation must identify at least one carrier.",
            },
            {
                "when": "candidate mode is flight",
                "exactly_one": [
                    "flight_identity.flight_numbers",
                    "flight_identity.unknown_reason",
                ],
                "unknown_reason_codes": [
                    "not_disclosed_by_source",
                    "not_visible_in_search_result",
                    "provider_omitted",
                ],
                "rule": "Return all displayed flight numbers or one structured unknown reason, never both.",
            }
        ],
        "offer_collection_policy": {
            "multi_platform": (
                "Preserve separately observed sales-platform offers for the same "
                "itinerary; never overwrite one seller's quote with another."
            ),
            "provider_offer_id": "Optional unless the inspected source displays it.",
            "research_to_seller": (
                "If Matrix, a calendar, or another research source led to the "
                "candidate, preserve it as research_only discovery_provenance and "
                "admit only a separately verified airline or named-seller quote."
            ),
            "assigned_source": (
                assigned_source["fulfillment_rule"]
                if assigned_source
                else "Legacy generic task; choose a named applicable source and preserve it."
            ),
        },
        "no_result": _no_result_contract(),
        "browser_boundary": (
            "The AI agent operates and interprets the browser. This brief renderer "
            "does not navigate, query, click, scrape, or authenticate."
        ),
    }


def build_briefs(state: dict[str, Any]) -> dict[str, Any]:
    state = _object(state, "state")
    tasks = state.get("research_tasks")
    if not isinstance(tasks, list):
        raise BrowserTaskBriefError("state.research_tasks must be an array")
    actionable = [
        task
        for task in tasks
        if isinstance(task, dict) and _task_is_open(task, state)
    ]
    actionable.sort(
        key=lambda item: (
            0 if item.get("status") == "pending" else 1,
            int(item.get("sequence", 0)),
            str(item.get("task_id", "")),
        )
    )
    briefs = [
        task_to_brief(task, state, index) for index, task in enumerate(actionable, 1)
    ]
    pending_count = sum(item.get("status") == "pending" for item in actionable)
    candidate_count = sum(item.get("status") == "candidate" for item in actionable)
    return {
        "brief_set_version": BRIEF_VERSION,
        "session_id": state.get("session_id"),
        "trip_scope": state.get("trip_scope"),
        "actionable_task_count": len(briefs),
        "pending_task_count": pending_count,
        "candidate_task_count": candidate_count,
        "ordered_hypothesis_tree": _ordered_tree(state),
        "briefs": briefs,
    }


def build_catalog(state: dict[str, Any]) -> dict[str, Any]:
    state = _object(state, "state")
    tasks = state.get("research_tasks")
    if not isinstance(tasks, list):
        raise BrowserTaskBriefError("state.research_tasks must be an array")
    actionable = [
        task
        for task in tasks
        if isinstance(task, dict) and _task_is_open(task, state)
    ]
    groups: dict[str, dict[str, Any]] = {}
    for task in actionable:
        group_id = str(
            task.get("parent_request_key")
            or task.get("task_signature")
            or task.get("task_id")
        )
        group = groups.setdefault(
            group_id,
            {
                "question_id": group_id,
                "request_type": task.get("request_type"),
                "hypothesis_id": task.get("hypothesis_id"),
                "hypothesis_kind": task.get("hypothesis_kind"),
                "parent_hypothesis_id": _hypothesis_node(task, state).get(
                    "parent_hypothesis_id", task.get("parent_hypothesis_id")
                ),
                "sibling_order": _hypothesis_node(task, state).get("sibling_order"),
                "tree_status": _hypothesis_node(task, state).get("status"),
                "next_task_id": _hypothesis_node(task, state).get("next_task_id"),
                "route_shape": task.get("route_shape"),
                "geographic_cell": task.get("geographic_cell"),
                "origins": list(task.get("origins", [])),
                "destinations": list(task.get("destinations", [])),
                "earliest_departure": task.get("earliest_departure"),
                "latest_arrival": task.get("latest_arrival"),
                "suggested_modes": list(task.get("suggested_modes", [])),
                "options": [],
            },
        )
        assigned = task.get("assigned_source")
        group["options"].append(
            {
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                "script_recommended": _script_recommended(task),
                "source_id": (
                    assigned.get("source_id")
                    if isinstance(assigned, dict)
                    else "ai-choice"
                ),
                "source_name": (
                    assigned.get("name")
                    if isinstance(assigned, dict)
                    else "AI-selected suitable source"
                ),
                "selection_advice": copy.deepcopy(task.get("selection_advice")),
                "source_suggestion_ids": [
                    str(source.get("source_id"))
                    for source in task.get("source_suggestions", [])
                    if isinstance(source, dict) and source.get("source_id")
                ],
            }
        )
    catalog = list(groups.values())
    for group in catalog:
        group["options"].sort(
            key=lambda option: (
                0 if option["script_recommended"] else 1,
                0 if option["source_id"] == "ai-choice" else 1,
                str(option["source_id"]),
                str(option["task_id"]),
            )
        )
    tree = _ordered_tree(state)
    tree_nodes = tree.get("nodes", {})
    ordered_hypotheses: list[str] = []

    def walk(hypothesis_id: str) -> None:
        node = tree_nodes.get(hypothesis_id, {}) if isinstance(tree_nodes, dict) else {}
        for child_id in node.get("child_hypothesis_ids", []):
            ordered_hypotheses.append(str(child_id))
            walk(str(child_id))

    if isinstance(tree, dict) and tree.get("root_hypothesis_id"):
        walk(str(tree["root_hypothesis_id"]))
    hypothesis_rank = {
        hypothesis_id: rank for rank, hypothesis_id in enumerate(ordered_hypotheses)
    }
    catalog.sort(
        key=lambda group: (
            hypothesis_rank.get(str(group.get("hypothesis_id")), 10**9),
            str(group["request_type"]),
            str(group["question_id"]),
        )
    )
    return {
        "catalog_version": BRIEF_VERSION,
        "session_id": state.get("session_id"),
        "trip_scope": state.get("trip_scope"),
        "active_hypothesis": copy.deepcopy(state.get("active_hypothesis")),
        "ordered_hypothesis_tree": _ordered_tree(state),
        "hypothesis_outcomes": copy.deepcopy(state.get("hypothesis_outcomes", {})),
        "frontier_research_view": copy.deepcopy(state.get("frontier_research_view", {})),
        "question_count": len(catalog),
        "actionable_task_count": len(actionable),
        "questions": catalog,
        "ai_authority": (
            "Choose any task_id, including ai-choice. Script recommendation, hold, "
            "budget, quota, and priority metadata are advisory. Render only the "
            "chosen task as a full brief."
        ),
    }


def catalog_view(
    catalog: dict[str, Any],
    *,
    request_type: str | None = None,
    hypothesis_id: str | None = None,
    show_all: bool = False,
) -> dict[str, Any]:
    all_questions = list(catalog["questions"])
    request_type_counts: dict[str, int] = {}
    for question in all_questions:
        key = str(question.get("request_type", "unknown"))
        request_type_counts[key] = request_type_counts.get(key, 0) + 1
    active = catalog.get("active_hypothesis")
    active_id = (
        active.get("hypothesis_id") if isinstance(active, dict) else None
    )
    outcomes = catalog.get("hypothesis_outcomes", {})
    if hypothesis_id:
        shown = [
            question
            for question in all_questions
            if question.get("hypothesis_id") == hypothesis_id
        ]
        filter_description = f"hypothesis_id={hypothesis_id}"
    elif request_type:
        shown = [
            question
            for question in all_questions
            if question.get("request_type") == request_type
        ]
        filter_description = f"request_type={request_type}"
    elif show_all:
        shown = all_questions
        filter_description = "all_questions"
    elif active_id:
        shown = [
            question
            for question in all_questions
            if question.get("hypothesis_id") == active_id
        ]
        filter_description = f"active_hypothesis={active_id}"
    else:
        eligible = [
            question
            for question in all_questions
            if outcomes.get(str(question.get("hypothesis_id")), {}).get("status")
            not in {"complete", "hard_dead", "deferred_low_value"}
        ]
        ordered = eligible
        shown = []
        represented: set[str] = set()
        for question in ordered:
            current = str(question.get("hypothesis_id") or question["question_id"])
            if current in represented:
                continue
            represented.add(current)
            shown.append(question)
            if len(shown) >= 5:
                break
        filter_description = "five_diverse_route_hypotheses"
    result = copy.deepcopy(catalog)
    result["total_question_count"] = len(all_questions)
    result["question_count"] = len(shown)
    result["hidden_question_count"] = len(all_questions) - len(shown)
    result["request_type_counts"] = dict(sorted(request_type_counts.items()))
    result["view_filter"] = filter_description
    result["questions"] = shown
    result["actionable_task_count"] = sum(
        len(question["options"]) for question in shown
    )
    return result


def _scope_text(scope: dict[str, Any]) -> str:
    nodes = scope["node_ids"]
    selected = ", ".join(nodes) if nodes else "(no explicit node)"
    region = scope.get("region")
    if region:
        selected = f"{region['label']} [{selected}]"
    return f"{selected} ({scope['policy']})"


def render_markdown(brief_set: dict[str, Any]) -> str:
    lines = [
        "# AI browser execution briefs",
        "",
        (
            f"Session: `{brief_set.get('session_id') or 'unknown'}` · actionable options: "
            f"{brief_set['actionable_task_count']} "
            f"({brief_set['pending_task_count']} script-recommended, "
            f"{brief_set['candidate_task_count']} additional candidates)"
        ),
    ]
    for brief in brief_set["briefs"]:
        budget = brief["price_improvement"]
        if budget["available"]:
            amount = f"{budget['remaining_amount']:g}"
            budget_text = f"{amount} {budget.get('currency') or '(currency unspecified)'}; {budget['application']}"
        else:
            budget_text = budget["application"]
        field_names = ", ".join(item["field"] for item in brief["must_capture"])
        lines.extend(
            [
                "",
                f"## {brief['task_sequence']}. {brief['task_id']} — {brief['task_family']}",
                "",
                f"- Status: {brief['task_status']} · script recommendation is advisory; AI chooses the next action",
                f"- Purpose: {brief['task_purpose']}",
                f"- Scope: {_scope_text(brief['search_scope']['from'])} → {_scope_text(brief['search_scope']['to'])}",
                f"- Provider query-shape ladder: {json.dumps(brief['query_scope_ladder']['scope_ladder'], ensure_ascii=False, sort_keys=True)}",
                f"- Frontier context: {json.dumps(brief.get('frontier_context'), ensure_ascii=False, sort_keys=True) if brief.get('frontier_context') else 'none'}",
                f"- Window: {brief['time_window']['earliest_departure']} → {brief['time_window']['latest_arrival']}",
                f"- Date search: {brief['date_search']['mode']} — {brief['date_search']['execution_rule']}",
                f"- Corridor: {json.dumps(brief['corridor_context'], ensure_ascii=False, sort_keys=True) if brief.get('corridor_context') else 'conventional/unclassified'}",
                f"- Modes: {', '.join(brief['candidate_modes'])}",
                f"- Price-improvement allowance: {budget_text}",
                (
                    f"- Assigned source: {brief['assigned_source']['name']} "
                    f"(`{brief['assigned_source']['source_id']}`) — "
                    f"{brief['assigned_source']['url']}"
                    if brief.get("assigned_source")
                    else "- Assigned source: legacy generic provider task"
                ),
                (
                    f"- Source admission: {brief['assigned_source']['admission_policy']} — "
                    f"{brief['assigned_source']['fulfillment_rule']}"
                    if brief.get("assigned_source")
                    else "- Source admission: capture a named provider and exact evidence."
                ),
                f"- Suggested sources: {', '.join(brief['suggested_source_categories'])}",
                f"- Must capture: {field_names}",
                "- Flight identity: provide at least one of marketing/operating carrier, plus either all displayed flight numbers or one permitted structured unknown reason.",
                "- Multiple sellers: preserve each platform's offer separately; never overwrite another seller's quote for the same itinerary.",
                "- Research provenance: preserve Matrix/calendar discovery as research_only and admit only the separately verified seller quote.",
                f"- No result: {brief['no_result']['meaning']} {brief['no_result']['does_not_mean']}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_catalog_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# AI research decision catalog",
        "",
        (
            f"Session: `{catalog.get('session_id') or 'unknown'}` · questions: "
            f"{catalog['question_count']} shown / {catalog.get('total_question_count', catalog['question_count'])} total · source/task options shown: "
            f"{catalog['actionable_task_count']}"
        ),
        "",
        catalog["ai_authority"],
        f"Active hypothesis: {json.dumps(catalog.get('active_hypothesis'), ensure_ascii=False, sort_keys=True) if catalog.get('active_hypothesis') else 'none'}",
        f"View: {catalog.get('view_filter', 'all_questions')} · hidden questions: {catalog.get('hidden_question_count', 0)}",
        f"Question categories: {json.dumps(catalog.get('request_type_counts', {}), ensure_ascii=False, sort_keys=True)}",
        f"Frontier research: {json.dumps(catalog.get('frontier_research_view', {}), ensure_ascii=False, sort_keys=True)}",
    ]
    for index, question in enumerate(catalog["questions"], 1):
        recommended = next(
            (option for option in question["options"] if option["script_recommended"]),
            None,
        )
        ai_choice = next(
            (option for option in question["options"] if option["source_id"] == "ai-choice"),
            None,
        )
        recommended_text = (
            f"{recommended['source_id']}={recommended['task_id']}"
            if recommended
            else "none"
        )
        ai_choice_text = ai_choice["task_id"] if ai_choice else "not available"
        suggested_sources = sorted(
            {
                source_id
                for option in question["options"]
                for source_id in option.get("source_suggestion_ids", [])
            }
        )
        lines.extend(
            [
                "",
                f"## {index}. {question['request_type']} — {question['question_id']}",
                f"- Hypothesis: {question.get('hypothesis_id')} ({question.get('hypothesis_kind')}); route shape: {question.get('route_shape') or 'unclassified'}",
                f"- Tree: parent={question.get('parent_hypothesis_id') or 'root'} · sibling order={question.get('sibling_order') or 'unassigned'} · status={question.get('tree_status') or 'unknown'} · next task={question.get('next_task_id') or 'none'}",
                f"- Scope: {', '.join(question['origins']) or '(open)'} → {', '.join(question['destinations']) or '(open)'}",
                f"- Window: {question['earliest_departure']} → {question['latest_arrival']}",
                f"- Modes: {', '.join(question['suggested_modes'])}",
                f"- Script recommendation: {recommended_text}",
                f"- Open AI source choice: {ai_choice_text}",
                f"- Source suggestions: {', '.join(suggested_sources) if suggested_sources else '(AI chooses without registry suggestions)'}",
                f"- Additional source-specific options available: {max(0, len(question['options']) - int(recommended is not None) - int(ai_choice is not None))}",
            ]
        )
    return "\n".join(lines) + "\n"


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserTaskBriefError(f"Cannot read JSON {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render an AI decision catalog or one selected browser task"
    )
    parser.add_argument("--state", required=True, help="live provider session state JSON")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument(
        "--task-id",
        help="render one candidate/pending task as a full execution brief; omit for the compact decision catalog",
    )
    parser.add_argument(
        "--request-type",
        help="show only one request_type in the decision catalog",
    )
    parser.add_argument(
        "--hypothesis-id",
        help="show every actionable question that belongs to one route hypothesis",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="show every research question instead of the default recommended summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state = read_json(args.state)
        if args.task_id:
            tasks = state.get("research_tasks", [])
            task = next(
                (
                    item
                    for item in tasks
                    if isinstance(item, dict) and item.get("task_id") == args.task_id
                ),
                None,
            )
            if task is None:
                raise BrowserTaskBriefError(
                    f"task_id is not present in this session: {args.task_id}"
                )
            briefs = {
                "brief_set_version": BRIEF_VERSION,
                "session_id": state.get("session_id"),
                "trip_scope": state.get("trip_scope"),
                "actionable_task_count": 1,
                "pending_task_count": int(task.get("status") == "pending"),
                "candidate_task_count": int(task.get("status") == "candidate"),
                "ordered_hypothesis_tree": _ordered_tree(state),
                "briefs": [task_to_brief(task, state, 1)],
            }
            output = render_markdown(briefs) if args.format == "markdown" else json.dumps(briefs, ensure_ascii=False, indent=2)
        else:
            catalog = catalog_view(
                build_catalog(state),
                request_type=args.request_type,
                hypothesis_id=args.hypothesis_id,
                show_all=args.all,
            )
            output = render_catalog_markdown(catalog) if args.format == "markdown" else json.dumps(catalog, ensure_ascii=False, indent=2)
        print(output, end="" if output.endswith("\n") else "\n")
        return 0
    except BrowserTaskBriefError as exc:
        print(f"browser_task_brief: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BRIEF_VERSION",
    "BrowserTaskBriefError",
    "build_catalog",
    "build_briefs",
    "catalog_view",
    "render_catalog_markdown",
    "render_markdown",
    "task_to_brief",
]
