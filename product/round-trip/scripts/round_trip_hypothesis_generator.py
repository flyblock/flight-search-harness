"""Generate evidence-backed round-trip route-hypothesis suggestions lazily."""

from __future__ import annotations

import argparse
import copy
import heapq
import json
import re
from pathlib import Path
from typing import Any


VERSION = "0.5.0"
TERMINAL_GATEWAY_STATUSES = {"complete", "hard_dead", "deferred_low_value"}


class HypothesisGeneratorError(ValueError):
    pass


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HypothesisGeneratorError(f"{field} must be a non-empty string")
    return value.strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _money_amount(signal: dict[str, Any], field: str) -> float | None:
    value = signal.get(field)
    if isinstance(value, dict):
        return _number(value.get("amount"))
    return _number(value)


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    signal = candidate.get("signal", {}) if isinstance(candidate.get("signal"), dict) else {}
    complete_lb = _money_amount(signal, "complete_cost_lower_bound")
    fare = _money_amount(signal, "dated_fare_amount")
    access_cost = _money_amount(signal, "access_cost")
    duration = _number(signal.get("duration_minutes"))
    kind = str(signal.get("kind", ""))
    strength = 0 if any(token in kind for token in ("fare", "price", "access_and_fare")) else 1
    return (
        strength,
        complete_lb is None,
        complete_lb if complete_lb is not None else float("inf"),
        fare is None,
        fare if fare is not None else float("inf"),
        access_cost is None,
        access_cost if access_cost is not None else float("inf"),
        duration is None,
        duration if duration is not None else float("inf"),
        str(candidate.get("candidate_id", "")),
    )


def _normalized_candidates(state: dict[str, Any], side: str) -> list[dict[str, Any]]:
    raw = state.get("gateway_candidates", {}).get(side, [])
    if not isinstance(raw, list):
        raise HypothesisGeneratorError(f"gateway_candidates.{side} must be an array")
    by_node: dict[str, dict[str, Any]] = {}
    for index, candidate in enumerate(raw):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("node"), dict):
            raise HypothesisGeneratorError(f"gateway_candidates.{side}[{index}] is invalid")
        if candidate.get("evidence_status") == "ai_proposed":
            continue
        node_id = _text(candidate["node"].get("node_id"), f"gateway_candidates.{side}[{index}].node.node_id")
        value = copy.deepcopy(candidate)
        current = by_node.get(node_id)
        if current is None or _candidate_sort_key(value) < _candidate_sort_key(current):
            by_node[node_id] = value
    result = list(by_node.values())
    result.sort(key=_candidate_sort_key)
    return result


def _pair_lower_bound(origin: dict[str, Any], destination: dict[str, Any]) -> float | None:
    amounts = []
    for candidate in (origin, destination):
        signal = candidate.get("signal", {}) if isinstance(candidate.get("signal"), dict) else {}
        value = _money_amount(signal, "complete_cost_lower_bound")
        if value is None:
            value = _money_amount(signal, "dated_fare_amount")
        if value is not None:
            amounts.append(value)
    return round(sum(amounts), 2) if amounts else None


def _safe_token(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "gateway"


def _pair_spec(origin: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    origin_node = origin["node"]
    destination_node = destination["node"]
    origin_id = origin_node["node_id"]
    destination_id = destination_node["node_id"]
    return {
        "hypothesis_id": f"hypothesis-auto-{_safe_token(origin_id)}-{_safe_token(destination_id)}",
        "route_shape": "closed_airport_pair",
        "origin_gateway_nodes": [origin_id],
        "destination_gateway_nodes": [destination_id],
        "geographic_cell": f"{origin_node.get('country_code', 'unknown')}-{destination_node.get('country_code', 'unknown')}",
        "advisory_basis": {
            "origin_candidate_id": origin["candidate_id"],
            "destination_candidate_id": destination["candidate_id"],
            "complete_cost_lower_bound": _pair_lower_bound(origin, destination),
            "evidence_backed": True,
        },
        "research_sequence": [
            "joint_2d_surface_then_ai_selected_exact_atomic_quote",
            "directional_surfaces_then_ai_selected_compatible_split_verification",
            "four_directional_surface_access_egress",
        ],
        "temporal_strategy": "ai_led_date_frontier",
        "joint_baseline_reason": "A single atomic round-trip ticket often prices below two separately purchased one-ways; observe it directly before comparing split totals.",
    }


def _lazy_pair_specs(origins: list[dict[str, Any]], destinations: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not origins or not destinations or limit <= 0:
        return []
    heap: list[tuple[int, int, int]] = [(0, 0, 0)]
    visited = {(0, 0)}
    result = []
    while heap and len(result) < limit:
        _, origin_index, destination_index = heapq.heappop(heap)
        result.append(_pair_spec(origins[origin_index], destinations[destination_index]))
        for next_origin, next_destination in ((origin_index + 1, destination_index), (origin_index, destination_index + 1)):
            if next_origin >= len(origins) or next_destination >= len(destinations) or (next_origin, next_destination) in visited:
                continue
            visited.add((next_origin, next_destination))
            heapq.heappush(heap, (next_origin + next_destination, next_origin, next_destination))
    return result


def _open_jaw_spec(origins: list[dict[str, Any]], destinations: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(origins) < 2 and len(destinations) < 2:
        return None
    selected_origins = origins[:2] if len(origins) >= 2 else origins[:1]
    selected_destinations = destinations[:2] if len(destinations) >= 2 else destinations[:1]
    origin_ids = [item["node"]["node_id"] for item in selected_origins]
    destination_ids = [item["node"]["node_id"] for item in selected_destinations]
    return {
        "hypothesis_id": f"hypothesis-auto-open-jaw-{'-'.join(_safe_token(item) for item in origin_ids)}-{'-'.join(_safe_token(item) for item in destination_ids)}",
        "route_shape": "regional_open_jaw",
        "origin_gateway_nodes": origin_ids,
        "destination_gateway_nodes": destination_ids,
        "geographic_cell": "regional-open-jaw",
        "advisory_basis": {
            "origin_candidate_ids": [item["candidate_id"] for item in selected_origins],
            "destination_candidate_ids": [item["candidate_id"] for item in selected_destinations],
            "evidence_backed": True,
            "reason": "materially different admitted gateways permit different outbound and return airports",
        },
        "research_sequence": ["joint_2d_surface_then_ai_selected_exact_atomic_quote", "directional_surfaces_then_ai_selected_compatible_split_verification", "surface_access_egress"],
        "temporal_strategy": "ai_led_date_frontier",
        "joint_baseline_reason": "Search an atomic open-jaw or multi-city quote before assuming separately purchased directions are cheaper.",
    }


def _advanced_specs(
    origins: list[dict[str, Any]], destinations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compile only explicit evidence-carried portfolio anomalies into advice."""

    admitted = {
        item["node"]["node_id"] for item in [*origins, *destinations]
    }
    result = []
    seen = set()
    for candidate in [*origins, *destinations]:
        signal = candidate.get("signal", {})
        hint = signal.get("portfolio_challenger") if isinstance(signal, dict) else None
        if not isinstance(hint, dict):
            continue
        route_shape = hint.get("route_shape")
        if route_shape not in {"nested_round_trips", "mixed_atomic_portfolio"}:
            continue
        signal_id = _text(hint.get("signal_id"), "portfolio_challenger.signal_id")
        origin_ids = list(hint.get("origin_gateway_nodes", []))
        destination_ids = list(hint.get("destination_gateway_nodes", []))
        task_specs = copy.deepcopy(hint.get("task_specs"))
        if (
            not origin_ids
            or not destination_ids
            or not set(origin_ids + destination_ids) <= admitted
            or not isinstance(task_specs, list)
            or not task_specs
        ):
            raise HypothesisGeneratorError(
                f"portfolio challenger {signal_id} requires admitted gateways and task_specs"
            )
        hypothesis_id = f"hypothesis-signal-{_safe_token(signal_id)}"
        if hypothesis_id in seen:
            continue
        seen.add(hypothesis_id)
        result.append(
            {
                "hypothesis_id": hypothesis_id,
                "route_shape": route_shape,
                "origin_gateway_nodes": origin_ids,
                "destination_gateway_nodes": destination_ids,
                "geographic_cell": str(hint.get("geographic_cell") or "evidence-triggered-portfolio"),
                "advisory_basis": {
                    "evidence_backed": True,
                    "trigger_candidate_id": candidate["candidate_id"],
                    "signal_id": signal_id,
                    "reason": _text(hint.get("reason"), "portfolio_challenger.reason"),
                    "binding": False,
                },
                "research_sequence": list(
                    hint.get("research_sequence", ["verify_each_atomic_offer", "solve_complete_portfolio"])
                ),
                "temporal_strategy": "ai_led_date_frontier",
                "joint_baseline_reason": "This advanced challenger exists only because current evidence exposed a concrete structural pricing anomaly.",
                "task_specs": task_specs,
            }
        )
    return result


def _ai_structural_specs(state: dict[str, Any]) -> list[dict[str, Any]]:
    exploration = state.get("structural_exploration", {})
    dispositions = exploration.get("dispositions", {})
    result = []
    for proposal in exploration.get("proposals", []):
        proposal_id = str(proposal.get("proposal_id") or "")
        if dispositions.get(proposal_id) == "dismissed":
            continue
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
                    "proposal_id": proposal_id,
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


def generate_suggestions(raw_state: Any, *, max_closed_pairs: int = 8, include_open_jaw: bool = True) -> dict[str, Any]:
    if not isinstance(raw_state, dict) or raw_state.get("trip_scope") != "round_trip":
        raise HypothesisGeneratorError("state must be a round_trip Session")
    if max_closed_pairs < 1:
        raise HypothesisGeneratorError("max_closed_pairs must be positive")
    tree_nodes = raw_state.get("hypothesis_tree", {}).get("nodes", {})
    gateway_blockers = [
        hypothesis_id
        for hypothesis_id in ("hypothesis-gateway-origin", "hypothesis-gateway-destination")
        if tree_nodes.get(hypothesis_id, {}).get("status") not in TERMINAL_GATEWAY_STATUSES
    ]
    origins = _normalized_candidates(raw_state, "origin")
    destinations = _normalized_candidates(raw_state, "destination")
    if gateway_blockers:
        return {
            "generator_version": VERSION,
            "decision_owner": "ai",
            "candidate_pool_exhaustion_required": False,
            "world_airport_enumeration": False,
            "origin_gateway_count": len(origins),
            "destination_gateway_count": len(destinations),
            "published_suggestion_count": 0,
            "gateway_blockers": gateway_blockers,
            "suggestions": [],
            "selection_instruction": "Finish or explicitly dispose both AI-scoped gateway-discovery hypotheses before locking a route hypothesis.",
        }
    existing_ids = set(raw_state.get("hypothesis_tree", {}).get("nodes", {}))
    closed = [item for item in _lazy_pair_specs(origins, destinations, max_closed_pairs) if item["hypothesis_id"] not in existing_ids]
    open_jaw = _open_jaw_spec(origins, destinations) if include_open_jaw else None
    if open_jaw is not None and open_jaw["hypothesis_id"] in existing_ids:
        open_jaw = None
    advanced = [
        item
        for item in [
            *_advanced_specs(origins, destinations),
            *_ai_structural_specs(raw_state),
        ]
        if item["hypothesis_id"] not in existing_ids
    ]
    suggestions = [*closed, *([open_jaw] if open_jaw else []), *advanced]
    return {
        "generator_version": VERSION,
        "decision_owner": "ai",
        "candidate_pool_exhaustion_required": False,
        "world_airport_enumeration": False,
        "origin_gateway_count": len(origins),
        "destination_gateway_count": len(destinations),
        "published_suggestion_count": len(suggestions),
        "gateway_blockers": [],
        "suggestions": suggestions,
        "selection_instruction": "Choose observed-gateway suggestions or explicitly AI-proposed structural corridors worth verifying. AI proposals are not Evidence. Joint atomic pricing is the first baseline inside each ordinary gateway hypothesis, while every selected custom task remains evidence-gated.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output")
    parser.add_argument("--max-closed-pairs", type=int, default=8)
    parser.add_argument("--no-open-jaw", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = generate_suggestions(read_json(args.state), max_closed_pairs=args.max_closed_pairs, include_open_jaw=not args.no_open_jaw)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (OSError, json.JSONDecodeError, HypothesisGeneratorError) as exc:
        print(f"round_trip_hypothesis_generator: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
