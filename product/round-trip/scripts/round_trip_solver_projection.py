"""Build a bounded solver view without deleting permanent round-trip evidence."""

from __future__ import annotations

import copy
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECTION_VERSION = "1.0"


class SolverProjectionError(ValueError):
    pass


def _load_identity():
    path = Path(__file__).with_name("round_trip_identity.py")
    spec = importlib.util.spec_from_file_location("round_trip_identity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


IDENTITY = _load_identity()


def _money_in_base(
    money: dict[str, Any] | None, state: dict[str, Any]
) -> float:
    scenario = state["scenario"]
    base = scenario["base_currency"]
    if not isinstance(money, dict):
        return 0.0
    amount = float(money.get("amount", 0))
    currency = money.get("currency")
    if currency == base:
        return amount
    rates = scenario.get("fx_snapshot", {}).get("rates", {})
    rate = rates.get(currency)
    return (
        amount * float(rate)
        if isinstance(rate, (int, float))
        and not isinstance(rate, bool)
        and float(rate) > 0
        else float("inf")
    )


def _comparison_cost_in_base(offer: dict[str, Any], state: dict[str, Any]) -> float:
    """Use the Mandatory Air Ticket Total basis compared by the Solver."""

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

    parts = [
        _money_in_base(offer.get("price"), state),
        _money_in_base(offer.get("mandatory_baggage_cost"), state),
    ]
    return sum(parts) if all(value != float("inf") for value in parts) else float("inf")


def _offer_order(offer: dict[str, Any], state: dict[str, Any]) -> tuple[Any, ...]:
    try:
        observed_at = datetime.fromisoformat(
            str(offer.get("observed_at") or "").replace("Z", "+00:00")
        )
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError
        recency = -observed_at.astimezone(timezone.utc).timestamp()
    except ValueError:
        recency = float("inf")
    return (
        _comparison_cost_in_base(offer, state),
        recency,
        str(offer.get("offer_id") or ""),
    )


def _seller_identity(offer: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(offer.get(field) or "").strip().casefold()
        for field in ("source_id", "provider", "seller")
    )


def _projection_representatives(
    offers: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, list[str]],
    dict[str, str],
]:
    """Collapse snapshots and Seller Variants without deleting ledger Evidence."""

    movement_map = {
        str(item.get("movement_id")): item
        for item in movements
        if item.get("movement_id")
    }
    all_offer_map = {
        str(item.get("offer_id")): item
        for item in offers
        if item.get("offer_id")
    }

    def lineage_root(offer: dict[str, Any]) -> str:
        current = offer
        seen: set[str] = set()
        while current.get("refresh_of_offer_id"):
            target_id = str(current["refresh_of_offer_id"])
            if target_id in seen or target_id not in all_offer_map:
                return target_id
            seen.add(target_id)
            current = all_offer_map[target_id]
        return str(current.get("offer_id"))
    by_structure: dict[str, list[dict[str, Any]]] = {}
    for offer in offers:
        structure_id = IDENTITY.offer_ticketing_structure_id(
            offer, movement_map
        )
        by_structure.setdefault(structure_id, []).append(offer)

    representatives: dict[str, dict[str, Any]] = {}
    representative_by_offer_id: dict[str, str] = {}
    represented_offer_ids: dict[str, list[str]] = {}
    exclusion_reasons: dict[str, str] = {}
    for structure_offers in by_structure.values():
        by_seller: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for offer in structure_offers:
            snapshot_key = (*_seller_identity(offer), lineage_root(offer))
            by_seller.setdefault(snapshot_key, []).append(offer)
        current_seller_offers = []
        for seller_offers in by_seller.values():
            current = min(seller_offers, key=lambda item: _offer_order(item, state)[1:])
            current_seller_offers.append(current)
            for offer in seller_offers:
                if offer is not current:
                    exclusion_reasons[str(offer["offer_id"])] = (
                        "superseded_snapshot"
                    )
        representative = min(
            current_seller_offers,
            key=lambda item: _offer_order(item, state),
        )
        representative_id = str(representative["offer_id"])
        representatives[representative_id] = representative
        represented_offer_ids[representative_id] = sorted(
            str(item["offer_id"]) for item in structure_offers
        )
        for offer in structure_offers:
            offer_id = str(offer["offer_id"])
            representative_by_offer_id[offer_id] = representative_id
            if offer_id != representative_id and offer_id not in exclusion_reasons:
                exclusion_reasons[offer_id] = "represented_seller_variant"
    return (
        representatives,
        representative_by_offer_id,
        represented_offer_ids,
        exclusion_reasons,
    )


def build_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Return selected offers/movements plus an auditable projection decision.

    The Session remains the permanent ledger. This function only chooses the
    bounded view supplied to the combinatorial solver. AI pins and the current
    incumbent are preserved first; deterministic coverage and price ordering
    fill the remaining slots.
    """

    if not isinstance(state, dict):
        raise SolverProjectionError("state must be an object")
    ledger_offers = [copy.deepcopy(item) for item in state.get("offers", [])]
    ledger_offer_map = {
        str(item.get("offer_id")): item
        for item in ledger_offers
        if item.get("offer_id")
    }
    if len(ledger_offer_map) != len(ledger_offers):
        raise SolverProjectionError("every ledger offer needs a unique offer_id")
    limit = int(state.get("scenario", {}).get("intent", {}).get("max_solver_offers", 24))
    if limit < 1:
        raise SolverProjectionError("max_solver_offers must be positive")
    previous = state.get("solver_projection", {})
    retired = (
        list(dict.fromkeys(previous.get("retired_offer_ids", [])))
        if isinstance(previous, dict)
        else []
    )
    unknown_retired = sorted(set(retired) - set(ledger_offer_map))
    if unknown_retired:
        raise SolverProjectionError(
            f"retired offers are unknown: {unknown_retired}"
        )
    eligible_ledger_offer_map = {
        offer_id: offer
        for offer_id, offer in ledger_offer_map.items()
        if offer_id not in set(retired)
    }
    (
        offer_map,
        representative_by_offer_id,
        represented_offer_ids,
        exclusion_reasons,
    ) = _projection_representatives(
        list(eligible_ledger_offer_map.values()),
        state.get("movements", []),
        state,
    )
    offers = list(offer_map.values())
    pins = list(dict.fromkeys(previous.get("pinned_offer_ids", []))) if isinstance(previous, dict) else []
    unknown = sorted(set(pins) - set(eligible_ledger_offer_map))
    if unknown:
        raise SolverProjectionError(f"pinned offers are unknown: {unknown}")
    represented_pins = list(
        dict.fromkeys(representative_by_offer_id[offer_id] for offer_id in pins)
    )
    if len(represented_pins) > limit:
        raise SolverProjectionError("pinned offers exceed max_solver_offers")

    selected: list[str] = []
    reasons: dict[str, list[str]] = {}

    def add(offer_id: str, reason: str) -> None:
        if offer_id not in offer_map or offer_id in selected or len(selected) >= limit:
            return
        selected.append(offer_id)
        reasons.setdefault(offer_id, []).append(reason)

    def add_group(offer_ids: list[str], reason: str) -> bool:
        represented = list(
            dict.fromkeys(
                representative_by_offer_id.get(str(offer_id), str(offer_id))
                for offer_id in offer_ids
                if representative_by_offer_id.get(str(offer_id), str(offer_id))
                in offer_map
            )
        )
        missing = [offer_id for offer_id in represented if offer_id not in selected]
        if len(selected) + len(missing) > limit:
            return False
        for offer_id in represented:
            add(offer_id, reason)
        return True

    for offer_id in represented_pins:
        add(offer_id, "ai_pinned")
    incumbent = state.get("result", {}).get("best_portfolio")
    if isinstance(incumbent, dict):
        add_group(
            [str(item) for item in incumbent.get("offer_ids", [])],
            "current_incumbent",
        )

    ledger_movement_map = {
        str(item.get("movement_id")): item
        for item in state.get("movements", [])
        if item.get("movement_id")
    }
    preserved_journeys: set[str] = set()
    for portfolio in state.get("result", {}).get("portfolios", []):
        if not isinstance(portfolio, dict):
            continue
        journey_id = IDENTITY.journey_alternative_id(
            portfolio, ledger_offer_map, ledger_movement_map
        )
        if journey_id in preserved_journeys:
            continue
        if add_group(
            [str(item) for item in portfolio.get("offer_ids", [])],
            "complete_journey_reservation",
        ):
            preserved_journeys.add(journey_id)
        if len(preserved_journeys) >= 3:
            break
    active = state.get("active_hypothesis")
    active_id = active.get("hypothesis_id") if isinstance(active, dict) else None
    if active_id:
        active_offers = [
            offer for offer in offers if offer.get("hypothesis_id") == active_id
        ]
        add_group(
            [str(offer["offer_id"]) for offer in active_offers],
            "active_hypothesis",
        )

    by_task: dict[str, list[dict[str, Any]]] = {}
    for offer in offers:
        key = str(offer.get("research_task_id") or "unassigned")
        by_task.setdefault(key, []).append(offer)
    for task_id in sorted(by_task):
        best = min(by_task[task_id], key=lambda item: _offer_order(item, state))
        add(str(best["offer_id"]), f"lowest_cost_for_task:{task_id}")

    for offer in sorted(offers, key=lambda item: _offer_order(item, state)):
        add(str(offer["offer_id"]), "deterministic_cost_fill")

    selected_set = set(selected)
    movement_ids = {
        str(movement_id)
        for offer_id in selected
        for movement_id in (
            offer_map[offer_id].get("movement_ids", [])
            + offer_map[offer_id].get("unused_movement_ids", [])
        )
    }
    movements = [
        copy.deepcopy(item)
        for item in state.get("movements", [])
        if str(item.get("movement_id")) in movement_ids
    ]
    excluded = sorted(set(ledger_offer_map) - selected_set)
    capacity_excluded = sorted(set(offer_map) - selected_set)
    for offer_id in retired:
        exclusion_reasons[offer_id] = "retired"
    for offer_id in capacity_excluded:
        exclusion_reasons[offer_id] = "unresolved_competitive_alternative"
    return {
        "projection_version": PROJECTION_VERSION,
        "status": (
            "complete_ledger" if not capacity_excluded else "bounded_projection"
        ),
        "ledger_offer_count": len(ledger_offers),
        "solver_offer_limit": limit,
        "pinned_offer_ids": pins,
        "retired_offer_ids": retired,
        "selected_offer_ids": selected,
        "excluded_offer_ids": excluded,
        "unresolved_capacity_exclusion_offer_ids": capacity_excluded,
        "unresolved_competitive_alternative_offer_ids": capacity_excluded,
        "exclusion_reasons": exclusion_reasons,
        "represented_offer_ids": represented_offer_ids,
        "selection_reasons": reasons,
        "coverage_claim": "solver result is complete only within selected_offer_ids; excluded ledger evidence remains persisted",
        "offers": [offer_map[offer_id] for offer_id in selected],
        "movements": movements,
    }


def set_pins(state: dict[str, Any], offer_ids: list[str], reason: str) -> dict[str, Any]:
    if not isinstance(offer_ids, list) or not all(isinstance(item, str) and item for item in offer_ids):
        raise SolverProjectionError("offer_ids must be non-empty strings")
    next_state = copy.deepcopy(state)
    projection = next_state.setdefault("solver_projection", {})
    projection["pinned_offer_ids"] = list(dict.fromkeys(offer_ids))
    projection["pin_reason"] = str(reason).strip()
    return next_state


def retire_offers(
    state: dict[str, Any], offer_ids: list[str], reason: str
) -> dict[str, Any]:
    if not isinstance(offer_ids, list) or not offer_ids or not all(
        isinstance(item, str) and item for item in offer_ids
    ):
        raise SolverProjectionError("retired offer_ids must be non-empty strings")
    ledger_ids = {
        str(offer.get("offer_id"))
        for offer in state.get("offers", [])
        if offer.get("offer_id")
    }
    unknown = sorted(set(offer_ids) - ledger_ids)
    if unknown:
        raise SolverProjectionError(f"retired offers are unknown: {unknown}")
    next_state = copy.deepcopy(state)
    projection = next_state.setdefault("solver_projection", {})
    retired = list(
        dict.fromkeys(
            [*projection.get("retired_offer_ids", []), *offer_ids]
        )
    )
    projection["retired_offer_ids"] = retired
    projection["pinned_offer_ids"] = [
        offer_id
        for offer_id in projection.get("pinned_offer_ids", [])
        if offer_id not in set(retired)
    ]
    projection["retirement_reason"] = str(reason).strip()
    return next_state
