"""Compile a round-trip Gateway pool into bounded Browser search slices."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any


VERSION = "1.0"
DEFAULT_ORIGIN_CAPACITY = 7
DEFAULT_DESTINATION_CAPACITY = 7


class ScopeCompilerError(ValueError):
    pass


def _ids(values: Any, field: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ScopeCompilerError(f"{field} must be a non-empty array")
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ScopeCompilerError(f"{field}[{index}] must be a non-empty string")
        normalized = value.strip().upper()
        if normalized not in result:
            result.append(normalized)
    return result


def _local_date(value: Any, field: str) -> date:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ScopeCompilerError(f"{field} must be an ISO date or timestamp") from exc


def _window(value: Any, field: str) -> tuple[date, date]:
    if not isinstance(value, dict):
        raise ScopeCompilerError(f"{field} must be an object")
    start = _local_date(value.get("start"), f"{field}.start")
    end = _local_date(value.get("end"), f"{field}.end")
    if start > end:
        raise ScopeCompilerError(f"{field}.start must not be after end")
    return start, end


def _chunks(values: list[str] | list[date], size: int) -> list[list[Any]]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ScopeCompilerError("Browser capacity must be a positive integer")
    return [values[index : index + size] for index in range(0, len(values), size)]


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def safe_date_rows(
    outbound_window: dict[str, Any],
    return_window: dict[str, Any],
    minimum_stay_nights: int | None,
) -> list[dict[str, str]]:
    """Return a conservative pre-screen envelope using compact date rows.

    The exact Destination Stay Nights remain dependent on the observed arrival
    Local Date.  Without a verified maximum outbound duration, maximum stay
    cannot safely remove a return date before exact itinerary verification.
    """

    outbound_start, outbound_end = _window(
        outbound_window, "outbound_departure_window"
    )
    return_start, return_end = _window(
        return_window, "return_departure_window"
    )
    minimum = 0 if minimum_stay_nights is None else minimum_stay_nights
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ScopeCompilerError("minimum_stay_nights must be non-negative or null")
    rows = []
    for outbound_date in _dates(outbound_start, outbound_end):
        first_return = max(return_start, outbound_date + timedelta(days=minimum))
        if first_return > return_end:
            continue
        rows.append(
            {
                "outbound_date": outbound_date.isoformat(),
                "return_date_start": first_return.isoformat(),
                "return_date_end": return_end.isoformat(),
            }
        )
    return rows


def _stable_id(kind: str, value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{token}"


def compile_region_pool_scope(
    query_shape: dict[str, Any],
    *,
    origin_capacity: int = DEFAULT_ORIGIN_CAPACITY,
    destination_capacity: int = DEFAULT_DESTINATION_CAPACITY,
) -> dict[str, Any]:
    """Compile one requested pool into airport-bounded Browser Work slices."""

    origins = _ids(query_shape.get("origin_gateway_node_ids"), "origin_gateway_node_ids")
    destinations = _ids(
        query_shape.get("destination_gateway_node_ids"),
        "destination_gateway_node_ids",
    )
    rows = safe_date_rows(
        query_shape.get("outbound_departure_window"),
        query_shape.get("return_departure_window"),
        query_shape.get("minimum_stay_nights"),
    )
    if not rows:
        raise ScopeCompilerError("requested windows contain no feasible date row")
    plan_basis = {
        "origins": origins,
        "destinations": destinations,
        "rows": rows,
        "travel_window_id": query_shape.get("travel_window_id"),
        "origin_capacity": origin_capacity,
        "destination_capacity": destination_capacity,
    }
    plan_id = _stable_id("scope-plan", plan_basis)
    slices = []
    for origin_group in _chunks(origins, origin_capacity):
        for destination_group in _chunks(destinations, destination_capacity):
            slice_basis = {
                "plan_id": plan_id,
                "origins": origin_group,
                "destinations": destination_group,
            }
            slice_id = _stable_id("scope-slice", slice_basis)
            slices.append(
                {
                    "scope_plan_id": plan_id,
                    "scope_slice_id": slice_id,
                    **(
                        {
                            "travel_window_id": query_shape.get(
                                "travel_window_id"
                            )
                        }
                        if query_shape.get("travel_window_id")
                        else {}
                    ),
                    "origin_gateway_node_ids": list(origin_group),
                    "destination_gateway_node_ids": list(destination_group),
                    "outbound_departure_window": copy.deepcopy(
                        query_shape["outbound_departure_window"]
                    ),
                    "return_departure_window": copy.deepcopy(
                        query_shape["return_departure_window"]
                    ),
                    "date_rows": copy.deepcopy(rows),
                    "minimum_stay_nights": query_shape.get(
                        "minimum_stay_nights"
                    ),
                    "maximum_stay_nights": query_shape.get(
                        "maximum_stay_nights"
                    ),
                    "required_view": "visible_date_grid",
                }
            )
    return {
        "scope_compiler_version": VERSION,
        "scope_plan_id": plan_id,
        "origin_gateway_node_ids": origins,
        "destination_gateway_node_ids": destinations,
        "date_rows": rows,
        "logical_date_pair_count": sum(
            (date.fromisoformat(row["return_date_end"]) - date.fromisoformat(row["return_date_start"])).days + 1
            for row in rows
        ),
        "slices": slices,
    }


def compile_discovery_scope(
    query_shape: dict[str, Any],
    *,
    destination_search_geography: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prefer one Provider-native region query; retain rectangles as fallback."""

    if not isinstance(destination_search_geography, dict):
        return compile_region_pool_scope(query_shape)
    if (
        destination_search_geography.get("kind") != "country"
        or not isinstance(
            destination_search_geography.get("country_code"), str
        )
    ):
        raise ScopeCompilerError(
            "destination_search_geography must identify a country"
        )
    origins = _ids(
        query_shape.get("origin_gateway_node_ids"),
        "origin_gateway_node_ids",
    )
    destinations = _ids(
        query_shape.get("destination_gateway_node_ids"),
        "destination_gateway_node_ids",
    )
    rows = safe_date_rows(
        query_shape.get("outbound_departure_window"),
        query_shape.get("return_departure_window"),
        query_shape.get("minimum_stay_nights"),
    )
    if not rows:
        raise ScopeCompilerError("requested windows contain no feasible date row")
    plan_basis = {
        "scope_strategy": "provider_native_first",
        "origins": origins,
        "destinations": destinations,
        "date_rows": rows,
        "travel_window_id": query_shape.get("travel_window_id"),
        "destination_search_geography": destination_search_geography,
    }
    plan_id = _stable_id("scope-plan", plan_basis)
    slice_basis = {"scope_plan_id": plan_id, **plan_basis}
    scope_slice = {
        "scope_plan_id": plan_id,
        "scope_slice_id": _stable_id("scope-slice", slice_basis),
        "scope_strategy": "provider_native_first",
        **(
            {"travel_window_id": query_shape.get("travel_window_id")}
            if query_shape.get("travel_window_id")
            else {}
        ),
        "origin_gateway_node_ids": origins,
        "destination_gateway_node_ids": destinations,
        "destination_search_geography": copy.deepcopy(
            destination_search_geography
        ),
        "outbound_departure_window": copy.deepcopy(
            query_shape.get("outbound_departure_window")
        ),
        "return_departure_window": copy.deepcopy(
            query_shape.get("return_departure_window")
        ),
        "date_rows": rows,
        "minimum_stay_nights": query_shape.get("minimum_stay_nights"),
        "maximum_stay_nights": query_shape.get("maximum_stay_nights"),
        "required_view": "provider_price_ranked_region",
        "required_browser_capabilities": [
            "multi_origin",
            "native_country_destination",
            "flexible_round_trip_dates",
            "visible_price_ordering",
        ],
    }
    return {
        "scope_compiler_version": VERSION,
        "scope_plan_id": plan_id,
        "scope_strategy": "provider_native_first",
        "origin_gateway_node_ids": origins,
        "destination_gateway_node_ids": destinations,
        "date_rows": rows,
        "logical_date_pair_count": sum(
            (
                date.fromisoformat(row["return_date_end"])
                - date.fromisoformat(row["return_date_start"])
            ).days
            + 1
            for row in rows
        ),
        "slices": [scope_slice],
    }


def slice_task_spec(scope_slice: dict[str, Any], sequence: int) -> dict[str, Any]:
    """Project one compiled slice into one Product-owned research task spec."""

    slice_id = str(scope_slice["scope_slice_id"])
    return {
        "task_id": f"task-region-pool-{sequence:03d}-{slice_id.removeprefix('scope-slice-')}",
        "research_kind": "region_pool_fare_screen",
        "question": (
            "Use a public Browser Provider whose visible controls can express "
            "this country or regional scope and return its price-ranked fare."
            if scope_slice.get("scope_strategy") == "provider_native_first"
            else "Use the Browser's visible lowest-price date grid for this exact "
            "airport rectangle and date tile."
        ),
        "query_shape": {
            "trip_type": "region_pool_fare_screen_slice",
            **copy.deepcopy(scope_slice),
            "objective": "lowest_total_price",
            "required_price_selection_method": True,
        },
    }

