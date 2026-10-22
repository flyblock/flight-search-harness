"""Generate evidence-backed round-trip route-hypothesis suggestions lazily."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flight_search_harness.price_freshness import offer_price_deadline


VERSION = "0.5.0"
TERMINAL_GATEWAY_STATUSES = {"complete", "hard_dead", "deferred_low_value"}
FRONTIER_SCREEN_RESEARCH_KIND = "portfolio_frontier_screen"
PRICE_CHALLENGE_SCREEN_RESEARCH_KIND = "price_challenge_screen"
DATE_COVERAGE_VALUES = {
    "sampled_date_pair",
    "exact_dates",
    "bounded_window",
}
ENDPOINT_SURFACE_ROLES = {
    "origin-access-outbound",
    "origin-egress-return",
    "destination-access-return",
    "destination-egress-outbound",
}


class HypothesisGeneratorError(ValueError):
    pass


def _load_identity():
    path = Path(__file__).with_name("round_trip_identity.py")
    spec = importlib.util.spec_from_file_location("round_trip_identity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


IDENTITY = _load_identity()


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
        if not isinstance(date_coverage, str) or date_coverage not in DATE_COVERAGE_VALUES:
            raise HypothesisGeneratorError(
                f"{field}.date_coverage must be one of {sorted(DATE_COVERAGE_VALUES)}"
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
        if candidate.get("candidate_scope") in {
            "portfolio_frontier",
            "price_challenge",
        }:
            continue
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
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(float(rate))
        or float(rate) <= 0
    ):
        return None
    return round(float(amount) * float(rate), 2)


def _signal_itinerary_dates(signal: dict[str, Any]) -> tuple[str, str, str]:
    if signal.get("temporal_signal_version") == "2.0":
        dates = signal.get("itinerary_dates")
        if not isinstance(dates, dict):
            raise HypothesisGeneratorError(
                "Temporal Signal v2 requires itinerary_dates"
            )
        fields = (
            "outbound_departure_local_date",
            "outbound_destination_arrival_local_date",
            "return_departure_local_date",
        )
        values = tuple(str(dates.get(field) or "") for field in fields)
        if any(not value for value in values):
            raise HypothesisGeneratorError(
                "Temporal Signal v2 requires all itinerary dates"
            )
        return values
    outbound = str(signal.get("outbound_date") or "")
    arrival = str(signal.get("outbound_destination_arrival_date") or outbound)
    returning = str(signal.get("return_date") or "")
    return outbound, arrival, returning


def _screen_pair_specs(
    state: dict[str, Any],
    origins: list[dict[str, Any]],
    destinations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    origin_by_id = {item["node"]["node_id"]: item for item in origins}
    destination_by_id = {item["node"]["node_id"]: item for item in destinations}
    screen_tasks = {
        str(task.get("task_id")): task
        for task in state.get("research_tasks", [])
        if task.get("research_kind") == "region_pool_fare_screen"
    }
    screen_task_ids = set(screen_tasks)
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
        outbound_date, destination_arrival_date, return_date = (
            _signal_itinerary_dates(signal)
        )
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
            "temporal_signal_version": "2.0",
            "itinerary_dates": {
                "outbound_departure_local_date": outbound_date,
                "outbound_destination_arrival_local_date": destination_arrival_date,
                "return_departure_local_date": return_date,
            },
            "atomic_total": copy.deepcopy(signal.get("atomic_total")),
            "total_in_base_currency": normalized_total,
            "minimum_savings_ratio": float(
                screen_tasks[str(signal.get("research_task_id"))]
                .get("query_shape", {})
                .get("minimum_savings_ratio", 0.0)
            ),
            "travel_window_id": screen_tasks[
                str(signal.get("research_task_id"))
            ]
            .get("query_shape", {})
            .get("travel_window_id"),
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
            item["screen_candidate"]["itinerary_dates"][
                "outbound_departure_local_date"
            ],
            item["screen_candidate"]["itinerary_dates"][
                "return_departure_local_date"
            ],
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
    state: dict[str, Any],
    origins: list[dict[str, Any]],
    destinations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile only explicit evidence-carried portfolio anomalies into advice."""

    admitted = {
        item["node"]["node_id"] for item in [*origins, *destinations]
    }
    result = []
    seen = set()
    frontier_candidates = [
        copy.deepcopy(candidate)
        for side in ("origin", "destination")
        for candidate in state.get("gateway_candidates", {}).get(side, [])
        if isinstance(candidate, dict)
        and candidate.get("candidate_scope") in {
            "portfolio_frontier",
            "price_challenge",
        }
    ]
    for candidate in [*origins, *destinations, *frontier_candidates]:
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
                    "optimistic_mandatory_air_ticket_total": hint.get(
                        "optimistic_mandatory_air_ticket_total",
                        hint.get("complete_cost_lower_bound"),
                    ),
                    "frontier_improvement_ratio": hint.get(
                        "frontier_improvement_ratio"
                    ),
                    "requires_all_payable_tasks": bool(
                        hint.get("requires_all_payable_tasks", False)
                    ),
                    "frontier_positioning": copy.deepcopy(
                        hint.get("frontier_positioning")
                    ),
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


def _price_challenge_screen_spec(
    state: dict[str, Any],
    origins: list[dict[str, Any]],
    destinations: list[dict[str, Any]],
    incumbent: float,
) -> dict[str, Any] | None:
    intent = state.get("scenario", {}).get("intent", {})
    origin_ids = [str(item["node"]["node_id"]) for item in origins]
    destination_ids = [str(item["node"]["node_id"]) for item in destinations]
    if not origin_ids or not destination_ids:
        return None
    prior_tasks = [
        task
        for task in state.get("research_tasks", [])
        if task.get("research_kind") in {
            PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
            FRONTIER_SCREEN_RESEARCH_KIND,
        }
    ]
    if prior_tasks and prior_tasks[-1].get("requirement_status") == "open":
        return None
    prior_candidates = [
        candidate
        for side in state.get("gateway_candidates", {}).values()
        for candidate in side
        if isinstance(candidate, dict)
        and candidate.get("candidate_scope") in {
            "portfolio_frontier",
            "price_challenge",
        }
    ]
    if prior_tasks:
        last_task_id = str(prior_tasks[-1].get("task_id") or "")
        last_task_candidates = [
            candidate
            for candidate in prior_candidates
            if str(candidate.get("research_task_id") or "") == last_task_id
        ]
        if not last_task_candidates:
            return None
    if prior_candidates:
        last_signal = prior_candidates[-1].get("signal", {})
        if last_signal.get(
            "price_challenge_disposition",
            last_signal.get("frontier_disposition"),
        ) != "competitive":
            return None
    research_credits = state.get("research_credits", {})
    if int(research_credits.get("remaining", 0)) < 5:
        return None
    challenge_round = len(prior_tasks) + 1
    threshold = float(
        research_credits.get("price_challenge_threshold", 0.05)
    )
    excluded_challenge_node_ids = sorted(
        {
            str(candidate.get("node", {}).get("node_id"))
            for candidate in prior_candidates
            if candidate.get("node", {}).get("node_id")
        }
    )
    return {
        "hypothesis_id": f"hypothesis-price-challenge-screen-g{challenge_round}",
        "route_shape": "custom_complete_journey",
        "origin_gateway_nodes": origin_ids,
        "destination_gateway_nodes": destination_ids,
        "geographic_cell": "price-challenge-frontier",
        "advisory_basis": {
            "evidence_backed": False,
            "price_challenge_probe": True,
            "reason": (
                "Search the next unexamined airfare partition only while its "
                "optimistic Mandatory Air Ticket Total can materially improve the winner."
            ),
            "binding": True,
        },
        "research_sequence": [
            "provider_ranked_price_challenge",
            "promote_only_threshold_competitive_portfolios",
        ],
        "temporal_strategy": "provider_ranked_window",
        "joint_baseline_reason": None,
        "task_specs": [
            {
                "task_id": f"task-price-challenge-screen-g{challenge_round}",
                "research_kind": PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
                "journey_scope": "atomic_itinerary",
                "question": (
                    "Find the lowest unexamined airfare challenger outside the Self-access "
                    "Gateway Pool. Air positioning is mandatory and will be priced after "
                    "a signal clears the active Price Challenge Threshold."
                ),
                "query_shape": {
                    "trip_type": "price_challenge_screen",
                    "origin_anchor_node_ids": list(intent["origin_anchor_nodes"]),
                    "admitted_origin_gateway_node_ids": origin_ids,
                    "destination_gateway_node_ids": destination_ids,
                    "excluded_gateway_node_ids": sorted(
                        set(origin_ids + destination_ids + excluded_challenge_node_ids)
                    ),
                    "outbound_departure_window": copy.deepcopy(
                        intent["outbound_departure_window"]
                    ),
                    "return_departure_window": copy.deepcopy(
                        intent["return_departure_window"]
                    ),
                    "travel_windows": copy.deepcopy(
                        intent.get("travel_windows", [])
                    ),
                    "minimum_stay_nights": intent.get("minimum_stay_nights"),
                    "maximum_stay_nights": intent.get("maximum_stay_nights"),
                    "incumbent_mandatory_air_ticket_total": {
                        "amount": round(float(incumbent), 2),
                        "currency": state["scenario"]["base_currency"],
                    },
                    "improvement_ratio": threshold,
                    "price_challenge_round": challenge_round,
                    "selection_rule": (
                        "Use a price-ranked query and return source-visible signals in "
                        "ascending price order. The screen is research-only and never "
                        "proves a complete portfolio; no airport, hub, or candidate-count "
                        "quota applies. Each outside-pool signal must state an optimistic "
                        "airfare lower bound for every mandatory positioning flight; do "
                        "not include ground movement or accommodation."
                    ),
                },
                "required": True,
            }
        ],
    }


def _signal_endpoint_pairs(signal: dict[str, Any]) -> set[tuple[str, str]]:
    pairs = set()
    outbound_origin = signal.get(
        "outbound_origin_gateway_node_id", signal.get("origin_gateway_node_id")
    )
    outbound_destination = signal.get(
        "outbound_destination_gateway_node_id",
        signal.get("destination_gateway_node_id"),
    )
    return_origin = signal.get(
        "return_origin_gateway_node_id", signal.get("destination_gateway_node_id")
    )
    return_destination = signal.get(
        "return_destination_gateway_node_id", signal.get("origin_gateway_node_id")
    )
    if isinstance(outbound_origin, str) and isinstance(outbound_destination, str):
        pairs.add((outbound_origin, outbound_destination))
    if isinstance(return_destination, str) and isinstance(return_origin, str):
        pairs.add((return_destination, return_origin))
    return pairs


def _ranked_distinct_portfolios(
    state: dict[str, Any], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Return the cheapest Portfolio for each Journey Alternative."""

    result = []
    seen = set()
    portfolios = state.get("result", {}).get("portfolios", [])
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
    for portfolio in portfolios if isinstance(portfolios, list) else []:
        if not isinstance(portfolio, dict):
            continue
        key = IDENTITY.journey_alternative_id(
            portfolio,
            offer_map,
            movement_map,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(portfolio)
        if len(result) >= limit:
            break
    return result


def _portfolio_comparison_amount(portfolio: dict[str, Any]) -> float | None:
    money = portfolio.get("mandatory_air_ticket_total")
    if not isinstance(money, dict):
        money = (portfolio.get("cost_scope") or {}).get(
            "mandatory_air_ticket_total"
        )
    if not isinstance(money, dict):
        money = (portfolio.get("cost_scope") or {}).get("comparison_total")
    if not isinstance(money, dict):
        money = portfolio.get("total_mandatory_cost")
    amount = money.get("amount") if isinstance(money, dict) else None
    return (
        float(amount)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool)
        else None
    )


def _utc_datetime(value: Any, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HypothesisGeneratorError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HypothesisGeneratorError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _price_refresh_spec(
    state: dict[str, Any], *, now_utc: datetime | None = None
) -> dict[str, Any] | None:
    ranked = _ranked_distinct_portfolios(state)
    if not ranked:
        return None
    offer_ids = list(
        dict.fromkeys(
            str(offer_id)
            for portfolio in ranked[:1]
            for offer_id in portfolio.get("offer_ids", [])
            if offer_id
        )
    )
    offer_map = {
        str(offer.get("offer_id")): offer
        for offer in state.get("offers", [])
        if isinstance(offer, dict) and offer.get("offer_id")
    }
    current = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stale_offers = []
    for offer_id in offer_ids:
        offer = offer_map.get(offer_id)
        if not isinstance(offer, dict):
            continue
        try:
            expires_at = offer_price_deadline(
                str(offer.get("observed_at")),
                str(offer["expires_at"])
                if offer.get("expires_at") is not None
                else None,
            )
        except ValueError as exc:
            raise HypothesisGeneratorError(str(exc)) from exc
        if current >= expires_at:
            stale_offers.append(offer)
    if not stale_offers:
        return None

    task_map = {
        str(task.get("task_id")): task
        for task in state.get("research_tasks", [])
        if isinstance(task, dict) and task.get("task_id")
    }
    movement_map = {
        str(movement.get("movement_id")): movement
        for movement in state.get("movements", [])
        if isinstance(movement, dict) and movement.get("movement_id")
    }
    offers_by_task: dict[str, list[dict[str, Any]]] = {}
    for offer in stale_offers:
        task_id = _text(
            offer.get("research_task_id"),
            f"offers.{offer.get('offer_id')}.research_task_id",
        )
        source_task = task_map.get(task_id)
        if not isinstance(source_task, dict) or source_task.get("research_kind") != "priced_offer":
            raise HypothesisGeneratorError(
                f"stale selected Offer {offer.get('offer_id')} has no priced Work Item"
            )
        offers_by_task.setdefault(task_id, []).append(offer)

    source_hypothesis_ids = {
        str(offer.get("hypothesis_id")) for offer in stale_offers
    }
    source_nodes = [
        state.get("hypothesis_tree", {}).get("nodes", {}).get(hypothesis_id, {})
        for hypothesis_id in source_hypothesis_ids
    ]
    origin_gateway_nodes = list(
        dict.fromkeys(
            gateway_id
            for node in source_nodes
            for gateway_id in (node.get("gateway_nodes", {}) or {}).get("origin", [])
        )
    )
    destination_gateway_nodes = list(
        dict.fromkeys(
            gateway_id
            for node in source_nodes
            for gateway_id in (node.get("gateway_nodes", {}) or {}).get("destination", [])
        )
    )
    if not origin_gateway_nodes or not destination_gateway_nodes:
        raise HypothesisGeneratorError(
            "selected stale Offers have no admitted gateway geometry"
        )
    route_shapes = {
        str(node.get("route_shape")) for node in source_nodes if node.get("route_shape")
    }
    route_shape = (
        next(iter(route_shapes))
        if len(route_shapes) == 1
        else "custom_complete_journey"
    )
    token = hashlib.sha256(
        "|".join(sorted(offer["offer_id"] for offer in stale_offers)).encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    task_specs = []
    for index, (source_task_id, task_offers) in enumerate(
        sorted(offers_by_task.items()), start=1
    ):
        source_task = task_map[source_task_id]
        refresh_targets = []
        for offer in task_offers:
            refresh_targets.append(
                {
                    "offer_id": offer["offer_id"],
                    "source_id": offer.get("source_id"),
                    "provider_offer_id": offer.get("provider_offer_id"),
                    "provider": offer.get("provider"),
                    "seller": offer.get("seller"),
                    "url": offer.get("url"),
                    "observed_at": offer.get("observed_at"),
                    "journey_scope": offer.get("journey_scope"),
                    "direction": offer.get("direction"),
                    "surface_role": offer.get("surface_role"),
                    "ticket_context": copy.deepcopy(
                        offer.get("ticket_context")
                    ),
                    "included_baggage": copy.deepcopy(
                        offer.get("included_baggage", [])
                    ),
                    "movement_geometry": [
                        {
                            key: movement_map[movement_id].get(key)
                            for key in (
                                "movement_id",
                                "from_node",
                                "to_node",
                                "departure",
                                "arrival",
                                "mode",
                                "operator",
                                "service_id",
                            )
                        }
                        for movement_id in offer.get("movement_ids", [])
                        if movement_id in movement_map
                    ],
                }
            )
        query_shape = copy.deepcopy(source_task.get("query_shape", {}))
        query_shape["price_refresh"] = {
            "mode": "exact_selected_offers",
            "refresh_targets": refresh_targets,
        }
        task_specs.append(
            {
                "task_id": f"task-price-refresh-{token}-{index}",
                "research_kind": "priced_offer",
                "journey_scope": source_task.get("journey_scope"),
                "direction": source_task.get("direction"),
                "question": (
                    "Refresh only the selected itinerary on the same dates, endpoints, "
                    "and movement geometry. Revisit each target's original source first; "
                    "for flights, capture any cheaper named seller variant already visible "
                    "on that exact itinerary. For Surface targets, preserve each actual "
                    "source identity or record a transparent replacement source. Do not "
                    "reopen airport or date discovery."
                ),
                "query_shape": query_shape,
                "required": True,
            }
        )
    refresh_offer_ids = sorted(offer["offer_id"] for offer in stale_offers)
    return {
        "hypothesis_id": f"hypothesis-price-refresh-{token}",
        "route_shape": route_shape,
        "origin_gateway_nodes": origin_gateway_nodes,
        "destination_gateway_nodes": destination_gateway_nodes,
        "geographic_cell": "selected-portfolio-price-refresh",
        "advisory_basis": {
            "evidence_backed": True,
            "price_refresh": True,
            "binding": True,
            "requires_all_payable_tasks": True,
            "refresh_offer_ids": refresh_offer_ids,
            "reason": (
                "The selected portfolio contains a price Observation older than "
                "the current-price TTL."
            ),
        },
        "research_sequence": ["refresh_selected_offer_prices_only"],
        "temporal_strategy": "exact_selected_offer_refresh",
        "joint_baseline_reason": None,
        "task_specs": task_specs,
    }
def generate_suggestions(
    raw_state: Any,
    *,
    max_closed_pairs: int = 8,
    include_open_jaw: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
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
    screen_pairs = _screen_pair_specs(raw_state, origins, destinations)
    if gateway_blockers and not screen_pairs:
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
    available_closed = [
        item
        for item in (
            screen_pairs
            if screen_pairs
            else []
            if gateway_blockers
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
    remaining_credits = int(
        raw_state.get("research_credits", {}).get("remaining", 0)
    )
    threshold = float(
        raw_state.get("research_credits", {}).get(
            "price_challenge_threshold", 0.05
        )
    )
    competitive_ceiling = (
        float(incumbent) * (1.0 - threshold)
        if isinstance(incumbent, (int, float))
        and not isinstance(incumbent, bool)
        else None
    )
    if competitive_ceiling is not None:
        available_closed = [
            item
            for item in available_closed
            if item.get("screen_candidate") is None
            or item["screen_candidate"]["total_in_base_currency"]
            < competitive_ceiling
        ]
    if remaining_credits < 2:
        available_closed = []
    closed = available_closed[:max_closed_pairs]
    open_jaw = (
        _open_jaw_spec(origins, destinations)
        if include_open_jaw and not gateway_blockers
        else None
    )
    if open_jaw is not None and open_jaw["hypothesis_id"] in existing_ids:
        open_jaw = None
    advanced = [
        item
        for item in _advanced_specs(raw_state, origins, destinations)
        if item["hypothesis_id"] not in existing_ids
    ] if not gateway_blockers else []
    if competitive_ceiling is not None:
        advanced = [
            item
            for item in advanced
            if not isinstance(
                (item.get("advisory_basis") or {}).get(
                    "optimistic_mandatory_air_ticket_total"
                ),
                (int, float),
            )
            or float(
                (item.get("advisory_basis") or {})[
                    "optimistic_mandatory_air_ticket_total"
                ]
            )
            < competitive_ceiling
        ]
    if remaining_credits < 2:
        advanced = []
    suggestions = [*closed, *([open_jaw] if open_jaw else []), *advanced]
    price_refresh = _price_refresh_spec(raw_state, now_utc=now_utc)
    if (
        price_refresh is not None
        and price_refresh["hypothesis_id"] not in existing_ids
    ):
        suggestions = [price_refresh]
    elif raw_state.get("research_credits", {}).get("stop_reason"):
        suggestions = []
    if (
        not suggestions
        and not raw_state.get("research_credits", {}).get("stop_reason")
        and isinstance(incumbent, (int, float))
        and not isinstance(incumbent, bool)
    ):
        challenge = _price_challenge_screen_spec(
            raw_state, origins, destinations, float(incumbent)
        )
        if (
            challenge is not None
            and challenge["hypothesis_id"] not in existing_ids
        ):
            suggestions.append(challenge)
    return {
        "generator_version": VERSION,
        "decision_owner": "ai",
        "candidate_pool_exhaustion_required": False,
        "world_airport_enumeration": False,
        "origin_gateway_count": len(origins),
        "destination_gateway_count": len(destinations),
        "published_suggestion_count": len(suggestions),
        "gateway_blockers": gateway_blockers,
        "suggestions": suggestions,
        "selection_instruction": (
            "Challenge only the current winner. Verify a candidate only when its "
            "optimistic Mandatory Air Ticket Total clears the active Price Challenge "
            "Threshold. Candidate Slate entries are natural search by-products and "
            "never create additional research work."
        ),
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
