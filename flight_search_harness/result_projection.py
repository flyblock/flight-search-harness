"""Thin, common final-result projection; full product output remains immutable by ref."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .cost_scope import normalize_cost_scope


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
        delivery = project_delivery(
            product_id, result, full_result_ref=full_result_ref
        )
        delivery_entries = delivery.get("candidate_slate", {}).get("entries", [])
        best = delivery_entries[0] if delivery_entries else {}
        return {
            **common,
            **delivery,
            "selection_id": best.get("journey_alternative_id"),
            "structure": best.get("portfolio_kind"),
            "total_price": copy.deepcopy(best.get("comparison_total")),
            "outbound": copy.deepcopy(best.get("outbound")),
            "return": copy.deepcopy(best.get("return")),
            "movements": copy.deepcopy(result.get("journey_movements", [])),
            "cost_scope": copy.deepcopy(best.get("cost_scope")),
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
        cost_scope = (
            copy.deepcopy(result["cost_scope"])
            if result.get("cost_scope", {}).get("cost_scope_version") == "2.0"
            else normalize_cost_scope(
                result["cost_scope"],
                base_currency=str(
                    incumbent.get("currency")
                    or result.get("base_currency")
                    or ""
                ),
            )
            if isinstance(result.get("cost_scope"), dict)
            else None
        )
        return {
            **common,
            "delivery_view_version": "1.0",
            "result_kind": "one_way_route",
            "claim_scope": result.get("claim_scope", "bounded_verified"),
            "claim": result.get("claim"),
            "selection_id": route.get("id") or incumbent.get("route_id"),
            "node_path": copy.deepcopy(route.get("node_path", [])),
            "edge_ids": copy.deepcopy(route.get("edge_ids", incumbent.get("edge_ids", []))),
            "departure": route.get("departure"),
            "arrival": route.get("arrival"),
            "total_price": {
                "amount": (
                    route.get("mandatory_air_ticket_total", {}).get("amount")
                    if isinstance(route.get("mandatory_air_ticket_total"), dict)
                    else incumbent.get("total_trip_cost")
                ),
                "currency": incumbent.get("currency") or result.get("base_currency"),
            },
            "mandatory_air_ticket_total": copy.deepcopy(
                route.get("mandatory_air_ticket_total")
                or result.get("mandatory_air_ticket_total")
            ),
            "ranking_basis": result.get("ranking_basis"),
            "search_stopping": copy.deepcopy(result.get("search_stopping")),
            "searched_scope": copy.deepcopy(result.get("searched_scope")),
            "unsearched_scope": copy.deepcopy(result.get("unsearched_scope")),
            "excluded_costs": copy.deepcopy(
                result.get("excluded_costs")
                or (cost_scope or {}).get("excluded_costs")
            ),
            "internal_surface_transfers": copy.deepcopy(
                route.get("internal_surface_transfers", [])
            ),
            "purchase_groups": [
                _delivery_offer(value)
                for value in result.get("selected_offers", [])
                if isinstance(value, dict)
            ],
            "freshness": copy.deepcopy(result.get("freshness")),
            "warnings": copy.deepcopy(result.get("warnings", [])),
            "transfer_count": route.get("transfer_count"),
            "finalization_audit": copy.deepcopy(
                result.get("live_provider_session_audit", {}).get("finalization")
            ),
            "cost_scope": cost_scope,
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


def project_delivery(
    product_id: str, result: dict[str, Any], *, full_result_ref: str
) -> dict[str, Any]:
    """Project the facts needed for user delivery, leaving audit state by ref."""

    if product_id != "round_trip":
        return summarize(product_id, result, full_result_ref=full_result_ref)
    slate = result.get("candidate_slate")
    slate = slate if isinstance(slate, dict) else {}
    entries = [
        _round_trip_delivery_entry(entry)
        for entry in slate.get("entries", [])
        if isinstance(entry, dict)
    ]
    return {
        "delivery_view_version": "1.0",
        "result_kind": "round_trip_candidate_slate",
        "claim_scope": "bounded_verified",
        "search_status": result.get("search_status"),
        "full_result_ref": full_result_ref,
        "candidate_slate": {
            "status": slate.get("status"),
            "ranking_method": slate.get("ranking_method"),
            "ranking_scope": slate.get("ranking_scope"),
            "entries": entries,
        },
        "search_stopping": copy.deepcopy(result.get("search_stopping")),
        "searched_scope": copy.deepcopy(result.get("searched_scope")),
        "unsearched_scope": copy.deepcopy(result.get("unsearched_scope")),
        "finalization_audit": copy.deepcopy(result.get("finalization_audit")),
    }


def delivery_price_observations(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the selected best Journey Alternative's price timestamps."""

    slate = result.get("candidate_slate")
    entries = slate.get("entries", []) if isinstance(slate, dict) else []
    if entries and isinstance(entries[0], dict):
        observations = []
        for offer in entries[0].get("selected_offers", []):
            if not isinstance(offer, dict) or not offer.get("observed_at"):
                continue
            observations.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in {
                        "offer_id": offer.get("offer_id"),
                        "observed_at": offer.get("observed_at"),
                        "expires_at": offer.get("expires_at"),
                    }.items()
                    if value is not None
                }
            )
        if observations:
            return observations
    one_way = result.get("price_observations")
    if isinstance(one_way, list):
        return copy.deepcopy(one_way)
    fallback = result.get("price_observations")
    return copy.deepcopy(fallback) if isinstance(fallback, list) else []


def _round_trip_delivery_entry(entry: dict[str, Any]) -> dict[str, Any]:
    movements = [
        copy.deepcopy(value)
        for value in entry.get("selected_movements", [])
        if isinstance(value, dict)
    ]
    offers = [
        _delivery_offer(value)
        for value in entry.get("selected_offers", [])
        if isinstance(value, dict)
    ]
    return {
        "rank": entry.get("rank"),
        "journey_alternative_id": entry.get("route_alternative_id"),
        "portfolio_kind": entry.get("portfolio_kind"),
        "comparison_total": copy.deepcopy(entry.get("comparison_total")),
        "mandatory_air_ticket_total": copy.deepcopy(
            entry.get("mandatory_air_ticket_total", entry.get("comparison_total"))
        ),
        "price_delta_from_rank_1": copy.deepcopy(
            entry.get("price_delta_from_rank_1")
        ),
        "cost_scope": copy.deepcopy(entry.get("cost_scope")),
        "excluded_costs": copy.deepcopy(
            (entry.get("cost_scope") or {}).get("excluded_costs")
        ),
        "outbound": copy.deepcopy(entry.get("outbound")),
        "return": copy.deepcopy(entry.get("return")),
        "total_elapsed_minutes": entry.get("total_elapsed_minutes"),
        "travel_window_ids": copy.deepcopy(entry.get("travel_window_ids", [])),
        "movements": movements,
        "internal_surface_transfers": copy.deepcopy(
            entry.get("internal_surface_transfers", [])
        ),
        "purchase_groups": offers,
        "freshness": copy.deepcopy(entry.get("evidence_freshness")),
        "warnings": _delivery_warnings(entry, movements, offers),
    }


def _delivery_offer(offer: dict[str, Any]) -> dict[str, Any]:
    url = offer.get("url") or offer.get("booking_url")
    return {
        key: copy.deepcopy(value)
        for key, value in {
            "offer_id": offer.get("offer_id"),
            "seller": offer.get("seller"),
            "provider": offer.get("provider"),
            "movement_ids": offer.get("movement_ids"),
            "price": offer.get("price"),
            "mandatory_baggage_cost": offer.get("mandatory_baggage_cost"),
            "required_accommodation_cost": offer.get(
                "required_accommodation_cost"
            ),
            "included_baggage": offer.get("included_baggage"),
            "booking_url": url,
            "booking_link_kind": _booking_link_kind(url),
            "ticket_context": _delivery_ticket_context(offer.get("ticket_context")),
            "observed_at": offer.get("observed_at"),
            "expires_at": offer.get("expires_at"),
        }.items()
        if value is not None
    }


def _delivery_ticket_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "quote_scope",
        "connection_protection",
        "baggage_details",
        "pricing_explanation",
        "ticketed_movement_ids",
        "unticketed_free_movement_ids",
        "seller_quote",
    }
    result = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in allowed and item is not None
    }
    return result or None


def _booking_link_kind(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unavailable"
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if "google." in host and "/travel/flights/booking" in path:
        return "comparison_listing"
    if "flixbus." in host and "/search" in path:
        return "exact_search"
    if host.endswith("kiwi.com"):
        return "booking_handoff"
    return "booking_handoff"


def _delivery_warnings(
    entry: dict[str, Any],
    movements: list[dict[str, Any]],
    offers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    offer_for_movement = {
        str(movement_id): str(offer.get("offer_id"))
        for offer in offers
        for movement_id in offer.get("movement_ids", [])
    }
    movement_by_id = {
        str(movement.get("movement_id")): movement
        for movement in movements
        if movement.get("movement_id")
    }
    for direction in ("outbound", "return"):
        summary = entry.get(direction)
        ids = summary.get("movement_ids", []) if isinstance(summary, dict) else []
        for previous_id, following_id in zip(ids, ids[1:]):
            previous = movement_by_id.get(str(previous_id))
            following = movement_by_id.get(str(following_id))
            if not isinstance(previous, dict) or not isinstance(following, dict):
                continue
            wait = _wait_minutes(previous.get("arrival"), following.get("departure"))
            if wait is not None and wait >= 360:
                warnings.append(
                    {
                        "code": "long_wait",
                        "direction": direction,
                        "at_node": previous.get("to_node"),
                        "minutes": wait,
                    }
                )
            if offer_for_movement.get(str(previous_id)) != offer_for_movement.get(
                str(following_id)
            ):
                warnings.append(
                    {
                        "code": "unprotected_atomic_boundary",
                        "direction": direction,
                        "at_node": previous.get("to_node"),
                        "previous_movement_id": previous_id,
                        "following_movement_id": following_id,
                    }
                )
            transfer = following.get("transfer_from_previous")
            if isinstance(transfer, dict) and transfer.get("status") == "unknown":
                warnings.append(
                    {
                        "code": "connection_requirements_unverified",
                        "direction": direction,
                        "at_node": previous.get("to_node"),
                    }
                )
    for movement in movements:
        distance = movement.get("distance_km")
        if movement.get("mode") == "walk" and isinstance(distance, (int, float)):
            if distance >= 5:
                warnings.append(
                    {
                        "code": "long_walk",
                        "movement_id": movement.get("movement_id"),
                        "distance_km": distance,
                    }
                )
    if entry.get("overnight_connection_count"):
        warnings.append(
            {
                "code": "overnight_connection",
                "count": entry.get("overnight_connection_count"),
            }
        )
    return warnings


def _wait_minutes(arrival: Any, departure: Any) -> int | None:
    if not isinstance(arrival, str) or not isinstance(departure, str):
        return None
    try:
        start = datetime.fromisoformat(arrival.replace("Z", "+00:00"))
        end = datetime.fromisoformat(departure.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end - start).total_seconds() // 60))
