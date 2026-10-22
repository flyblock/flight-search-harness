"""Materialize city/region round-trip intent without choosing airports for the AI."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flight_search_harness.canonical_values import (
    CanonicalValueError,
    normalize_node_identity,
    reconcile_node,
)
from flight_search_harness.search_effort import normalize_search_effort


VERSION = "0.4.0"
MODES = ["flight", "rail", "bus", "coach", "ferry", "local_transit", "walk", "rideshare", "car"]
GROUND_MODES = [mode for mode in MODES if mode != "flight"]
DEFAULT_BUFFERS = {
    "surface_to_flight": 120,
    "self_transfer_to_flight": 120,
    "flight_to_flight": 60,
    "flight_to_surface": 60,
    "surface_to_surface": 0,
}


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


class MaterializationError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaterializationError(f"{field} must be a non-empty string")
    return value.strip()


def _node(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError(f"{field} must be an object")
    node = dict(raw)
    node["node_id"] = _text(raw.get("node_id"), f"{field}.node_id")
    node["node_type"] = _text(raw.get("node_type"), f"{field}.node_type")
    if node["node_type"] not in {"airport", "railway_station", "bus_terminal", "ferry_terminal", "city_destination"}:
        raise MaterializationError(f"{field}.node_type is unsupported")
    node["location"] = _text(raw.get("location"), f"{field}.location")
    node["country_or_region"] = _text(raw.get("country_or_region"), f"{field}.country_or_region")
    node["timezone"] = _text(raw.get("timezone"), f"{field}.timezone")
    if raw.get("utc_offset") is not None:
        offset = _text(raw.get("utc_offset"), f"{field}.utc_offset")
        try:
            datetime.fromisoformat("2000-01-01T00:00:00" + offset)
        except ValueError as exc:
            raise MaterializationError(f"{field}.utc_offset must be an ISO UTC offset") from exc
    country_code = raw.get("country_code")
    if country_code is not None and (not isinstance(country_code, str) or len(country_code) != 2 or country_code.upper() != country_code):
        raise MaterializationError(f"{field}.country_code must be an uppercase two-letter code")
    try:
        return normalize_node_identity(node)
    except CanonicalValueError as exc:
        raise MaterializationError(f"{field}: {exc}") from exc


def _nodes(raw: Any, field: str, *, required: bool) -> list[dict[str, Any]]:
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        raise MaterializationError(f"{field} must be a {'non-empty ' if required else ''}array")
    result = [_node(item, f"{field}[{index}]") for index, item in enumerate(raw)]
    ids = [item["node_id"] for item in result]
    if len(ids) != len(set(ids)):
        raise MaterializationError(f"{field} contains duplicate node IDs")
    return result


def _side(raw: Any, field: str, currency: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError(f"{field} must be an object")
    anchors = _nodes(raw.get("anchor_nodes"), f"{field}.anchor_nodes", required=True)
    gateways = _nodes(raw.get("seed_gateway_nodes"), f"{field}.seed_gateway_nodes", required=False)
    policy = raw.get("policy", "city_region")
    if policy not in {"exact", "city_region", "extended_region"}:
        raise MaterializationError(f"{field}.policy is unsupported")
    endpoint_policy = raw.get("endpoint_policy", "anchor_required")
    if endpoint_policy not in {"anchor_required", "any_admitted_gateway"}:
        raise MaterializationError(f"{field}.endpoint_policy is unsupported")
    if policy == "exact" and endpoint_policy != "anchor_required":
        raise MaterializationError(f"{field}.exact policy requires anchor_required")
    expansion = raw.get("expansion", {})
    if not isinstance(expansion, dict):
        raise MaterializationError(f"{field}.expansion must be an object")
    enabled = expansion.get("enabled", policy != "exact")
    if policy == "exact" and enabled:
        raise MaterializationError(f"{field}.exact policy cannot enable Provider expansion")
    ground_modes = raw.get("allowed_ground_modes", GROUND_MODES)
    if not isinstance(ground_modes, list) or not ground_modes or not set(ground_modes) <= set(GROUND_MODES):
        raise MaterializationError(f"{field}.allowed_ground_modes is invalid")
    max_ground_cost = _optional_money(
        raw.get("max_ground_cost"), f"{field}.max_ground_cost", currency
    )
    return {
        "region_id": _text(raw.get("region_id"), f"{field}.region_id"),
        "label": _text(raw.get("label"), f"{field}.label"),
        "country_codes": list(raw.get("country_codes", [])),
        "search_geography": copy.deepcopy(raw.get("search_geography")),
        "policy": policy,
        "endpoint_policy": endpoint_policy,
        "anchor_nodes": anchors,
        "seed_gateway_nodes": gateways,
        "max_access_time_minutes": _optional_nonnegative_integer(
            raw.get("max_access_time_minutes"), f"{field}.max_access_time_minutes"
        ),
        "max_egress_time_minutes": _optional_nonnegative_integer(
            raw.get("max_egress_time_minutes"), f"{field}.max_egress_time_minutes"
        ),
        "max_ground_cost": max_ground_cost,
        "allowed_ground_modes": ground_modes,
        "expansion": {
            "enabled": bool(enabled),
            "provider_kinds": list(expansion.get("provider_kinds", ["browser_search", "region_catalog", "scheduled_transport", "road_routing"])),
            "require_observed_evidence": bool(expansion.get("require_observed_evidence", True)),
        },
    }


def _date_flexibility(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "mode": "fully_flexible",
            "outbound_mode": "fully_flexible",
            "return_mode": "fully_flexible",
        }
    if not isinstance(raw, dict):
        raise MaterializationError("date_flexibility must be an object")
    shared_mode = raw.get("mode", "bounded_window")
    if shared_mode not in {"exact_dates", "bounded_window", "fully_flexible", "independent"}:
        raise MaterializationError("date_flexibility.mode is unsupported")
    outbound_mode = raw.get("outbound_mode", shared_mode)
    return_mode = raw.get("return_mode", shared_mode)
    if shared_mode == "independent" and ("outbound_mode" not in raw or "return_mode" not in raw):
        raise MaterializationError("independent date flexibility requires outbound_mode and return_mode")
    if outbound_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise MaterializationError("date_flexibility.outbound_mode is unsupported")
    if return_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise MaterializationError("date_flexibility.return_mode is unsupported")
    result = {
        "mode": outbound_mode if outbound_mode == return_mode else "independent",
        "outbound_mode": outbound_mode,
        "return_mode": return_mode,
    }
    for field in ("outbound_exact_departure_dates", "return_exact_departure_dates"):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
                raise MaterializationError(f"date_flexibility.{field} must be a non-empty date array")
            normalized = [date.fromisoformat(item).isoformat() for item in value]
            if len(normalized) != len(set(normalized)):
                raise MaterializationError(f"date_flexibility.{field} contains duplicates")
            result[field] = normalized
    if outbound_mode == "exact_dates" and "outbound_exact_departure_dates" not in result:
        raise MaterializationError("outbound exact_dates requires outbound_exact_departure_dates")
    if return_mode == "exact_dates" and "return_exact_departure_dates" not in result:
        raise MaterializationError("return exact_dates requires return_exact_departure_dates")
    return result


def _optional_money(raw: Any, field: str, currency: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {"amount": raw, "currency": currency}
    if not isinstance(raw, dict) or raw.get("currency") != currency:
        raise MaterializationError(f"{field} must use base currency")
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise MaterializationError(f"{field}.amount must be non-negative")
    return {"amount": float(amount), "currency": currency}


def _optional_nonnegative_integer(raw: Any, field: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise MaterializationError(f"{field} must be a non-negative integer")
    return raw


def _baggage_policy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError("baggage_policy must be an object")
    required_items = raw.get("required_items")
    if not isinstance(required_items, list) or not all(
        isinstance(item, str) and item.strip() for item in required_items
    ):
        raise MaterializationError("baggage_policy.required_items must be an array of non-empty strings")
    normalized = [item.strip() for item in required_items]
    if len(normalized) != len(set(normalized)):
        raise MaterializationError("baggage_policy.required_items contains duplicates")
    paid_baggage_allowed = raw.get("paid_baggage_allowed", False)
    if not isinstance(paid_baggage_allowed, bool):
        raise MaterializationError(
            "baggage_policy.paid_baggage_allowed must be boolean"
        )
    return {
        "required_items": normalized,
        "paid_baggage_allowed": paid_baggage_allowed,
    }


def _parse_date(value: Any, field: str) -> date:
    raw = _text(value, field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise MaterializationError(f"{field} must be an ISO date") from exc


def _window(
    raw: Any,
    field: str,
    timezone_name: str | None,
    utc_offset_fallback: str | None = None,
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise MaterializationError(f"{field} must be an object")
    start_value = raw.get("start")
    end_value = raw.get("end")
    zone = None

    def local_zone() -> Any:
        nonlocal zone
        if zone is not None:
            return zone
        if timezone_name in {"UTC", "Etc/UTC"}:
            zone = datetime.fromisoformat("2000-01-01T00:00:00+00:00").tzinfo
            return zone
        if timezone_name is not None:
            try:
                zone = ZoneInfo(timezone_name)
                return zone
            except ZoneInfoNotFoundError:
                pass
        if utc_offset_fallback is not None:
            try:
                zone = datetime.fromisoformat(
                    "2000-01-01T00:00:00" + utc_offset_fallback
                ).tzinfo
                return zone
            except ValueError:
                pass
        raise MaterializationError(
            f"{field} date-only values require a resolvable IANA timezone"
        )

    def normalize(value: Any, item_field: str, *, end: bool) -> datetime:
        text = _text(value, item_field)
        if "T" not in text:
            parsed = datetime.combine(
                _parse_date(text, item_field),
                time.max if end else time.min,
                local_zone(),
            )
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MaterializationError(
                    f"{item_field} must be an ISO date or offset datetime"
                ) from exc
        if parsed.tzinfo is None and zone is not None:
            parsed = parsed.replace(tzinfo=zone)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise MaterializationError(f"{item_field} must resolve to a UTC-offset datetime")
        return parsed

    start = normalize(start_value, f"{field}.start", end=False)
    end = normalize(end_value, f"{field}.end", end=True)
    if end < start:
        raise MaterializationError(f"{field}.end must not precede start")
    return {"start": start.isoformat(), "end": end.isoformat()}


def materialize_intent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError("intent must be an object")
    if raw.get("intent_version", VERSION) != VERSION:
        raise MaterializationError(f"intent_version must be {VERSION}")
    if raw.get("trip_scope") != "round_trip" or raw.get("trip_mode") != "fixed_destination":
        raise MaterializationError("intent must be a fixed_destination round_trip")
    currency = _text(raw.get("currency"), "currency").upper()
    origin = _side(raw.get("origin"), "origin", currency)
    destination = _side(raw.get("destination"), "destination", currency)
    origin_timezones = {
        str(node.get("timezone")) for node in origin["anchor_nodes"]
    }
    destination_timezones = {
        str(node.get("timezone")) for node in destination["anchor_nodes"]
    }
    if len(origin_timezones) != 1 or len(destination_timezones) != 1:
        raise MaterializationError(
            "date-window Anchors on one side must share one timezone"
        )
    origin_timezone = next(iter(origin_timezones))
    destination_timezone = next(iter(destination_timezones))
    origin_offsets = {
        str(node.get("utc_offset"))
        for node in origin["anchor_nodes"]
        if node.get("utc_offset") is not None
    }
    destination_offsets = {
        str(node.get("utc_offset"))
        for node in destination["anchor_nodes"]
        if node.get("utc_offset") is not None
    }
    origin_offset = next(iter(origin_offsets)) if len(origin_offsets) == 1 else None
    destination_offset = (
        next(iter(destination_offsets)) if len(destination_offsets) == 1 else None
    )
    travel_window = raw.get("travel_window")
    outbound_raw = raw.get("outbound_departure_window", travel_window)
    return_raw = raw.get("return_departure_window", travel_window)
    if outbound_raw is None or return_raw is None:
        raise MaterializationError("provide travel_window or both directional departure windows")
    outbound_window = _window(
        outbound_raw,
        "outbound_departure_window",
        origin_timezone,
        origin_offset,
    )
    return_window = _window(
        return_raw,
        "return_departure_window",
        destination_timezone,
        destination_offset,
    )
    minimum_stay_nights = _optional_nonnegative_integer(raw.get("minimum_stay_nights"), "minimum_stay_nights")
    maximum_stay_nights = _optional_nonnegative_integer(raw.get("maximum_stay_nights"), "maximum_stay_nights")
    if minimum_stay_nights is not None and maximum_stay_nights is not None and minimum_stay_nights > maximum_stay_nights:
        raise MaterializationError("minimum_stay_nights must not exceed maximum_stay_nights")
    travel_windows_raw = raw.get("travel_windows")
    travel_windows = []
    if isinstance(travel_windows_raw, list) and travel_windows_raw:
        for index, item in enumerate(travel_windows_raw):
            if not isinstance(item, dict):
                raise MaterializationError(
                    f"travel_windows[{index}] must be an object"
                )
            minimum = _optional_nonnegative_integer(
                item.get("minimum_stay_nights"),
                f"travel_windows[{index}].minimum_stay_nights",
            )
            maximum = _optional_nonnegative_integer(
                item.get("maximum_stay_nights"),
                f"travel_windows[{index}].maximum_stay_nights",
            )
            if minimum is not None and maximum is not None and minimum > maximum:
                raise MaterializationError(
                    f"travel_windows[{index}] minimum_stay_nights must not exceed maximum_stay_nights"
                )
            travel_windows.append(
                {
                    "travel_window_id": _text(
                        item.get("travel_window_id"),
                        f"travel_windows[{index}].travel_window_id",
                    ),
                    "outbound_departure_window": _window(
                        item.get("outbound_departure_window"),
                        f"travel_windows[{index}].outbound_departure_window",
                        origin_timezone,
                        origin_offset,
                    ),
                    "return_departure_window": _window(
                        item.get("return_departure_window"),
                        f"travel_windows[{index}].return_departure_window",
                        destination_timezone,
                        destination_offset,
                    ),
                    "minimum_stay_nights": minimum,
                    "maximum_stay_nights": maximum,
                    **(
                        {"label": _text(item.get("label"), f"travel_windows[{index}].label")}
                        if item.get("label") is not None
                        else {}
                    ),
                }
            )
    else:
        travel_windows = [
            {
                "travel_window_id": "travel-window-primary",
                "outbound_departure_window": copy.deepcopy(outbound_window),
                "return_departure_window": copy.deepcopy(return_window),
                "minimum_stay_nights": minimum_stay_nights,
                "maximum_stay_nights": maximum_stay_nights,
            }
        ]
    all_nodes: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for node in [*origin["anchor_nodes"], *destination["anchor_nodes"], *origin["seed_gateway_nodes"], *destination["seed_gateway_nodes"]]:
        existing = seen.get(node["node_id"])
        if existing is not None:
            try:
                reconcile_node(existing, node)
            except CanonicalValueError as exc:
                raise MaterializationError(str(exc)) from exc
        if existing is None:
            seen[node["node_id"]] = node
            all_nodes.append(node)
    allowed_modes = raw.get("allowed_modes", MODES)
    if not isinstance(allowed_modes, list) or not allowed_modes or not set(allowed_modes) <= set(MODES):
        raise MaterializationError("allowed_modes is invalid")
    buffers = dict(DEFAULT_BUFFERS)
    buffers.update(raw.get("mode_transition_buffers_minutes", {}))
    date_flexibility = _date_flexibility(raw.get("date_flexibility"))
    traveler_count = raw.get("traveler_count")
    if isinstance(traveler_count, bool) or not isinstance(traveler_count, int) or traveler_count < 1:
        raise MaterializationError("traveler_count must be a positive integer")
    baggage_policy = _baggage_policy(raw.get("baggage_policy"))
    self_transfer_policy = raw.get("self_transfer_policy")
    if self_transfer_policy not in {"allow", "never"}:
        raise MaterializationError("self_transfer_policy is unsupported")
    overnight_allowed = raw.get("overnight_allowed")
    if not isinstance(overnight_allowed, bool):
        raise MaterializationError("overnight_allowed must be boolean")
    maximum_outbound_duration_minutes = _optional_nonnegative_integer(
        raw.get("maximum_outbound_duration_minutes"),
        "maximum_outbound_duration_minutes",
    )
    maximum_return_duration_minutes = _optional_nonnegative_integer(
        raw.get("maximum_return_duration_minutes"),
        "maximum_return_duration_minutes",
    )
    if maximum_outbound_duration_minutes == 0:
        raise MaterializationError(
            "maximum_outbound_duration_minutes must be a positive integer"
        )
    if maximum_return_duration_minutes == 0:
        raise MaterializationError(
            "maximum_return_duration_minutes must be a positive integer"
        )
    airport_transit_unknown_policy = raw.get("airport_transit_unknown_policy", "allow_with_warning")
    if airport_transit_unknown_policy not in {"allow_with_warning", "reject"}:
        raise MaterializationError("airport_transit_unknown_policy is unsupported")
    accommodation_cost_policy = raw.get(
        "accommodation_cost_policy", "exclude_from_ranking"
    )
    if accommodation_cost_policy not in {
        "exclude_from_ranking",
        "include_required",
    }:
        raise MaterializationError(
            "accommodation_cost_policy is unsupported"
        )
    scenario: dict[str, Any] = {
        "scenario_version": VERSION,
        "base_currency": currency,
        "intent": {
            "intent_id": _text(raw.get("intent_id"), "intent_id"),
            "origin_anchor_nodes": [item["node_id"] for item in origin["anchor_nodes"]],
            "destination_anchor_nodes": [item["node_id"] for item in destination["anchor_nodes"]],
            "origin_pool": {key: value for key, value in origin.items() if key not in {"anchor_nodes", "seed_gateway_nodes"}} | {"anchor_node_ids": [item["node_id"] for item in origin["anchor_nodes"]], "seed_gateway_node_ids": [item["node_id"] for item in origin["seed_gateway_nodes"]]},
            "destination_pool": {key: value for key, value in destination.items() if key not in {"anchor_nodes", "seed_gateway_nodes"}} | {"anchor_node_ids": [item["node_id"] for item in destination["anchor_nodes"]], "seed_gateway_node_ids": [item["node_id"] for item in destination["seed_gateway_nodes"]]},
            "outbound_departure_window": outbound_window,
            "return_departure_window": return_window,
            "travel_windows": travel_windows,
            "traveler_count": traveler_count,
            "baggage_policy": baggage_policy,
            "accommodation_cost_policy": accommodation_cost_policy,
            "date_flexibility": date_flexibility,
            "budget": _optional_money(raw.get("budget"), "budget", currency),
            "overnight_allowed": overnight_allowed,
            "self_transfer_policy": self_transfer_policy,
            "search_effort": normalize_search_effort(raw.get("search_effort")),
            "maximum_outbound_duration_minutes": maximum_outbound_duration_minutes,
            "maximum_return_duration_minutes": maximum_return_duration_minutes,
            "airport_transit_unknown_policy": airport_transit_unknown_policy,
            "allowed_modes": allowed_modes,
            "mode_transition_buffers_minutes": buffers,
            "max_portfolio_offers": int(raw.get("max_portfolio_offers", 8)),
            "max_solver_offers": int(raw.get("max_solver_offers", 24)),
        },
        "nodes": all_nodes,
        "transport_entitlements": list(raw.get("transport_entitlements", [])),
    }
    if raw.get("fx_snapshot") is not None:
        scenario["fx_snapshot"] = raw["fx_snapshot"]
    if minimum_stay_nights is not None:
        scenario["intent"]["minimum_stay_nights"] = minimum_stay_nights
    if maximum_stay_nights is not None:
        scenario["intent"]["maximum_stay_nights"] = maximum_stay_nights
    return scenario


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_atomic(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args(argv)
    try:
        scenario = materialize_intent(read_json(args.intent))
        write_json_atomic(args.scenario, scenario)
        print(json.dumps({"valid": True, "scenario": _display_path(args.scenario), "airport_nodes_preselected": sum(item["node_type"] == "airport" for item in scenario["nodes"])}))
        return 0
    except (OSError, json.JSONDecodeError, MaterializationError) as exc:
        print(f"round_trip_intent_materializer: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
