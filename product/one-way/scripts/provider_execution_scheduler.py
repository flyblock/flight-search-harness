from __future__ import annotations

"""Expand engine questions into auditable, source-specific Provider tasks.

The scheduler is deterministic and never opens a browser.  It reads the product
source registry and its execution policy, records a disposition for every
registered source, and gives each applicable source its own immutable request.
"""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SCHEDULER_VERSION = "0.7.1"
PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = PRODUCT_ROOT / "data" / "source-registry.json"
DEFAULT_POLICY = PRODUCT_ROOT / "data" / "provider-execution-policy.json"


class ProviderExecutionError(ValueError):
    pass


FAMILY_ALIASES = {
    "baseline": "baseline",
    "conventional_reference": "baseline",
    "market": "market",
    "origin_market_scan": "market",
    "regional_market_scan": "market",
    "origin_access_market_scan": "market",
    "reverse": "reverse",
    "destination_market_scan": "reverse",
    "corridor_calendar_scan": "corridor_calendar",
    "budgeted_predecessor": "budgeted_predecessor",
    "budgeted_predecessor_scan": "budgeted_predecessor",
    "direct_closure": "budgeted_predecessor",
    "bridge": "bridge",
    "unresolved_connector": "bridge",
    "predecessor_connector_scan": "bridge",
    "corridor_connector_research": "bridge",
    "refresh": "refresh",
    "refresh_quote": "refresh",
}


MODE_ALIASES = {
    "flight": {"flight"},
    "rail": {"rail"},
    "bus": {"bus"},
    "coach": {"bus"},
    "connecting_bus": {"bus"},
    "ferry": {"ferry"},
    "public_transit": {"local_transit"},
    "local_transit": {"local_transit"},
    "walking": {"walk"},
    "walk": {"walk"},
    "automobile": {"car"},
    "car": {"car"},
    "shared_mobility": {"rideshare"},
    "optional_ground_airport_transfer": {"rail", "bus", "local_transit", "walk", "car"},
}


EUROPE = {
    "AL", "AT", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE", "DK",
    "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT",
    "LI", "LT", "LU", "LV", "MD", "ME", "MK", "MT", "NL", "NO", "PL",
    "PT", "RO", "RS", "SE", "SI", "SK", "TR", "UA",
}
ASIA_PACIFIC = {
    "AE", "AF", "AU", "BD", "BN", "BT", "CN", "FJ", "HK", "ID", "IL",
    "IN", "IQ", "IR", "JP", "KG", "KH", "KR", "KZ", "LA", "LK", "MM",
    "MN", "MO", "MV", "MY", "NP", "NZ", "OM", "PH", "PK", "QA", "SA",
    "SG", "TH", "TJ", "TL", "TM", "TW", "UZ", "VN",
}
NORTH_AMERICA = {"CA", "MX", "US"}
CENTRAL_AMERICA_CARIBBEAN = {
    "AG", "AI", "AW", "BB", "BS", "BZ", "CR", "CU", "CW", "DM", "DO",
    "GD", "GT", "HN", "HT", "JM", "KN", "KY", "LC", "NI", "PA", "PR",
    "SV", "TC", "TT", "VC", "VG", "VI",
}
AFRICA = {
    "DZ", "EG", "ET", "GH", "KE", "MA", "MU", "NG", "SN", "TN", "TZ",
    "UG", "ZA",
}


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderExecutionError(f"Cannot read JSON {path}: {exc}") from exc


def _write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _catalog(
    registry_path: str | Path = DEFAULT_REGISTRY,
    policy_path: str | Path = DEFAULT_POLICY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = _read_json(registry_path)
    policy = _read_json(policy_path)
    if policy.get("policy_version") != SCHEDULER_VERSION:
        raise ProviderExecutionError(
            f"provider execution policy must be {SCHEDULER_VERSION}"
        )
    sources = registry.get("sources")
    rules = registry.get("dynamic_source_rules")
    source_policies = policy.get("source_policies")
    rule_policies = policy.get("dynamic_rule_policies")
    if not all(isinstance(value, list) for value in (sources, rules, source_policies, rule_policies)):
        raise ProviderExecutionError("registry and policy source collections must be arrays")
    source_ids = [item.get("source_id") for item in sources]
    policy_ids = [item.get("source_id") for item in source_policies]
    rule_ids = [item.get("source_id") for item in rules]
    rule_policy_ids = [item.get("source_id") for item in rule_policies]
    for label, values in (
        ("registry sources", source_ids),
        ("source policies", policy_ids),
        ("dynamic rules", rule_ids),
        ("dynamic rule policies", rule_policy_ids),
    ):
        if any(not isinstance(value, str) or not value for value in values):
            raise ProviderExecutionError(f"{label} contain an invalid source_id")
        if len(values) != len(set(values)):
            raise ProviderExecutionError(f"{label} contain duplicate source_id values")
    if set(source_ids) != set(policy_ids):
        raise ProviderExecutionError(
            "provider execution policy must cover every static registry source exactly once"
        )
    if set(rule_ids) != set(rule_policy_ids):
        raise ProviderExecutionError(
            "provider execution policy must cover every dynamic registry rule exactly once"
        )
    return registry, policy


def _family(request: dict[str, Any]) -> str:
    raw = str(request.get("request_type", "")).strip().lower()
    family = FAMILY_ALIASES.get(raw)
    if family is None:
        raise ProviderExecutionError(f"unsupported provider request type: {raw}")
    return family


def _request_modes(request: dict[str, Any]) -> set[str]:
    raw = request.get("suggested_modes", request.get("allowed_modes", ["flight"]))
    if not isinstance(raw, list) or not raw:
        raise ProviderExecutionError("provider request modes must be a non-empty array")
    return {str(value).strip().lower() for value in raw if str(value).strip()}


def _source_modes(source: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for raw in source.get("modes", []):
        result.update(MODE_ALIASES.get(str(raw).lower(), {str(raw).lower()}))
    return result


def _node_ids(request: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for field in ("origins", "destinations"):
        values = request.get(field, [])
        if isinstance(values, list):
            result.update(str(value).upper() for value in values if value)
    for field in ("from_node", "to_node"):
        if request.get(field):
            result.add(str(request[field]).upper())
    return result


def _country_codes(request: dict[str, Any], nodes: list[dict[str, Any]]) -> set[str]:
    wanted = _node_ids(request)
    codes: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("node_id", "")).upper() not in wanted:
            continue
        code = str(node.get("country_code", "")).strip().upper()
        if code:
            codes.add(code)
    return codes


def _geographic_tags(codes: set[str]) -> set[str]:
    tags: set[str] = set()
    if codes & EUROPE:
        tags.update({"europe", "mediterranean", "global_where_available"})
    if codes & ASIA_PACIFIC:
        tags.update({"asia", "asia_pacific", "selected_asia", "selected_long_haul_routes"})
    if codes & NORTH_AMERICA:
        tags.update({"north_america", "united_states", "mexico"})
    if codes & CENTRAL_AMERICA_CARIBBEAN:
        tags.update({"central_america", "caribbean"})
    if codes & AFRICA:
        tags.update({"africa", "north_africa", "mediterranean"})
    return tags


def _regional_match(source: dict[str, Any], codes: set[str]) -> bool:
    regions = {str(value).lower() for value in source.get("regions", [])}
    if any("global" in region for region in regions):
        return True
    tags = _geographic_tags(codes)
    for region in regions:
        if any(tag in region or region in tag for tag in tags):
            return True
    return False


def _hints(request: dict[str, Any]) -> str:
    selected = {
        key: request.get(key)
        for key in (
            "carrier_hints",
            "operator_hints",
            "provider_hints",
            "target_operator",
            "target_carrier",
            "consumer_contexts",
        )
        if request.get(key) is not None
    }
    return json.dumps(selected, ensure_ascii=False).casefold()


def _operator_match(source: dict[str, Any], request: dict[str, Any]) -> bool:
    hints = _hints(request)
    if not hints:
        return False
    candidates = {
        str(source.get("source_id", "")).casefold(),
        str(source.get("name", "")).casefold(),
    }
    return any(candidate and candidate in hints for candidate in candidates)


def _configured_source(source_id: str, config: dict[str, Any]) -> tuple[bool, str | None]:
    live = config.get("live_provider_session", {})
    if not isinstance(live, dict):
        live = {}
    for field in ("configured_sources", "provider_endpoints", "structured_sources"):
        values = live.get(field, {})
        if isinstance(values, list) and source_id in values:
            return True, None
        if isinstance(values, dict) and values.get(source_id):
            configured = values[source_id]
            if isinstance(configured, str):
                return True, configured
            if isinstance(configured, dict):
                endpoint = configured.get("url") or configured.get("endpoint") or configured.get("feed_url")
                return True, str(endpoint) if endpoint else None
            return True, None
    return False, None


def _parent_key(request: dict[str, Any]) -> str:
    payload = {
        "request_type": request.get("request_type"),
        "origins": request.get("origins", []),
        "destinations": request.get("destinations", []),
        "from_node": request.get("from_node"),
        "to_node": request.get("to_node"),
        "earliest_departure": request.get("earliest_departure"),
        "latest_arrival": request.get("latest_arrival"),
        "suggested_modes": request.get("suggested_modes", request.get("allowed_modes", [])),
        "date_search": request.get("date_search"),
        "corridor_context": request.get("corridor_context"),
        "parent_hypothesis_id": request.get("parent_hypothesis_id"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return f"parent-{digest}"


def _hypothesis_context(request: dict[str, Any]) -> dict[str, Any]:
    corridor = request.get("corridor_context")
    if isinstance(corridor, dict) and corridor.get("corridor_id"):
        return {
            "hypothesis_id": str(corridor["corridor_id"]),
            "hypothesis_kind": str(
                corridor.get("corridor_kind", "temporal_corridor")
            ),
            "parent_hypothesis_id": request.get("parent_hypothesis_id"),
            "route_shape": corridor.get("route_shape"),
            "geographic_cell": corridor.get("geographic_cell"),
            "anchor_edge_ids": [
                *list(corridor.get("forward_anchor_edge_ids", [])),
                *list(corridor.get("backward_anchor_edge_ids", [])),
            ],
        }
    consumers = request.get("consumer_contexts", [])
    if not isinstance(consumers, list):
        consumers = []
    suffix_edges = [
        str(edge_id)
        for consumer in consumers
        if isinstance(consumer, dict)
        for edge_id in consumer.get("known_suffix_edge_ids", [])
    ]
    payload = {
        "request_type": request.get("request_type"),
        "origins": request.get("origins", []),
        "destinations": request.get("destinations", []),
        "from_node": request.get("from_node"),
        "to_node": request.get("to_node"),
        "suffix_terminal_edge": suffix_edges[-1] if suffix_edges else None,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "hypothesis_id": f"hypothesis-{digest}",
        "hypothesis_kind": str(request.get("request_type", "route_question")),
        "parent_hypothesis_id": request.get("parent_hypothesis_id"),
        "route_shape": None,
        "geographic_cell": None,
        "anchor_edge_ids": suffix_edges,
    }


def _disposition(
    source: dict[str, Any],
    source_policy: dict[str, Any],
    request: dict[str, Any],
    nodes: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[str, str, str | None]:
    family = _family(request)
    if family not in source_policy["task_families"]:
        return "not_applicable", f"task_family_{family}_not_supported", None
    if not (_source_modes(source) & _request_modes(request)):
        return "not_applicable", "transport_mode_not_supported", None
    activation = source_policy["activation"]
    codes = _country_codes(request, nodes)
    if activation == "global_modes":
        return "scheduled_candidate", "global_source_matches_task", None
    if activation == "regional_modes":
        if _regional_match(source, codes):
            return "scheduled_candidate", "source_region_overlaps_task", None
        return "not_applicable", "source_region_does_not_overlap_task", None
    if activation == "identified_operator":
        if _operator_match(source, request):
            return "scheduled_candidate", "operator_identified_in_request_context", None
        return "not_applicable", "operator_not_identified_for_this_task", None
    if activation == "regional_market_or_identified_operator":
        if _operator_match(source, request):
            return "scheduled_candidate", "operator_identified_in_request_context", None
        if family in {"market", "reverse"} and _regional_match(source, codes):
            return "scheduled_candidate", "regional_market_discovery_matches_task", None
        return "not_applicable", "regional_market_or_operator_trigger_not_met", None
    if activation == "configured_source":
        configured, endpoint = _configured_source(source["source_id"], config)
        if configured:
            return "scheduled_candidate", "configured_source_available", endpoint
        return "configuration_required", "deployment_endpoint_or_feed_not_configured", None
    raise ProviderExecutionError(f"unsupported activation: {activation}")


def _source_request(
    request: dict[str, Any],
    parent_key: str,
    source: dict[str, Any],
    source_policy: dict[str, Any],
    endpoint_override: str | None,
) -> dict[str, Any]:
    child = copy.deepcopy(request)
    source_id = source["source_id"]
    original_id = str(request.get("request_id") or parent_key)
    child["request_id"] = f"{original_id}::source::{source_id}"
    child["parent_request_id"] = original_id
    child["parent_request_key"] = parent_key
    child.update(_hypothesis_context(request))
    child["assigned_source"] = {
        "source_id": source_id,
        "name": source["name"],
        "url": endpoint_override or source["url"],
        "source_type": source["source_type"],
        "registry_priority": int(source.get("priority", 0)),
        "execution_kind": source_policy["execution_kind"],
        "admission_policy": source_policy["admission_policy"],
        "registry_last_verified": source.get("last_verified"),
        "capabilities": copy.deepcopy(source.get("capabilities", {})),
    }
    child["provider_kinds"] = [
        f"registered_source:{source_id}",
        source_policy["execution_kind"],
    ]
    return child


def schedule_provider_requests(
    base_requests: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    config: dict[str, Any],
    registry_path: str | Path = DEFAULT_REGISTRY,
    policy_path: str | Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    if not isinstance(base_requests, list):
        raise ProviderExecutionError("base_requests must be an array")
    if not isinstance(nodes, list):
        raise ProviderExecutionError("nodes must be an array")
    if not isinstance(config, dict):
        raise ProviderExecutionError("config must be an object")
    registry, policy = _catalog(registry_path, policy_path)
    live_config = config.get("live_provider_session", {})
    if not isinstance(live_config, dict):
        live_config = {}
    source_selection_mode = str(
        live_config.get("source_selection_mode", "ai_led")
    ).strip().lower()
    if source_selection_mode != "ai_led":
        raise ProviderExecutionError("source_selection_mode must be ai_led")
    source_map = {item["source_id"]: item for item in registry["sources"]}
    policy_map = {item["source_id"]: item for item in policy["source_policies"]}
    per_parent: list[list[dict[str, Any]]] = []
    coverage: list[dict[str, Any]] = []
    for base_index, base in enumerate(base_requests):
        if not isinstance(base, dict):
            raise ProviderExecutionError(f"base_requests[{base_index}] must be an object")
        family = _family(base)
        parent_key = _parent_key(base)
        applicable: list[dict[str, Any]] = []
        for source in registry["sources"]:
            source_policy = policy_map[source["source_id"]]
            disposition, reason, endpoint = _disposition(
                source, source_policy, base, nodes, config
            )
            entry = {
                "parent_request_key": parent_key,
                "parent_request_id": base.get("request_id"),
                "task_family": family,
                "source_id": source["source_id"],
                "source_name": source["name"],
                "source_type": source["source_type"],
                "execution_kind": source_policy["execution_kind"],
                "admission_policy": source_policy["admission_policy"],
                "disposition": disposition,
                "reason": reason,
            }
            if disposition == "scheduled_candidate":
                child = _source_request(base, parent_key, source, source_policy, endpoint)
                entry["provider_request_id"] = child["request_id"]
                applicable.append(child)
            coverage.append(entry)
        applicable.sort(
            key=lambda item: (
                -int(item["assigned_source"]["registry_priority"]),
                item["assigned_source"]["source_id"],
            )
        )
        ai_choice = copy.deepcopy(base)
        original_id = str(base.get("request_id") or parent_key)
        ai_choice["request_id"] = f"{original_id}::source::ai-choice"
        ai_choice["parent_request_id"] = original_id
        ai_choice["parent_request_key"] = parent_key
        ai_choice.update(_hypothesis_context(base))
        ai_choice["provider_kinds"] = ["ai_selected_source"]
        ai_choice["source_suggestions"] = [
            copy.deepcopy(item["assigned_source"]) for item in applicable
        ]
        existing_advice = ai_choice.get("selection_advice")
        ai_choice["selection_advice"] = {
            **(
                copy.deepcopy(existing_advice)
                if isinstance(existing_advice, dict)
                else {}
            ),
            "script_recommended": bool(
                isinstance(existing_advice, dict)
                and existing_advice.get("script_recommended")
            ),
            "reason": (
                existing_advice.get("reason")
                if isinstance(existing_advice, dict)
                and existing_advice.get("reason")
                else "open_source_choice_for_ai"
            ),
            "binding": False,
            "source_suggestion_count": len(applicable),
        }
        per_parent.append([ai_choice])
        for rule, rule_policy in zip(
            registry["dynamic_source_rules"], policy["dynamic_rule_policies"]
        ):
            coverage.append(
                {
                    "parent_request_key": parent_key,
                    "parent_request_id": base.get("request_id"),
                    "task_family": family,
                    "source_id": rule["source_id"],
                    "source_name": rule["source_id"],
                    "source_type": "dynamic_rule",
                    "execution_kind": rule_policy["execution_kind"],
                    "admission_policy": rule_policy["admission_policy"],
                    "disposition": "awaiting_trigger",
                    "reason": f"{rule_policy['activation']}_not_yet_observed",
                }
            )
    # Interleave source rank across parent questions so one high-priority parent
    # cannot consume the entire pending publication queue.
    scheduled: list[dict[str, Any]] = []
    rank = 0
    while any(rank < len(items) for items in per_parent):
        for items in per_parent:
            if rank < len(items):
                scheduled.append(items[rank])
        rank += 1
    counts: dict[str, int] = {}
    for entry in coverage:
        disposition = entry["disposition"]
        counts[disposition] = counts.get(disposition, 0) + 1
    return {
        "scheduler_version": SCHEDULER_VERSION,
        "registry_schema_version": registry.get("registry_schema_version"),
        "registry_last_verified": registry.get("last_verified"),
        "registry_is_whitelist": bool(not registry.get("not_a_whitelist", True)),
        "static_source_count": len(registry["sources"]),
        "dynamic_rule_count": len(registry["dynamic_source_rules"]),
        "parent_request_count": len(base_requests),
        "source_selection_mode": source_selection_mode,
        "scheduled_request_count": len(scheduled),
        "disposition_counts": counts,
        "requests": scheduled,
        "coverage": coverage,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expand engine Provider questions into source-specific tasks"
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = _read_json(args.bundle)
        if not isinstance(bundle, dict):
            raise ProviderExecutionError("bundle must be an object")
        result = schedule_provider_requests(
            bundle.get("base_requests", []),
            bundle.get("nodes", []),
            bundle.get("config", {}),
        )
        if args.output:
            _write_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ProviderExecutionError as exc:
        print(f"provider_execution_scheduler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
