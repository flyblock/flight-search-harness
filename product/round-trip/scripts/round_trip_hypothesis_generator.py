"""Generate evidence-backed round-trip route-hypothesis suggestions lazily."""

from __future__ import annotations

import argparse
import copy
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


def _money_identity(value: Any, field: str) -> tuple[float, str | None]:
    if isinstance(value, dict):
        amount = _number(value.get("amount"))
        currency = value.get("currency")
        if amount is None or not isinstance(currency, str) or len(currency.strip()) != 3:
            raise HypothesisGeneratorError(f"{field} must be a valid money object")
        return amount, currency.strip().upper()
    amount = _number(value)
    if amount is None:
        raise HypothesisGeneratorError(
            f"{field} must be a non-negative number or money object"
        )
    return amount, None


def _normalized_gateway_signal(
    raw: dict[str, Any], field: str, candidate_node_id: str, side: str
) -> dict[str, Any]:
    """Read canonical signals and the pre-contract aliases from persisted Runs."""

    result = copy.deepcopy(raw)
    kind = raw.get("kind")
    alias_kind = raw.get("signal_kind")
    if kind is not None and alias_kind is not None and kind != alias_kind:
        raise HypothesisGeneratorError(f"{field}.kind conflicts with signal_kind")
    result["kind"] = _text(
        kind if kind is not None else alias_kind, f"{field}.kind"
    )
    result.pop("signal_kind", None)

    fare = raw.get("dated_fare_amount")
    alias_fare = raw.get("lowest_displayed_total")
    if fare is not None and alias_fare is not None:
        if _money_identity(fare, f"{field}.dated_fare_amount") != _money_identity(
            alias_fare, f"{field}.lowest_displayed_total"
        ):
            raise HypothesisGeneratorError(
                f"{field}.dated_fare_amount conflicts with lowest_displayed_total"
            )
    selected_fare = fare if fare is not None else alias_fare
    if selected_fare is not None:
        _money_identity(selected_fare, f"{field}.dated_fare_amount")
        result["dated_fare_amount"] = copy.deepcopy(selected_fare)
        date_coverage = raw.get("date_coverage", "sampled_date_pair")
        if date_coverage not in {
            "sampled_date_pair",
            "exact_dates",
            "bounded_window",
        }:
            raise HypothesisGeneratorError(
                f"{field}.date_coverage is unsupported"
            )
        result["date_coverage"] = date_coverage
    elif raw.get("date_coverage") is not None:
        raise HypothesisGeneratorError(
            f"{field}.date_coverage requires dated_fare_amount"
        )
    result.pop("lowest_displayed_total", None)

    legacy_opposite_field = (
        "destination_gateway_node_id"
        if side == "origin"
        else "origin_gateway_node_id"
    )
    legacy_candidate_field = (
        "origin_gateway_node_id"
        if side == "origin"
        else "destination_gateway_node_id"
    )
    opposite_value = raw.get("opposite_gateway_node_id")
    legacy_opposite_value = raw.get(legacy_opposite_field)
    if opposite_value is not None and legacy_opposite_value is not None:
        opposite = _text(opposite_value, f"{field}.opposite_gateway_node_id")
        legacy_opposite = _text(
            legacy_opposite_value, f"{field}.{legacy_opposite_field}"
        )
        if opposite != legacy_opposite:
            raise HypothesisGeneratorError(
                f"{field}.opposite_gateway_node_id conflicts with {legacy_opposite_field}"
            )
    else:
        opposite = opposite_value if opposite_value is not None else legacy_opposite_value
    legacy_candidate_value = raw.get(legacy_candidate_field)
    if (
        legacy_candidate_value is not None
        and _text(legacy_candidate_value, f"{field}.{legacy_candidate_field}")
        != candidate_node_id
    ):
        raise HypothesisGeneratorError(
            f"{field}.{legacy_candidate_field} conflicts with the candidate node"
        )
    if selected_fare is not None or raw.get("complete_cost_lower_bound") is not None:
        opposite = _text(
            opposite,
            f"{field}.opposite_gateway_node_id",
        )
        if opposite == candidate_node_id:
            raise HypothesisGeneratorError(
                f"{field}.opposite_gateway_node_id must identify the other gateway"
            )
        result["opposite_gateway_node_id"] = opposite
    elif opposite is not None:
        result["opposite_gateway_node_id"] = _text(
            opposite, f"{field}.opposite_gateway_node_id"
        )
    result.pop("origin_gateway_node_id", None)
    result.pop("destination_gateway_node_id", None)
    return result


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    signal = candidate.get("signal", {}) if isinstance(candidate.get("signal"), dict) else {}
    fare = _money_amount(signal, "dated_fare_amount")
    access_cost = _money_amount(signal, "access_cost")
    duration = _number(signal.get("duration_minutes"))
    kind = str(signal.get("kind", ""))
    strength = 0 if any(token in kind for token in ("fare", "price", "access_and_fare")) else 1
    coverage_rank = {
        "bounded_window": 0,
        "exact_dates": 1,
        "sampled_date_pair": 2,
    }.get(signal.get("date_coverage"), 3)
    return (
        strength,
        coverage_rank,
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
        signal = value.get("signal")
        if not isinstance(signal, dict):
            raise HypothesisGeneratorError(
                f"gateway_candidates.{side}[{index}].signal must be an object"
            )
        value["signal"] = _normalized_gateway_signal(
            signal,
            f"gateway_candidates.{side}[{index}].signal",
            node_id,
            side,
        )
        current = by_node.get(node_id)
        if current is None or _candidate_sort_key(value) < _candidate_sort_key(current):
            by_node[node_id] = value
    result = list(by_node.values())
    result.sort(key=_candidate_sort_key)
    return result


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
            "complete_cost_lower_bound": None,
            "observed_complete_cost_signal": None,
            "evidence_backed": True,
        },
        "research_sequence": [
            "joint_2d_surface_then_ai_selected_exact_atomic_quote",
            "four_directional_surface_access_egress",
        ],
        "temporal_strategy": "ai_led_date_frontier",
        "joint_baseline_reason": "Observe the atomic round-trip baseline first; verify separate one-ways only when current Evidence indicates a credible improvement.",
    }


def _money_in_base_currency(
    state: dict[str, Any], money: dict[str, Any]
) -> float | None:
    amount = money.get("amount")
    currency = money.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    base = state.get("scenario", {}).get("base_currency")
    if currency == base:
        return round(float(amount), 2)
    rate = (
        state.get("scenario", {})
        .get("fx_snapshot", {})
        .get("rates", {})
        .get(currency)
    )
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return round(float(amount) * float(rate), 2)


def _screen_pair_specs(
    state: dict[str, Any],
    origins: list[dict[str, Any]],
    destinations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    origin_by_id = {item["node"]["node_id"]: item for item in origins}
    destination_by_id = {item["node"]["node_id"]: item for item in destinations}
    screen_task_ids = {
        str(task.get("task_id"))
        for task in state.get("research_tasks", [])
        if task.get("research_kind") == "region_pool_fare_screen"
    }
    result = []
    for signal in state.get("temporal_signals", []):
        if (
            signal.get("research_task_id") not in screen_task_ids
            or signal.get("signal_kind") != "joint_date_pair"
        ):
            continue
        outbound_origin_id = signal.get(
            "outbound_origin_gateway_node_id",
            signal.get("origin_gateway_node_id"),
        )
        return_destination_id = signal.get(
            "return_destination_gateway_node_id",
            signal.get("origin_gateway_node_id"),
        )
        outbound_destination_id = signal.get(
            "outbound_destination_gateway_node_id",
            signal.get("destination_gateway_node_id"),
        )
        return_origin_id = signal.get(
            "return_origin_gateway_node_id",
            signal.get("destination_gateway_node_id"),
        )
        origin_ids = list(dict.fromkeys([outbound_origin_id, return_destination_id]))
        destination_ids = list(
            dict.fromkeys([outbound_destination_id, return_origin_id])
        )
        origin_candidates = [origin_by_id.get(node_id) for node_id in origin_ids]
        destination_candidates = [
            destination_by_id.get(node_id) for node_id in destination_ids
        ]
        normalized_total = _money_in_base_currency(
            state, signal.get("atomic_total", {})
        )
        if (
            any(item is None for item in [*origin_candidates, *destination_candidates])
            or normalized_total is None
        ):
            continue
        spec = _pair_spec(origin_candidates[0], destination_candidates[0])
        spec["origin_gateway_nodes"] = origin_ids
        spec["destination_gateway_nodes"] = destination_ids
        spec["route_shape"] = (
            "regional_open_jaw"
            if len(origin_ids) > 1 or len(destination_ids) > 1
            else "closed_airport_pair"
        )
        outbound_date = str(signal.get("outbound_date"))
        return_date = str(signal.get("return_date"))
        endpoint_token = "-".join(
            _safe_token(str(item))
            for item in [*origin_ids, *destination_ids]
        )
        spec["hypothesis_id"] = (
            f"hypothesis-screen-{endpoint_token}-"
            f"{outbound_date.replace('-', '')}-{return_date.replace('-', '')}"
        )
        spec["advisory_basis"] = {
            **spec["advisory_basis"],
            "origin_candidate_ids": [
                item["candidate_id"] for item in origin_candidates
            ],
            "destination_candidate_ids": [
                item["candidate_id"] for item in destination_candidates
            ],
            "screen_signal_id": signal.get("signal_id"),
            "observed_complete_cost_signal": normalized_total,
            "screen_atomic_total": copy.deepcopy(signal.get("atomic_total")),
            "screen_total_in_base_currency": normalized_total,
            "evidence_backed": True,
        }
        spec["screen_candidate"] = {
            "signal_id": signal.get("signal_id"),
            "outbound_origin_gateway_node_id": outbound_origin_id,
            "outbound_destination_gateway_node_id": outbound_destination_id,
            "return_origin_gateway_node_id": return_origin_id,
            "return_destination_gateway_node_id": return_destination_id,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "atomic_total": copy.deepcopy(signal.get("atomic_total")),
            "total_in_base_currency": normalized_total,
        }
        result.append(spec)
    by_hypothesis: dict[str, dict[str, Any]] = {}
    for item in result:
        hypothesis_id = item["hypothesis_id"]
        current = by_hypothesis.get(hypothesis_id)
        if (
            current is None
            or item["screen_candidate"]["total_in_base_currency"]
            < current["screen_candidate"]["total_in_base_currency"]
        ):
            by_hypothesis[hypothesis_id] = item
    result = list(by_hypothesis.values())
    result.sort(
        key=lambda item: (
            item["screen_candidate"]["total_in_base_currency"],
            item["screen_candidate"]["outbound_date"],
            item["screen_candidate"]["return_date"],
            item["hypothesis_id"],
        )
    )
    return result


def _lazy_pair_specs(origins: list[dict[str, Any]], destinations: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not origins or not destinations or limit <= 0:
        return []
    pairs = [
        (origin, destination)
        for origin in origins
        for destination in destinations
    ]
    pairs.sort(
        key=lambda pair: (
            str(pair[0].get("candidate_id", "")),
            str(pair[1].get("candidate_id", "")),
        )
    )
    return [_pair_spec(*pair) for pair in pairs[:limit]]


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


def generate_suggestions(raw_state: Any, *, max_closed_pairs: int = 8, include_open_jaw: bool = False) -> dict[str, Any]:
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
    screen_pairs = _screen_pair_specs(raw_state, origins, destinations)
    available_closed = [
        item
        for item in (
            screen_pairs
            if screen_pairs
            else _lazy_pair_specs(
                origins, destinations, len(origins) * len(destinations)
            )
        )
        if item["hypothesis_id"] not in existing_ids
    ]
    incumbent = (
        (raw_state.get("result", {}).get("best_portfolio") or {})
        .get("total_mandatory_cost", {})
        .get("amount")
    )
    if isinstance(incumbent, (int, float)) and not isinstance(incumbent, bool):
        available_closed = [
            item
            for item in available_closed
            if item.get("screen_candidate") is None
            or item["screen_candidate"]["total_in_base_currency"] < float(incumbent)
        ]
    closed = available_closed[:max_closed_pairs]
    open_jaw = _open_jaw_spec(origins, destinations) if include_open_jaw else None
    if open_jaw is not None and open_jaw["hypothesis_id"] in existing_ids:
        open_jaw = None
    advanced = [
        item
        for item in _advanced_specs(origins, destinations)
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
        "selection_instruction": "Verify the lowest remaining region-pool pair/date signal first. Continue while an unverified screen total can still beat the incumbent complete mandatory total.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output")
    parser.add_argument("--max-closed-pairs", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        result = generate_suggestions(
            read_json(args.state),
            max_closed_pairs=args.max_closed_pairs,
            include_open_jaw=False,
        )
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
