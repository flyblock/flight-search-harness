"""Canonical round-trip Journey Alternative and Ticketing Structure identity."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from flight_search_harness.canonical_values import (
    CanonicalValueError,
    canonical_node_id,
    normalize_node_identity,
    reconcile_node,
)


IDENTITY_VERSION = "1.0"

ENDPOINT_SURFACE_ROLES = {
    "origin-access-outbound",
    "origin-egress-return",
    "destination-access-return",
    "destination-egress-outbound",
}

MOVEMENT_GEOMETRY_FIELDS = (
    "from_node",
    "to_node",
    "departure",
    "arrival",
    "mode",
    "operator",
    "service_id",
    "flight_identity",
    "transfer_from_previous",
)

ROUTE_GEOMETRY_FIELDS = (
    "from_node",
    "to_node",
    "mode",
    "operator",
    "service_id",
    "flight_identity",
)


def _digest(prefix: str, value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def movement_geometry(movement: dict[str, Any]) -> dict[str, Any]:
    """Return timed Movement geometry for Ticketing Structure identity."""

    return {field: movement.get(field) for field in MOVEMENT_GEOMETRY_FIELDS}


def movement_route_geometry(movement: dict[str, Any]) -> dict[str, Any]:
    """Return date-independent Movement geometry for Journey Alternative identity."""

    result = {field: movement.get(field) for field in ROUTE_GEOMETRY_FIELDS}
    transfer = movement.get("transfer_from_previous")
    if isinstance(transfer, dict):
        result["transfer_from_previous"] = {
            key: transfer.get(key)
            for key in ("status", "connection_protection")
        }
    else:
        result["transfer_from_previous"] = None
    return result


def _endpoint_movement_ids(
    portfolio: dict[str, Any], offer_map: dict[str, dict[str, Any]]
) -> set[str]:
    return {
        str(movement_id)
        for offer_id in portfolio.get("offer_ids", [])
        if offer_map.get(str(offer_id), {}).get("surface_role")
        in ENDPOINT_SURFACE_ROLES
        for movement_id in offer_map.get(str(offer_id), {}).get(
            "movement_ids", []
        )
    }


def journey_alternative_geometry(
    portfolio: dict[str, Any],
    offer_map: dict[str, dict[str, Any]],
    movement_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ordered Core Itinerary geometry for one complete Portfolio."""

    endpoint_ids = _endpoint_movement_ids(portfolio, offer_map)
    ordered_ids = [
        *portfolio.get("outbound_movement_ids", []),
        *portfolio.get("return_movement_ids", []),
    ]
    return [
        movement_route_geometry(movement_map.get(str(movement_id), {}))
        for movement_id in ordered_ids
        if str(movement_id) not in endpoint_ids
    ]


def journey_alternative_id(
    portfolio: dict[str, Any],
    offer_map: dict[str, dict[str, Any]],
    movement_map: dict[str, dict[str, Any]],
) -> str:
    return _digest(
        "journey-alternative",
        journey_alternative_geometry(portfolio, offer_map, movement_map),
    )


def offer_ticketing_structure_basis(
    offer: dict[str, Any], movement_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return Seller-independent structural and solver-compatibility facts."""

    movement_ids = [str(item) for item in offer.get("movement_ids", [])]
    unused_ids = [str(item) for item in offer.get("unused_movement_ids", [])]
    ticket_context = offer.get("ticket_context")
    structural_ticket_context = (
        {
            key: copy.deepcopy(value)
            for key, value in ticket_context.items()
            if key
            in {
                "quote_scope",
                "connection_protection",
                "portfolio_role",
                "frontier_endpoints",
                "ticket_structure",
                "ticketed_movement_ids",
                "unticketed_free_movement_ids",
                "planned_exit_node",
                "planned_exit_movement_id",
            }
        }
        if isinstance(ticket_context, dict)
        else None
    )
    return {
        "journey_scope": offer.get("journey_scope"),
        "direction": offer.get("direction"),
        "surface_role": offer.get("surface_role"),
        "atomic": offer.get("atomic"),
        "movement_geometry": [
            movement_geometry(movement_map.get(movement_id, {}))
            for movement_id in movement_ids
        ],
        "unused_movement_geometry": [
            movement_geometry(movement_map.get(movement_id, {}))
            for movement_id in unused_ids
        ],
        "ticket_context": structural_ticket_context,
        # These facts can change solver eligibility, so they deliberately keep
        # otherwise similar Seller Variants in separate equivalence classes.
        "included_baggage": sorted(offer.get("included_baggage", [])),
        "entitlement_tags": sorted(offer.get("entitlement_tags", [])),
    }


def offer_ticketing_structure_id(
    offer: dict[str, Any], movement_map: dict[str, dict[str, Any]]
) -> str:
    return _digest(
        "offer-structure",
        offer_ticketing_structure_basis(offer, movement_map),
    )


def portfolio_ticketing_structure_id(
    portfolio: dict[str, Any],
    offer_map: dict[str, dict[str, Any]],
    movement_map: dict[str, dict[str, Any]],
) -> str:
    endpoint_ids = {
        str(offer_id)
        for offer_id in portfolio.get("offer_ids", [])
        if offer_map.get(str(offer_id), {}).get("surface_role")
        in ENDPOINT_SURFACE_ROLES
    }
    structures = sorted(
        (
            offer_ticketing_structure_basis(
                offer_map[str(offer_id)], movement_map
            )
            for offer_id in portfolio.get("offer_ids", [])
            if str(offer_id) in offer_map
            and str(offer_id) not in endpoint_ids
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    return _digest("ticketing-structure", structures)
