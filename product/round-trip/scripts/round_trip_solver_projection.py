"""Build a bounded solver view without deleting permanent round-trip evidence."""

from __future__ import annotations

import copy
from typing import Any


PROJECTION_VERSION = "1.0"


class SolverProjectionError(ValueError):
    pass


def _price_in_base(offer: dict[str, Any], state: dict[str, Any]) -> float:
    scenario = state["scenario"]
    base = scenario["base_currency"]
    price = offer.get("price", {})
    amount = float(price.get("amount", 0))
    currency = price.get("currency")
    if currency == base:
        return amount
    rates = scenario.get("fx_snapshot", {}).get("rates", {})
    rate = rates.get(currency)
    return amount * float(rate) if rate is not None else float("inf")


def _offer_order(offer: dict[str, Any], state: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _price_in_base(offer, state),
        str(offer.get("observed_at") or ""),
        str(offer.get("offer_id") or ""),
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
    offers = [copy.deepcopy(item) for item in state.get("offers", [])]
    offer_map = {
        str(item.get("offer_id")): item for item in offers if item.get("offer_id")
    }
    if len(offer_map) != len(offers):
        raise SolverProjectionError("every ledger offer needs a unique offer_id")
    limit = int(state.get("scenario", {}).get("intent", {}).get("max_solver_offers", 24))
    if limit < 1:
        raise SolverProjectionError("max_solver_offers must be positive")
    previous = state.get("solver_projection", {})
    pins = list(dict.fromkeys(previous.get("pinned_offer_ids", []))) if isinstance(previous, dict) else []
    unknown = sorted(set(pins) - set(offer_map))
    if unknown:
        raise SolverProjectionError(f"pinned offers are unknown: {unknown}")
    if len(pins) > limit:
        raise SolverProjectionError("pinned offers exceed max_solver_offers")

    selected: list[str] = []
    reasons: dict[str, list[str]] = {}

    def add(offer_id: str, reason: str) -> None:
        if offer_id not in offer_map or offer_id in selected or len(selected) >= limit:
            return
        selected.append(offer_id)
        reasons.setdefault(offer_id, []).append(reason)

    for offer_id in pins:
        add(offer_id, "ai_pinned")
    incumbent = state.get("result", {}).get("best_portfolio")
    if isinstance(incumbent, dict):
        for offer_id in incumbent.get("offer_ids", []):
            add(str(offer_id), "current_incumbent")
    active = state.get("active_hypothesis")
    active_id = active.get("hypothesis_id") if isinstance(active, dict) else None
    if active_id:
        for offer in sorted(offers, key=lambda item: _offer_order(item, state)):
            if offer.get("hypothesis_id") == active_id:
                add(str(offer["offer_id"]), "active_hypothesis")

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
    excluded = sorted(set(offer_map) - selected_set)
    return {
        "projection_version": PROJECTION_VERSION,
        "status": "complete_ledger" if not excluded else "bounded_projection",
        "ledger_offer_count": len(offers),
        "solver_offer_limit": limit,
        "pinned_offer_ids": pins,
        "selected_offer_ids": selected,
        "excluded_offer_ids": excluded,
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
