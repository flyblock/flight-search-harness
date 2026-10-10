"""Advisory Provider capability matching for round-trip research tasks."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


VERSION = "0.5.0"
EUROPE_CODES = {
    "AL", "AD", "AT", "BE", "BA", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT", "LV", "LI", "LT", "LU", "MT", "MD", "MC", "ME", "NL", "MK", "NO", "PL", "PT", "RO", "SM", "RS", "SK", "SI", "ES", "SE", "CH", "TR", "UA", "GB", "VA"
}
MEDITERRANEAN_CODES = {"AL", "BA", "HR", "CY", "FR", "GR", "IT", "MT", "ME", "SI", "ES", "TR"}
ASIA_PACIFIC_CODES = {"AU", "BN", "KH", "CN", "FJ", "HK", "ID", "JP", "LA", "MO", "MY", "MM", "NZ", "PH", "SG", "KR", "TW", "TH", "TL", "VN"}


class ProviderAdvisorError(ValueError):
    pass


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _paths(registry_path: str | Path | None, policy_path: str | Path | None) -> tuple[Path, Path]:
    root = Path(__file__).parents[1]
    return (
        Path(registry_path) if registry_path else root / "data" / "source-registry.json",
        Path(policy_path) if policy_path else root / "data" / "provider-execution-policy.json",
    )


def load_contract(registry_path: str | Path | None = None, policy_path: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    registry_file, policy_file = _paths(registry_path, policy_path)
    registry = read_json(registry_file)
    policy = read_json(policy_file)
    if registry.get("registry_version") != VERSION or policy.get("policy_version") != VERSION:
        raise ProviderAdvisorError(f"Provider contracts must use version {VERSION}")
    if registry.get("not_a_whitelist") is not True or registry.get("decision_owner") != "ai":
        raise ProviderAdvisorError("registry must preserve AI authority and non-whitelist semantics")
    if policy.get("decision_owner") != "ai" or policy.get("one_question_one_task") is not True or policy.get("source_fan_out") is not False:
        raise ProviderAdvisorError("policy must preserve one-question/one-task AI-led execution")
    source_ids = [item.get("source_id") for item in registry.get("sources", [])]
    policy_ids = [item.get("source_id") for item in policy.get("source_policies", [])]
    if len(source_ids) != len(set(source_ids)) or len(policy_ids) != len(set(policy_ids)):
        raise ProviderAdvisorError("Provider contracts contain duplicate source IDs")
    if set(source_ids) != set(policy_ids):
        raise ProviderAdvisorError(f"registry/policy source mismatch: registry_only={sorted(set(source_ids)-set(policy_ids))} policy_only={sorted(set(policy_ids)-set(source_ids))}")
    return registry, policy


def task_family(task: dict[str, Any]) -> str:
    if task.get("research_kind") == "refresh":
        return "refresh"
    if task.get("research_kind") == "gateway_discovery":
        return "gateway_discovery"
    scope = task.get("journey_scope")
    if scope == "joint_round_trip":
        return "joint_quote"
    if scope == "one_way":
        return "split_outbound" if task.get("direction") == "outbound" else "split_return"
    if scope == "atomic_itinerary":
        return "nested_atomic"
    if scope == "surface":
        return "surface_access"
    raise ProviderAdvisorError(f"cannot derive task family from task {task.get('task_id')}")


def _node_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for node in state.get("scenario", {}).get("nodes", []):
        if isinstance(node, dict) and node.get("node_id"):
            result[str(node["node_id"])] = node
    return result


def _query_node_ids(task: dict[str, Any]) -> set[str]:
    query = task.get("query_shape", {})
    result: set[str] = set()
    for key in ("anchor_node_ids", "origins", "destinations"):
        value = query.get(key, [])
        if isinstance(value, list):
            result.update(str(item) for item in value)
    return result


def _geography(state: dict[str, Any], task: dict[str, Any]) -> tuple[set[str], set[str]]:
    nodes = _node_map(state)
    country_codes = {str(nodes[node_id].get("country_code")) for node_id in _query_node_ids(task) if node_id in nodes and nodes[node_id].get("country_code")}
    tags = set()
    if country_codes & EUROPE_CODES:
        tags.add("europe")
    if country_codes & MEDITERRANEAN_CODES:
        tags.add("mediterranean")
    if country_codes & ASIA_PACIFIC_CODES:
        tags.update({"asia_pacific", "selected_asia"})
    if "DE" in country_codes:
        tags.add("germany")
    if "US" in country_codes:
        tags.add("united_states")
    if country_codes & {"MX", "BZ", "CR", "SV", "GT", "HN", "NI", "PA"}:
        tags.add("central_america")
    if country_codes & {"AG", "BS", "BB", "CU", "DM", "DO", "GD", "HT", "JM", "KN", "LC", "VC", "TT"}:
        tags.add("caribbean")
    return country_codes, tags


def _task_modes(state: dict[str, Any], task: dict[str, Any]) -> set[str]:
    family = task_family(task)
    if family in {"joint_quote", "split_outbound", "split_return", "nested_atomic"}:
        return {"flight"}
    query = task.get("query_shape", {})
    constraints = query.get("pool_constraints", {})
    if isinstance(constraints.get("allowed_modes"), list):
        return {str(item) for item in constraints["allowed_modes"]}
    allowed = state.get("scenario", {}).get("intent", {}).get("allowed_modes", [])
    return {str(item) for item in allowed}


def _activation(source: dict[str, Any], source_policy: dict[str, Any], state: dict[str, Any], task: dict[str, Any]) -> tuple[str, list[str]]:
    activation = source_policy.get("activation")
    _, tags = _geography(state, task)
    regions = set(source.get("regions", []))
    query = task.get("query_shape", {})
    configured = set(query.get("configured_source_ids", [])) | set(state.get("scenario", {}).get("configured_source_ids", []))
    operator_hints = set(query.get("operator_hints", []))
    if activation == "global":
        return "applicable", ["global capability match"]
    if activation == "configured_source":
        return ("applicable", ["configured deployment supplied"]) if source["source_id"] in configured else ("configuration_required", ["requires a configured endpoint or feed"])
    if activation == "regional":
        if regions & tags or any(str(item).startswith("global_") for item in regions):
            return "applicable", [f"regional match: {sorted(regions & tags)}"]
        return "not_applicable", ["task geography is outside registered regional strength"]
    if activation == "regional_market_or_identified_operator":
        if regions & tags:
            return "applicable", [f"regional market match: {sorted(regions & tags)}"]
        if source["source_id"] in operator_hints:
            return "applicable", ["identified operator match"]
        return "awaiting_trigger", ["requires matching regional market or identified operator"]
    return "not_applicable", ["unknown activation rule"]


def _attempted_sources(state: dict[str, Any], task: dict[str, Any]) -> tuple[set[str], set[str]]:
    same_task: set[str] = set()
    same_hypothesis: set[str] = set()
    task_map = {item.get("task_id"): item for item in state.get("research_tasks", [])}
    for bundle in state.get("evidence_bundles", []):
        bundle_task_id = bundle.get("task_id")
        bundle_task = task_map.get(bundle_task_id, {})
        sources = {str(obs.get("source_id")) for obs in bundle.get("observations", []) if obs.get("source_id")}
        if bundle_task_id == task.get("task_id"):
            same_task.update(sources)
        if bundle_task.get("hypothesis_id") == task.get("hypothesis_id"):
            same_hypothesis.update(sources)
    return same_task, same_hypothesis


def _is_flexible(task: dict[str, Any]) -> bool:
    query = task.get("query_shape", {})
    return any(key in query for key in ("travel_windows", "departure_window", "outbound_departure_window", "return_departure_window"))


def _is_multi_airport(task: dict[str, Any]) -> bool:
    query = task.get("query_shape", {})
    return any(isinstance(query.get(key), list) and len(query[key]) > 1 for key in ("origins", "destinations"))


def recommend_sources(state: dict[str, Any], task: dict[str, Any], registry_path: str | Path | None = None, policy_path: str | Path | None = None) -> dict[str, Any]:
    registry, policy = load_contract(registry_path, policy_path)
    family = task_family(task)
    scoring = policy["scoring"]
    policies = {item["source_id"]: item for item in policy["source_policies"]}
    same_task, same_hypothesis = _attempted_sources(state, task)
    task_modes = _task_modes(state, task)
    suggestions = []
    statuses = []
    for source in registry["sources"]:
        source_policy = policies[source["source_id"]]
        base = source_policy.get("task_scores", {}).get(family)
        if base is None:
            statuses.append({"source_id": source["source_id"], "activation_state": "not_applicable", "reason": "task family unsupported"})
            continue
        if task_modes and not task_modes & set(source.get("modes", [])):
            statuses.append({"source_id": source["source_id"], "activation_state": "not_applicable", "reason": "transport mode mismatch"})
            continue
        activation_state, reasons = _activation(source, source_policy, state, task)
        if activation_state != "applicable":
            statuses.append({"source_id": source["source_id"], "activation_state": activation_state, "reason": "; ".join(reasons)})
            continue
        score = int(base)
        capabilities = source.get("capabilities", {})
        if source["source_id"] not in same_task:
            score += scoring["untried_source_bonus"]
            reasons.append("untried for this research question")
        else:
            score -= scoring["same_task_repeat_penalty"]
            reasons.append("already attempted for this research question")
        if source["source_id"] in same_hypothesis and source["source_id"] not in same_task:
            score -= scoring["same_hypothesis_repeat_penalty"]
            reasons.append("already used elsewhere in this hypothesis")
        priced = family not in {"gateway_discovery"}
        if priced and source["evidence_role"] in {"payable_offer", "payable_offer_when_seller_visible"}:
            score += scoring["payable_role_bonus_for_priced_task"]
            reasons.append("can support payable evidence")
        if family == "gateway_discovery" and source["evidence_role"] == "research_only_until_payable_seller_verified":
            score += scoring["research_role_bonus_for_gateway_task"]
            reasons.append("research role fits gateway discovery")
        if _is_multi_airport(task) and capabilities.get("multi_airport") is True:
            score += scoring["multi_airport_bonus"]
            reasons.append("multi-airport query fit")
        if _is_flexible(task) and capabilities.get("flexible_dates") is True:
            score += scoring["flexible_date_bonus"]
            reasons.append("flexible-date fit")
        if family == "nested_atomic" and capabilities.get("independent_airline_combination") is True:
            score += scoring["independent_combination_bonus"]
            reasons.append("independent atomic-combination fit")
        suggestions.append(
            {
                "source_id": source["source_id"],
                "name": source["name"],
                "url": source["url"],
                "source_type": source["source_type"],
                "evidence_role": source["evidence_role"],
                "advisory_score": score,
                "fit_reasons": reasons,
                "browser_notes": copy.deepcopy(source.get("browser_notes", [])),
                "binding": False,
            }
        )
    suggestions.sort(key=lambda item: (-item["advisory_score"], item["source_id"]))
    for rank, item in enumerate(suggestions, start=1):
        item["advisory_rank"] = rank
    dynamic = []
    operator_hints = set(task.get("query_shape", {}).get("operator_hints", []))
    authority_hints = set(task.get("query_shape", {}).get("authority_hints", []))
    for rule in registry.get("dynamic_source_rules", []):
        if family not in rule.get("task_families", []):
            continue
        if rule["source_id"] == "carrier_official_site":
            state_name = "applicable" if operator_hints and family != "surface_access" else "awaiting_trigger"
        elif rule["source_id"] == "operator_official_site":
            state_name = "applicable" if operator_hints and family == "surface_access" else "awaiting_trigger"
        else:
            state_name = "applicable" if authority_hints else "awaiting_trigger"
        dynamic.append({**copy.deepcopy(rule), "activation_state": state_name, "binding": False})
    return {
        "advisor_version": VERSION,
        "decision_owner": "ai",
        "task_id": task.get("task_id"),
        "task_family": family,
        "one_question_one_task": True,
        "source_fan_out": False,
        "registry_order_used_as_preference": False,
        "suggestions": suggestions,
        "dynamic_source_rules": dynamic,
        "source_statuses": statuses,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--registry")
    parser.add_argument("--policy")
    args = parser.parse_args(argv)
    try:
        state = read_json(args.state)
        task = next((item for item in state.get("research_tasks", []) if item.get("task_id") == args.task_id), None)
        if task is None:
            raise ProviderAdvisorError(f"unknown task_id {args.task_id}")
        print(json.dumps(recommend_sources(state, task, args.registry, args.policy), ensure_ascii=False, indent=2))
        return 0
    except (OSError, json.JSONDecodeError, ProviderAdvisorError) as exc:
        print(f"round_trip_provider_advisor: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
