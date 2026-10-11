"""Thin, common final-result projection; full product output remains immutable by ref."""

from __future__ import annotations

import copy
from typing import Any


def summarize(product_id: str, result: dict[str, Any], *, full_result_ref: str) -> dict[str, Any]:
    common = {
        "summary_version": "1.0",
        "product_id": product_id,
        "search_status": result.get("search_status") or result.get("status") or "final",
        "incomplete": bool(result.get("incomplete", False)),
        "outcome": result.get("outcome"),
        "full_result_ref": full_result_ref,
    }
    if product_id == "round_trip":
        portfolio = result.get("best_portfolio") or {}
        return {
            **common,
            "result_kind": "round_trip_portfolio",
            "selection_id": portfolio.get("portfolio_id"),
            "structure": portfolio.get("portfolio_kind"),
            "total_price": copy.deepcopy(portfolio.get("total_mandatory_cost")),
            "outbound": {
                "departure": portfolio.get("departure"),
                "arrival": portfolio.get("outbound_arrival"),
                "movement_ids": copy.deepcopy(portfolio.get("outbound_movement_ids", [])),
            },
            "return": {
                "departure": portfolio.get("return_departure"),
                "arrival": portfolio.get("arrival"),
                "movement_ids": copy.deepcopy(portfolio.get("return_movement_ids", [])),
            },
            "movements": copy.deepcopy(result.get("journey_movements", [])),
            "finalization_audit": copy.deepcopy(result.get("finalization_audit")),
        }
    if product_id == "one_way":
        incumbent = result.get("active_incumbent") or {}
        route = next(
            (
                value
                for value in result.get("routes", [])
                if value.get("id") == incumbent.get("route_id")
            ),
            (result.get("routes") or [{}])[0],
        )
        return {
            **common,
            "result_kind": "one_way_route",
            "selection_id": route.get("id") or incumbent.get("route_id"),
            "node_path": copy.deepcopy(route.get("node_path", [])),
            "edge_ids": copy.deepcopy(route.get("edge_ids", incumbent.get("edge_ids", []))),
            "departure": route.get("departure"),
            "arrival": route.get("arrival"),
            "total_price": {
                "amount": route.get("total_trip_cost", incumbent.get("total_trip_cost")),
                "currency": incumbent.get("currency") or result.get("base_currency"),
            },
            "transfer_count": route.get("transfer_count"),
            "finalization_audit": copy.deepcopy(
                result.get("live_provider_session_audit", {}).get("finalization")
            ),
        }
    if result.get("outcome") == "bounded_no_result":
        return {
            **common,
            "result_kind": "random_loop_no_result",
            "selection_id": None,
            "route": None,
            "finalization_audit": copy.deepcopy(result.get("finalization_audit")),
        }
    selected = result.get("selected_route") or {}
    return {
        **common,
        "result_kind": "random_loop_route",
        "selection_id": selected.get("route_id") or selected.get("id"),
        "route": copy.deepcopy(selected),
        "finalization_audit": copy.deepcopy(result.get("finalization_audit")),
    }
