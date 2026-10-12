"""Render a compact round-trip decision catalog or one browser evidence brief."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any


BRIEF_VERSION = "0.5.0"
REGISTRY_VERSION = "0.5.0"


def _load_session_module():
    path = Path(__file__).with_name("round_trip_session.py")
    spec = importlib.util.spec_from_file_location("round_trip_session", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SESSION = _load_session_module()


def _load_advisor_module():
    path = Path(__file__).with_name("round_trip_provider_advisor.py")
    spec = importlib.util.spec_from_file_location("round_trip_provider_advisor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ADVISOR = _load_advisor_module()


def _load_hypothesis_generator_module():
    path = Path(__file__).with_name("round_trip_hypothesis_generator.py")
    spec = importlib.util.spec_from_file_location("round_trip_hypothesis_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HYPOTHESIS_GENERATOR = _load_hypothesis_generator_module()


class BrowserBriefError(ValueError):
    pass


SEMANTIC_RESEARCH_KINDS = {
    "region_scope_definition",
}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _registry(path: str | Path | None = None) -> dict[str, Any]:
    try:
        return ADVISOR.load_registry(path)
    except ADVISOR.ProviderAdvisorError as exc:
        raise BrowserBriefError(str(exc)) from exc


def _source_advice(state: dict[str, Any], task: dict[str, Any], registry_path: str | Path | None = None) -> dict[str, Any]:
    try:
        return ADVISOR.recommend_sources(state, task, registry_path)
    except ADVISOR.ProviderAdvisorError as exc:
        raise BrowserBriefError(str(exc)) from exc


def _source_options(state: dict[str, Any], task: dict[str, Any], registry_path: str | Path | None = None) -> list[dict[str, Any]]:
    if task.get("research_kind") in SEMANTIC_RESEARCH_KINDS:
        return []
    advice = _source_advice(state, task, registry_path)
    return [
        {
            "source_id": "ai-choice",
            "name": "Any capability-matched source chosen by the AI",
            "url": None,
            "source_type": "open_ai_choice",
            "evidence_role": "depends_on_actual_source",
            "binding": False,
        },
        *copy.deepcopy(advice["suggestions"][:2]),
    ]


def _actionable(state: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [item for item in state["research_tasks"] if item["status"] in {"candidate", "pending"}]
    active = state.get("active_hypothesis")
    if isinstance(active, dict):
        tasks = [item for item in tasks if item["hypothesis_id"] == active["hypothesis_id"]]
    return tasks


def build_catalog(raw_state: Any, registry_path: str | Path | None = None) -> dict[str, Any]:
    state = SESSION.validate_session(raw_state)
    _registry(registry_path)
    actionable = _actionable(state)
    hypotheses = []
    nodes = state["hypothesis_tree"]["nodes"]
    for node in sorted((item for item in nodes.values() if item["hypothesis_id"] != SESSION.ROOT_HYPOTHESIS), key=lambda item: (item.get("sibling_order", 10**9), item["hypothesis_id"])):
        tasks = [item for item in actionable if item["hypothesis_id"] == node["hypothesis_id"]]
        hypotheses.append(
            {
                "hypothesis_id": node["hypothesis_id"],
                "hypothesis_kind": node["hypothesis_kind"],
                "route_shape": node["route_shape"],
                "status": node["status"],
                "is_next_candidate": state["hypothesis_tree"].get("next_candidate_id") == node["hypothesis_id"],
                "tasks": [
                    {
                        "task_id": task["task_id"],
                        "status": task["status"],
                        "question": task["question"],
                        "research_kind": task["research_kind"],
                        "journey_scope": task.get("journey_scope"),
                        "temporal_phase": task.get("temporal_state", {}).get("phase", "not_applicable"),
                        "source_option_count": len(_source_options(state, task, registry_path)),
                        "source_suggestion_ids": [item["source_id"] for item in _source_options(state, task, registry_path)[:3]],
                    }
                    for task in tasks
                ],
            }
        )
    route_suggestions = HYPOTHESIS_GENERATOR.generate_suggestions(state) if state["active_hypothesis"] is None else {"suggestions": [], "selection_instruction": "Close or pause the active hypothesis before considering another route."}
    return {
        "catalog_version": BRIEF_VERSION,
        "decision_owner": "ai",
        "source_registry_is_whitelist": False,
        "registry_order_used_as_preference": False,
        "session_id": state["session_id"],
        "active_hypothesis": copy.deepcopy(state.get("active_hypothesis")),
        "next_candidate_id": state["hypothesis_tree"].get("next_candidate_id"),
        "gateway_candidate_counts": {side: len(items) for side, items in state["gateway_candidates"].items()},
        "actionable_task_count": len(actionable),
        "hypotheses": hypotheses,
        "route_hypothesis_suggestions": copy.deepcopy(route_suggestions["suggestions"]),
        "route_suggestion_instruction": route_suggestions["selection_instruction"],
        "selection_instruction": "Choose any actionable task. Gateway discovery precedes route locking; examples never define the airport pool. Continue a selected hypothesis until terminal disposition or pause.",
    }


def task_to_brief(raw_state: Any, task_id: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    state = SESSION.validate_session(raw_state)
    _registry(registry_path)
    task = next((item for item in state["research_tasks"] if item["task_id"] == task_id), None)
    if task is None or task["status"] not in {"candidate", "pending"}:
        raise BrowserBriefError("task must be an AI-actionable candidate or pending task")
    pool_screen = task["research_kind"] == "region_pool_fare_screen"
    semantic = task["research_kind"] in SEMANTIC_RESEARCH_KINDS
    temporal_state = copy.deepcopy(task.get("temporal_state", {}))
    temporal_applicable = temporal_state.get("phase") not in {None, "not_applicable", "terminal"}
    if semantic:
        contract = {
            "evidence_status": "ai_proposed",
            "proposal_type": "region_scope",
            "rule": (
                "Use AI semantic judgment and return a bounded structured proposal. This is "
                "persisted AI state, not live Provider evidence and not a priced offer."
            ),
        }
    elif pool_screen:
        contract = {
            "evidence_status": "research_observed",
            "provider_scope": {
                "coverage_semantics": "provider_ranked_window",
                "price_selection_method": "price_sorted_api",
                "rule": (
                    "Report the exact airport partition, complete departure windows, "
                    "and stay range actually submitted to the Provider."
                ),
            },
            "signal_kind": "joint_date_pair",
            "required_signal_fields": [
                "origin_gateway_node_id",
                "destination_gateway_node_id",
                "outbound_origin_gateway_node_id",
                "outbound_destination_gateway_node_id",
                "return_origin_gateway_node_id",
                "return_destination_gateway_node_id",
                "outbound_date",
                "return_date",
                "atomic_total",
            ],
            "price_selection_method": "Required: price_sorted_api.",
            "rule": (
                "Use a Provider-native flexible-window query over the complete date "
                "and stay range. Search the requested airport pools in one query when "
                "supported; otherwise submit non-empty airport partitions in later "
                "attempts of this same Work Item. Each coverage-closing Observation "
                "carries its actual provider_scope and only that partition's local "
                "minimum or complete no-market result. The Product Kernel, not the "
                "Worker, derives coverage. "
                "Manual calendars, sampled dates, and Google Flights date-grid browsing "
                "may omit provider_scope to rank candidates, but cannot close this pool screen."
            ),
        }
    else:
        contract = {
            "evidence_status": "verified_observed",
            "observation_identity": ["observation_id", "source_id", "provider", "url", "observed_at", "price_selection_method"],
            "movement_fields": ["movement_id", "from_node", "to_node", "departure", "arrival", "mode", "operator", "service_id", "flight_identity for flight", "transfer_from_previous with status and connection_protection when researched"],
            "offer_fields": ["offer_id", "provider_offer_id", "source_id", "pricing_scope=atomic_offer", "journey_scope", "direction when one_way", "movement_ids", "price", "mandatory_baggage_cost", "required_accommodation_cost when unavoidable", "included_baggage", "provider", "seller", "url", "observed_at", "expires_at when visible", "ticket_context for planned exit", "evidence_status=verified_observed"],
            "rule": "Preserve every atomic ticket or surface quote whole. The price and mandatory baggage cost must cover the brief's traveler_count and baggage_policy. A required item missing from included_baggage is admissible only when paid_baggage_allowed=true and the Offer records a positive mandatory_baggage_cost. Never invent component prices; include RegionNode objects for newly observed movement endpoints. Mark connection_protection=carrier_protected only when the payable itinerary or seller explicitly establishes a protected through-ticket; atomic pricing alone is not proof.",
            "temporal_research_option": (
                {
                    "evidence_status": "research_observed",
                    "coverage_complete": False,
                    "signal_kind": "joint_date_pair" if task.get("journey_scope") == "joint_round_trip" else "directional_date",
                    "admission_role": "research_only",
                    "creates_offer_or_movement": False,
                    "rule": "Calendar, grid, and flexible-search signals keep this same task pending. They never become payable evidence. The AI may instead proceed directly to exact verification when useful.",
                }
                if temporal_applicable
                else None
            ),
        }
    advice = (
        {
            "dynamic_source_rules": [],
            "task_family": "ai_semantic_exploration",
        }
        if semantic
        else _source_advice(state, task, registry_path)
    )
    query_shape = copy.deepcopy(task["query_shape"])
    if pool_screen:
        query_shape["coverage_progress"] = SESSION.pool_screen_progress(
            state, task_id
        )
    if temporal_state.get("active_selection") is not None:
        query_shape["ai_selected_exact_target"] = copy.deepcopy(temporal_state["active_selection"])
    return {
        "brief_version": BRIEF_VERSION,
        "task_id": task["task_id"],
        "task_status": task["status"],
        "hypothesis": {key: task.get(key) for key in ("hypothesis_id", "hypothesis_kind", "parent_hypothesis_id", "route_shape")},
        "research_kind": task["research_kind"],
        "query_shape": query_shape,
        "traveler_count": state["scenario"]["intent"]["traveler_count"],
        "baggage_policy": copy.deepcopy(state["scenario"]["intent"]["baggage_policy"]),
        "source_options": _source_options(state, task, registry_path),
        "dynamic_source_rules": advice["dynamic_source_rules"],
        "source_policy": {"decision_owner": "ai", "registry_is_whitelist": False, "registry_order_is_preference": False, "one_task_per_research_question": True, "source_fan_out": False, "task_family": advice["task_family"], "research_only_source_requires_payable_offer_verification": not semantic},
        "temporal_execution": {
            **temporal_state,
            "decision_owner": "ai",
            "selection_advice_binding": False,
            "scheduler_may_not_publish_or_select_tasks": True,
            "same_task_id_for_surface_and_exact_verification": True,
        },
        "evidence_contract": contract,
        "terminal_evidence_contract": (
            {
                "fields": [
                    "observation_id",
                    "source_id",
                    "evidence_status=no_result|rejected",
                    "provider",
                    "url",
                    "observed_at",
                    "reason",
                    "provider_scope for a complete no-market partition",
                    "price_selection_method=price_sorted_api",
                ]
            }
            if pool_screen
            else {
                "fields": [
                    "observation_id",
                    "source_id",
                    "evidence_status=no_result|rejected",
                    "provider",
                    "url",
                    "observed_at",
                    "reason",
                    "date_coverage when the Provider exposes a date scope",
                ]
            }
            if not semantic
            else None
        ),
        "observations_template": {
            "observation_version": SESSION.SESSION_VERSION,
            "bundle_id": "<stable-unique-bundle-id>",
            "task_id": task["task_id"],
            "coverage_complete": False
            if pool_screen or temporal_state.get("phase") == "surface_discovery"
            else True,
            "observations": [
                "<one ai_proposed region_scope proposal>"
                if semantic
                else "<pool-wide joint date-pair signals>"
                if pool_screen
                else "<research-only temporal signals, verified payable offer, or scoped terminal observation>"
            ],
        },
    }


def catalog_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# AI round-trip research decision catalog",
        "",
        f"- Active hypothesis: {catalog['active_hypothesis'] or 'none'}",
        f"- Next candidate (advisory): {catalog['next_candidate_id']}",
        f"- Gateway candidates: {catalog['gateway_candidate_counts']}",
        f"- Actionable tasks: {catalog['actionable_task_count']}",
        "- Source registry is not a whitelist; AI owns source and route choice.",
        "- Work Items carry at most three source hints; registry file order is ignored.",
    ]
    for hypothesis in catalog["hypotheses"]:
        lines.extend(["", f"## {hypothesis['hypothesis_id']}", f"- Kind: {hypothesis['hypothesis_kind']}", f"- Shape: {hypothesis['route_shape']}", f"- Status: {hypothesis['status']}"])
        for task in hypothesis["tasks"]:
            lines.append(f"- `{task['task_id']}` [{task['status']}] {task['research_kind']}/{task.get('journey_scope')}: {task['question']}")
    if catalog.get("route_hypothesis_suggestions"):
        lines.extend(["", "## Evidence-backed route suggestions"])
        for suggestion in catalog["route_hypothesis_suggestions"]:
            lines.append(f"- `{suggestion['hypothesis_id']}` {suggestion['route_shape']}: {suggestion['origin_gateway_nodes']} -> {suggestion['destination_gateway_nodes']} — joint atomic quote is the first baseline")
        lines.append(catalog["route_suggestion_instruction"])
    lines.extend(["", catalog["selection_instruction"]])
    return "\n".join(lines) + "\n"


def brief_markdown(brief: dict[str, Any]) -> str:
    lines = [
        "# AI browser execution brief",
        "",
        f"- Task: `{brief['task_id']}`",
        f"- Hypothesis: `{brief['hypothesis']['hypothesis_id']}`",
        f"- Research kind: `{brief['research_kind']}`",
        f"- Query shape: `{json.dumps(brief['query_shape'], ensure_ascii=False)}`",
        f"- Evidence rule: {brief['evidence_contract']['rule']}",
        "- Source choice belongs to the AI; suggestions are non-binding.",
        "",
        "## Source options",
    ]
    for source in brief["source_options"]:
        lines.append(f"- {source['name']} (`{source['source_id']}`): {source['evidence_role']} — {source['url'] or 'AI-selected URL'}")
    active_dynamic = [item for item in brief.get("dynamic_source_rules", []) if item.get("activation_state") == "applicable"]
    if active_dynamic:
        lines.extend(["", "## Triggered dynamic sources"])
        for source in active_dynamic:
            lines.append(f"- {source['name']} (`{source['source_id']}`): {source['trigger']}")
    lines.extend(["", "## Evidence output", "", "```json", json.dumps(brief["observations_template"], ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--registry")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args(argv)
    try:
        state = read_json(args.state)
        value = task_to_brief(state, args.task_id, args.registry) if args.task_id else build_catalog(state, args.registry)
        output = json.dumps(value, ensure_ascii=False, indent=2) if args.format == "json" else (brief_markdown(value) if args.task_id else catalog_markdown(value))
        print(output, end="" if output.endswith("\n") else "\n")
        return 0
    except (OSError, json.JSONDecodeError, SESSION.RoundTripSessionError, BrowserBriefError) as exc:
        print(f"round_trip_browser_task_brief: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
