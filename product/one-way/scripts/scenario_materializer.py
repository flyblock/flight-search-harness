#!/usr/bin/env python3
"""Materialize an AI-interpreted one-way intent into engine-ready JSON files.

This module is deliberately not a browser client and contains no world transport
catalog.  The AI (or another upstream catalog) supplies region seed nodes; this
module validates that bounded intent and writes deterministic planner inputs.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flight_search_harness.search_effort import normalize_search_effort


VERSION = "0.7.1"
TRIP_SCOPE = "one_way"
TRIP_MODE = "fixed_destination"
DEFAULT_QUERY_BUDGET = 24
MODE_TRANSITION_BUFFER_KEYS = (
    "surface_to_flight",
    "self_transfer_to_flight",
    "flight_to_flight",
    "flight_to_surface",
    "surface_to_surface",
)
DEFAULT_MODE_TRANSITION_BUFFERS = {
    "surface_to_flight": 120,
    "self_transfer_to_flight": 120,
    "flight_to_flight": 60,
    "flight_to_surface": 0,
    "surface_to_surface": 0,
}


def _display_path(path: Path) -> str:
    try:
        relative = os.path.relpath(path.resolve(), Path.cwd().resolve())
    except ValueError:
        return path.resolve().name
    return Path(relative).as_posix()


DEFAULT_ALLOWED_MODES = [
    "flight",
    "rail",
    "bus",
    "coach",
    "ferry",
    "local_transit",
    "walk",
    "rideshare",
    "car",
]
GROUND_MODES = [mode for mode in DEFAULT_ALLOWED_MODES if mode != "flight"]
NODE_TYPES = {
    "airport",
    "railway_station",
    "bus_terminal",
    "ferry_terminal",
    "city_destination",
}
STRATEGIES = {
    "lowest_price",
    "shortest_time",
    "convenience",
    "balanced",
    "extreme_savings",
}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class MaterializationError(ValueError):
    """Raised when structured intent cannot be materialized safely."""


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read JSON {path}: {exc}") from exc


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise MaterializationError(f"{field} must be non-empty")
    return result


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field)
    if any(character.isspace() for character in url):
        raise MaterializationError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise MaterializationError(
            f"{field} must be an absolute HTTP(S) URL"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise MaterializationError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MaterializationError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def _identifier(value: Any, field: str) -> str:
    result = _text(value, field).lower()
    if not ID_PATTERN.fullmatch(result):
        raise MaterializationError(
            f"{field} must contain only lowercase letters, digits, '.', '_' or '-'"
        )
    return result


def _currency(value: Any, field: str) -> str:
    result = _text(value, field).upper()
    if not CURRENCY_PATTERN.fullmatch(result):
        raise MaterializationError(f"{field} must be a three-letter currency code")
    return result


def _number(value: Any, field: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise MaterializationError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MaterializationError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < minimum:
        raise MaterializationError(f"{field} must be at least {minimum}")
    return result


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise MaterializationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MaterializationError(f"{field} must be an integer") from exc
    if result != value and not (isinstance(value, str) and str(result) == value.strip()):
        raise MaterializationError(f"{field} must be an integer")
    if result < minimum:
        raise MaterializationError(f"{field} must be at least {minimum}")
    return result


def _mode_transition_buffers(raw: Any) -> dict[str, int]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MaterializationError("mode_transition_buffers_minutes must be an object")
    unknown = sorted(set(raw) - set(MODE_TRANSITION_BUFFER_KEYS))
    if unknown:
        raise MaterializationError(
            "mode_transition_buffers_minutes has unsupported keys: "
            + ", ".join(unknown)
        )
    result = dict(DEFAULT_MODE_TRANSITION_BUFFERS)
    for key, value in raw.items():
        result[key] = _integer(
            value, f"mode_transition_buffers_minutes.{key}", minimum=0
        )
    return result


def _unique_strings(
    value: Any,
    field: str,
    *,
    allowed: set[str] | None = None,
    upper: bool = False,
    required: bool = False,
) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise MaterializationError(f"{field} must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        normalized = _text(item, f"{field}[{index}]")
        normalized = normalized.upper() if upper else normalized.lower()
        if allowed is not None and normalized not in allowed:
            raise MaterializationError(f"{field}[{index}] has unsupported value {normalized}")
        if normalized not in result:
            result.append(normalized)
    if required and not result:
        raise MaterializationError(f"{field} must not be empty")
    return result


def _iso_datetime(value: Any, field: str, *, end_of_day: bool) -> str:
    raw = _text(value, field)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError as exc:
            raise MaterializationError(f"{field} must be a valid ISO date") from exc
        boundary = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        parsed = datetime.combine(parsed_date, boundary, timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MaterializationError(f"{field} must be an ISO date or date-time") from exc
        if parsed.tzinfo is None:
            raise MaterializationError(f"{field} date-time must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _money(value: Any, field: str, currency: str, default_amount: float) -> dict[str, Any]:
    if value is None:
        return {"amount": default_amount, "currency": currency}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"amount": _number(value, field), "currency": currency}
    if not isinstance(value, dict):
        raise MaterializationError(f"{field} must be an amount or money object")
    return {
        "amount": _number(value.get("amount"), f"{field}.amount"),
        "currency": _currency(value.get("currency", currency), f"{field}.currency"),
    }


def _optional_money(value: Any, field: str, currency: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _money(value, field, currency, 0)


def _timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterializationError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise MaterializationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalize_fx_snapshot(raw: Any, base_currency: str) -> dict[str, Any]:
    """Normalize source-attributed rates supplied by AI; never fetch rates here."""
    if not isinstance(raw, dict):
        raise MaterializationError("fx_snapshot must be an object")
    snapshot_base = _currency(raw.get("base_currency"), "fx_snapshot.base_currency")
    if snapshot_base != base_currency:
        raise MaterializationError(
            f"fx_snapshot.base_currency must equal currency {base_currency}"
        )
    raw_rates = raw.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        raise MaterializationError("fx_snapshot.rates must be a non-empty object")
    rates = {
        _currency(code, f"fx_snapshot.rates.{code}"): _number(
            value, f"fx_snapshot.rates.{code}", minimum=0.0000001
        )
        for code, value in raw_rates.items()
    }
    if snapshot_base not in rates:
        raise MaterializationError(
            f"fx_snapshot.rates must explicitly include {snapshot_base}"
        )
    if not math.isclose(rates[snapshot_base], 1.0, rel_tol=0, abs_tol=1e-12):
        raise MaterializationError(f"fx_snapshot.rates.{snapshot_base} must equal 1")
    source_url = _http_url(raw.get("source_url"), "fx_snapshot.source_url")
    observed_at = _timestamp(raw.get("observed_at"), "fx_snapshot.observed_at")
    expires_at = _timestamp(raw.get("expires_at"), "fx_snapshot.expires_at")
    if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.fromisoformat(
        observed_at.replace("Z", "+00:00")
    ):
        raise MaterializationError("fx_snapshot.expires_at must be after observed_at")
    return {
        "base_currency": snapshot_base,
        "rates": rates,
        "source_url": source_url,
        "provider": _text(raw.get("provider"), "fx_snapshot.provider"),
        "observed_at": observed_at,
        "expires_at": expires_at,
    }


def _normalize_node(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError(f"{field} must be an object")
    node = copy.deepcopy(raw)
    node_id = _text(node.get("node_id"), f"{field}.node_id").upper()
    node_type = _text(node.get("node_type"), f"{field}.node_type").lower()
    if node_type not in NODE_TYPES:
        raise MaterializationError(f"{field}.node_type has unsupported value {node_type}")
    node.update(
        {
            "node_id": node_id,
            "node_type": node_type,
            "location": _text(node.get("location"), f"{field}.location"),
            "country_or_region": _text(
                node.get("country_or_region"), f"{field}.country_or_region"
            ),
            "timezone": _text(node.get("timezone"), f"{field}.timezone"),
        }
    )
    if node.get("country_code") is not None:
        country_code = _text(node["country_code"], f"{field}.country_code").upper()
        if not re.fullmatch(r"[A-Z]{2}", country_code):
            raise MaterializationError(f"{field}.country_code must be a two-letter code")
        node["country_code"] = country_code
    coordinates = node.get("coordinates")
    if coordinates is not None:
        if not isinstance(coordinates, dict):
            raise MaterializationError(f"{field}.coordinates must be an object")
        latitude = _number(coordinates.get("latitude"), f"{field}.coordinates.latitude", minimum=-90)
        longitude = _number(coordinates.get("longitude"), f"{field}.coordinates.longitude", minimum=-180)
        if latitude > 90 or longitude > 180:
            raise MaterializationError(f"{field}.coordinates are outside valid bounds")
        node["coordinates"] = {"latitude": latitude, "longitude": longitude}
    node.setdefault("notes", [])
    return node


def _normalize_side(raw: Any, side: str, currency: str) -> dict[str, Any]:
    field = side
    if not isinstance(raw, dict):
        raise MaterializationError(f"{field} must be an object")
    region_id = _identifier(raw.get("region_id"), f"{field}.region_id")
    label = _text(raw.get("label"), f"{field}.label")
    policy = str(raw.get("policy", "extended_region")).lower()
    if policy not in {"exact", "city_region", "extended_region"}:
        raise MaterializationError(f"{field}.policy is unsupported")
    endpoint_policy = str(
        raw.get(
            "endpoint_policy",
            "anchor_required" if policy == "exact" else "any_admitted_gateway",
        )
    ).lower()
    if endpoint_policy not in {"anchor_required", "any_admitted_gateway"}:
        raise MaterializationError(f"{field}.endpoint_policy is unsupported")
    raw_nodes = raw.get("seed_nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise MaterializationError(f"{field}.seed_nodes must be a non-empty array")
    nodes = [
        _normalize_node(node, f"{field}.seed_nodes[{index}]")
        for index, node in enumerate(raw_nodes)
    ]
    node_ids = [node["node_id"] for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise MaterializationError(f"{field}.seed_nodes contains duplicate node_id values")
    airport_ids = [node["node_id"] for node in nodes if node["node_type"] == "airport"]
    if not airport_ids:
        raise MaterializationError(f"{field}.seed_nodes must include at least one airport")
    country_codes = _unique_strings(
        raw.get("country_codes", []),
        f"{field}.country_codes",
        upper=True,
        required=True,
    )
    if any(not re.fullmatch(r"[A-Z]{2}", code) for code in country_codes):
        raise MaterializationError(f"{field}.country_codes must contain two-letter codes")
    ground_modes = _unique_strings(
        raw.get("allowed_ground_modes", GROUND_MODES),
        f"{field}.allowed_ground_modes",
        allowed=set(GROUND_MODES),
        required=True,
    )
    expansion = raw.get("expansion", {})
    if not isinstance(expansion, dict):
        raise MaterializationError(f"{field}.expansion must be an object")
    expansion_enabled = bool(expansion.get("enabled", policy != "exact"))
    if policy == "exact" and expansion_enabled:
        raise MaterializationError(f"{field}.exact pool cannot enable expansion")
    provider_kinds = _unique_strings(
        expansion.get(
            "provider_kinds",
            ["browser_search", "region_catalog", "scheduled_transport", "road_routing"],
        ),
        f"{field}.expansion.provider_kinds",
        allowed={"browser_search", "region_catalog", "scheduled_transport", "road_routing"},
    )
    max_time_field = (
        "max_access_time_minutes" if side == "origin" else "max_egress_time_minutes"
    )
    other_time_field = (
        "max_egress_time_minutes" if side == "origin" else "max_access_time_minutes"
    )
    primary_raw = raw.get(max_time_field)
    other_raw = raw.get(other_time_field)
    primary_time = (
        None
        if primary_raw is None
        else _integer(primary_raw, f"{field}.{max_time_field}")
    )
    other_time = (
        None
        if other_raw is None
        else _integer(other_raw, f"{field}.{other_time_field}")
    )
    return {
        "region_id": region_id,
        "label": label,
        "country_codes": country_codes,
        "nodes": nodes,
        "node_ids": node_ids,
        "airport_ids": airport_ids,
        "endpoint_policy": endpoint_policy,
        "pool": {
            "pool_id": f"{region_id}-{side}",
            "label": label,
            "policy": policy,
            "endpoint_policy": endpoint_policy,
            "seed_nodes": node_ids,
            "region_hint": {"region_id": region_id, "label": label},
            "max_access_time_minutes": primary_time if side == "origin" else other_time,
            "max_egress_time_minutes": primary_time if side == "destination" else other_time,
            "max_cost": _optional_money(
                raw.get("max_ground_cost"), f"{field}.max_ground_cost", currency
            ),
            "allowed_modes": ground_modes,
            "expansion_by_provider": {
                "enabled": expansion_enabled,
                "provider_kinds": provider_kinds,
                "result_scope": (
                    "exact_seed_nodes"
                    if policy == "exact"
                    else "within_named_region"
                    if policy == "city_region"
                    else "within_constraints"
                ),
                "require_observed_evidence": True,
            },
            "notes": [
                "Seed nodes came from the structured AI intent; provider expansion requires observed evidence."
            ],
        },
    }


def validate_intent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaterializationError("intent must be an object")
    intent = copy.deepcopy(raw)
    intent_version = str(intent.get("intent_version", VERSION))
    if intent_version != VERSION:
        raise MaterializationError(f"intent_version must be {VERSION}")
    trip_scope = str(intent.get("trip_scope", TRIP_SCOPE)).lower()
    trip_mode = str(intent.get("trip_mode", TRIP_MODE)).lower()
    if trip_scope != TRIP_SCOPE:
        raise MaterializationError(f"trip_scope must be {TRIP_SCOPE}")
    if trip_mode != TRIP_MODE:
        raise MaterializationError(f"trip_mode must be {TRIP_MODE}")
    intent_id = _identifier(intent.get("intent_id"), "intent_id")
    currency = _currency(intent.get("currency"), "currency")
    origin = _normalize_side(intent.get("origin"), "origin", currency)
    destination = _normalize_side(intent.get("destination"), "destination", currency)
    duplicate_nodes = sorted(set(origin["node_ids"]) & set(destination["node_ids"]))
    if duplicate_nodes:
        raise MaterializationError(
            "origin and destination seed node ids overlap: " + ", ".join(duplicate_nodes)
        )
    window = intent.get("date_window")
    if not isinstance(window, dict):
        raise MaterializationError("date_window must be an object")
    start = _iso_datetime(window.get("start"), "date_window.start", end_of_day=False)
    end = _iso_datetime(window.get("end"), "date_window.end", end_of_day=True)
    if datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.fromisoformat(
        start.replace("Z", "+00:00")
    ):
        raise MaterializationError("date_window.end must be after date_window.start")
    raw_date_flexibility = intent.get(
        "date_flexibility", {"mode": "bounded_window"}
    )
    if not isinstance(raw_date_flexibility, dict):
        raise MaterializationError("date_flexibility must be an object")
    date_flexibility = copy.deepcopy(raw_date_flexibility)
    date_mode = str(date_flexibility.get("mode", "bounded_window")).lower()
    if date_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise MaterializationError(
            "date_flexibility.mode must be exact_dates, bounded_window, or fully_flexible"
        )
    date_flexibility["mode"] = date_mode
    if date_mode == "exact_dates":
        raw_dates = date_flexibility.get(
            "exact_departure_dates", date_flexibility.get("dates")
        )
        if not isinstance(raw_dates, list) or not raw_dates:
            raise MaterializationError("exact_dates requires exact_departure_dates")
        normalized_dates: list[str] = []
        start_date = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        end_date = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        for index, value in enumerate(raw_dates):
            raw_value = str(value).strip()
            try:
                exact = datetime.strptime(raw_value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise MaterializationError(
                    f"exact_departure_dates[{index}] must use YYYY-MM-DD"
                ) from exc
            if exact < start_date or exact > end_date:
                raise MaterializationError(
                    f"exact_departure_dates[{index}] is outside date_window"
                )
            normalized = exact.isoformat()
            if normalized not in normalized_dates:
                normalized_dates.append(normalized)
        date_flexibility = {
            "mode": "exact_dates",
            "exact_departure_dates": normalized_dates,
        }
    raw_query_budget = intent.get("query_budget")
    query_budget = (
        None
        if raw_query_budget is None
        else _integer(raw_query_budget, "query_budget", minimum=1)
    )
    allowed_modes = _unique_strings(
        intent.get("allowed_modes", DEFAULT_ALLOWED_MODES),
        "allowed_modes",
        allowed=set(DEFAULT_ALLOWED_MODES),
        required=True,
    )
    if "flight" not in allowed_modes:
        raise MaterializationError("allowed_modes must include flight")
    for side_name, side_value in (("origin", origin), ("destination", destination)):
        pool_modes = set(side_value["pool"]["allowed_modes"])
        unsupported_pool_modes = sorted(pool_modes - set(allowed_modes))
        if unsupported_pool_modes:
            raise MaterializationError(
                f"{side_name}.allowed_ground_modes is outside allowed_modes: "
                + ", ".join(unsupported_pool_modes)
            )
    strategy = str(intent.get("strategy", "lowest_price")).lower()
    if strategy not in STRATEGIES:
        raise MaterializationError(f"strategy has unsupported value {strategy}")
    raw_entitlements = intent.get("transport_entitlements", [])
    if not isinstance(raw_entitlements, list):
        raise MaterializationError("transport_entitlements must be an array")
    entitlements = copy.deepcopy(raw_entitlements)
    for index, entitlement in enumerate(entitlements):
        if not isinstance(entitlement, dict):
            raise MaterializationError(f"transport_entitlements[{index}] must be an object")
        _text(entitlement.get("entitlement_id"), f"transport_entitlements[{index}].entitlement_id")
    traveler_count = _integer(intent.get("traveler_count"), "traveler_count", minimum=1)
    baggage = intent.get("baggage")
    if not isinstance(baggage, dict):
        raise MaterializationError("baggage must be an object")
    baggage = copy.deepcopy(baggage)
    baggage["mode"] = _text(baggage.get("mode"), "baggage.mode")
    if not isinstance(baggage.get("paid_baggage"), bool):
        raise MaterializationError("baggage.paid_baggage must be boolean")
    accommodation_cost_policy = str(
        intent.get("accommodation_cost_policy", "exclude_from_ranking")
    ).lower()
    if accommodation_cost_policy not in {
        "include_required",
        "exclude_from_optimization",
        "exclude_from_ranking",
    }:
        raise MaterializationError(
            "accommodation_cost_policy is unsupported"
        )
    budget = _optional_money(intent.get("budget"), "budget", currency)
    currency_decimal_places = _integer(
        intent.get("currency_decimal_places"),
        "currency_decimal_places",
        minimum=0,
    )
    if currency_decimal_places > 6:
        raise MaterializationError("currency_decimal_places must not exceed 6")
    fx_snapshot = (
        _normalize_fx_snapshot(intent["fx_snapshot"], currency)
        if intent.get("fx_snapshot") is not None
        else None
    )
    fx_raw = intent.get("fx_to_base")
    if fx_raw is None:
        fx_raw = fx_snapshot["rates"] if fx_snapshot is not None else {currency: 1}
    if not isinstance(fx_raw, dict) or not fx_raw:
        raise MaterializationError("fx_to_base must be a non-empty object")
    fx_to_base = {
        _currency(code, f"fx_to_base.{code}"): _number(
            value, f"fx_to_base.{code}", minimum=0.0000001
        )
        for code, value in fx_raw.items()
    }
    fx_to_base.setdefault(currency, 1.0)
    if fx_snapshot is not None and (
        set(fx_to_base) != set(fx_snapshot["rates"])
        or any(
            not math.isclose(
                fx_to_base[code], fx_snapshot["rates"][code], rel_tol=0, abs_tol=1e-12
            )
            for code in fx_to_base
        )
    ):
        raise MaterializationError(
            "fx_to_base must match fx_snapshot.rates when both are present"
        )
    money_values = [
        budget,
        origin["pool"]["max_cost"],
        destination["pool"]["max_cost"],
    ]
    money_currencies = {
        value["currency"] for value in money_values if value is not None
    }
    missing_rates = sorted(money_currencies - set(fx_to_base))
    if missing_rates:
        raise MaterializationError(
            "fx_to_base is missing explicit rates for: " + ", ".join(missing_rates)
        )
    require_fresh_fx_snapshot = intent.get("require_fresh_fx_snapshot", False)
    if not isinstance(require_fresh_fx_snapshot, bool):
        raise MaterializationError("require_fresh_fx_snapshot must be boolean")
    if require_fresh_fx_snapshot and fx_snapshot is None:
        raise MaterializationError(
            "require_fresh_fx_snapshot requires a source-attributed fx_snapshot"
        )
    fx_as_of = (
        _timestamp(intent["fx_as_of"], "fx_as_of")
        if intent.get("fx_as_of") is not None
        else None
    )
    return {
        "intent_version": VERSION,
        "intent_id": intent_id,
        "profile_name": _text(
            intent.get("profile_name", f"{origin['label']} → {destination['label']}"),
            "profile_name",
        ),
        "trip_scope": TRIP_SCOPE,
        "trip_mode": TRIP_MODE,
        "currency": currency,
        "currency_decimal_places": currency_decimal_places,
        "traveler_count": traveler_count,
        "origin": origin,
        "destination": destination,
        "date_window": {"start": start, "end": end},
        "date_flexibility": date_flexibility,
        "query_budget": query_budget,
        "search_effort": normalize_search_effort(intent.get("search_effort")),
        "maximum_total_duration_minutes": (
            _integer(
                intent["maximum_total_duration_minutes"],
                "maximum_total_duration_minutes",
                minimum=1,
            )
            if intent.get("maximum_total_duration_minutes") is not None
            else None
        ),
        "maximum_transfer_count": (
            _integer(
                intent["maximum_transfer_count"],
                "maximum_transfer_count",
                minimum=0,
            )
            if intent.get("maximum_transfer_count") is not None
            else None
        ),
        "allowed_modes": allowed_modes,
        "strategy": strategy,
        "budget": budget,
        "baggage": baggage,
        "accommodation_cost_policy": accommodation_cost_policy,
        "transport_entitlements": entitlements,
        "fx_to_base": fx_to_base,
        "fx_snapshot": fx_snapshot,
        "require_fresh_fx_snapshot": require_fresh_fx_snapshot,
        "fx_as_of": fx_as_of,
        "overnight_allowed": intent.get("overnight_allowed"),
        "self_transfer_policy": str(intent.get("self_transfer_policy") or "").lower(),
        "mode_transition_buffers_minutes": _mode_transition_buffers(
            intent.get("mode_transition_buffers_minutes")
        ),
    }


def materialize_intent(raw: Any) -> dict[str, Any]:
    intent = validate_intent(raw)
    advisory_query_budget = intent["query_budget"] or DEFAULT_QUERY_BUDGET
    if not isinstance(intent["overnight_allowed"], bool):
        raise MaterializationError("overnight_allowed must be boolean")
    if intent["self_transfer_policy"] not in {"allow", "never"}:
        raise MaterializationError("self_transfer_policy must be allow or never")
    origin = intent["origin"]
    destination = intent["destination"]
    nodes = [*copy.deepcopy(origin["nodes"]), *copy.deepcopy(destination["nodes"])]
    profile = {
        "profile_version": VERSION,
        "profile_id": f"profile-{intent['intent_id']}",
        "profile_name": intent["profile_name"],
        "status": "confirmed",
        "trip_mode": TRIP_MODE,
        "trip_scope": TRIP_SCOPE,
        "origin_region": {
            "region_id": origin["region_id"],
            "label": origin["label"],
            "country_codes": origin["country_codes"],
        },
        "destination_region": {
            "region_id": destination["region_id"],
            "label": destination["label"],
            "country_codes": destination["country_codes"],
        },
        "origin_pool": copy.deepcopy(origin["pool"]),
        "destination_pool": copy.deepcopy(destination["pool"]),
        "origin_start_nodes": list(origin["airport_ids"]),
        "destination_end_nodes": list(destination["airport_ids"]),
        "origin_airport_nodes": list(origin["airport_ids"]),
        "destination_airport_nodes": list(destination["airport_ids"]),
        "endpoint_policy": {
            "origin": origin["endpoint_policy"],
            "destination": destination["endpoint_policy"],
        },
        "allowed_modes": list(intent["allowed_modes"]),
        "traveler_count": intent["traveler_count"],
        "date_window": copy.deepcopy(intent["date_window"]),
        "date_flexibility": copy.deepcopy(intent["date_flexibility"]),
        "budget": copy.deepcopy(intent["budget"]),
        "baggage": copy.deepcopy(intent["baggage"]),
        "accommodation_cost_policy": intent["accommodation_cost_policy"],
        "search_effort": intent["search_effort"],
        "transport_entitlements": copy.deepcopy(intent["transport_entitlements"]),
        "overnight_allowed": intent["overnight_allowed"],
        "self_transfer_policy": intent["self_transfer_policy"],
        "scoring_preferences": {"primary_strategy": intent["strategy"]},
        "currency": intent["currency"],
        "notes": [
            "Materialized from a structured AI intent; no browser operation or route claim was performed by this script.",
            "Seed nodes are search bounds, not evidence that a transport offer exists.",
        ],
    }
    config = {
        "product_version": VERSION,
        "trip_mode": TRIP_MODE,
        "trip_scope": TRIP_SCOPE,
        "base_currency": intent["currency"],
        "currency_decimal_places": intent["currency_decimal_places"],
        "accommodation_cost_policy": intent["accommodation_cost_policy"],
        "search_effort": intent["search_effort"],
        "fx_to_base": copy.deepcopy(intent["fx_to_base"]),
        "profile_file": "profile.json",
        "nodes_file": "nodes.json",
        "edges_file": "edges.json",
        "search": {
            "max_total_travel_minutes": intent["maximum_total_duration_minutes"],
            "max_transfers": intent["maximum_transfer_count"],
            "max_routes": None,
            "labels_per_node": None,
            "reference_cost": (
                round(
                    intent["budget"]["amount"]
                    * intent["fx_to_base"][intent["budget"]["currency"]],
                    8,
                )
                if intent["budget"] is not None
                else None
            ),
            "connector_probe_limit": advisory_query_budget,
            "cost_relaxation_factor": 1.2,
            "max_connector_gap_km": 3000,
            "minimum_connection_minutes": 0,
            "mode_transition_buffers_minutes": copy.deepcopy(
                intent["mode_transition_buffers_minutes"]
            ),
        },
        "frontier_search": {
            "search_intensity": (
                "extreme_exploration"
                if intent["strategy"] == "extreme_savings"
                else "deep_savings"
            ),
            "query_budget": advisory_query_budget,
            "allowed_connector_modes": list(intent["allowed_modes"]),
        },
        "temporal_corridor_search": {
            "schema_version": VERSION,
            "query_budget": advisory_query_budget,
        },
        "live_provider_session": {
            "source_selection_mode": "ai_led",
            "require_fresh_fx_snapshot": intent["require_fresh_fx_snapshot"],
            **(
                {"fx_as_of": intent["fx_as_of"]}
                if intent["fx_as_of"] is not None
                else {}
            ),
        },
        "materialization": {
            "intent_id": intent["intent_id"],
            "intent_version": VERSION,
            "source_kind": "ai_structured_intent",
            "browser_used": False,
            "world_catalog_embedded": False,
        },
    }
    if intent["fx_snapshot"] is not None:
        config["fx_snapshot"] = copy.deepcopy(intent["fx_snapshot"])
    return {
        "profile": profile,
        "config": config,
        "nodes": {
            "intent_id": intent["intent_id"],
            "materialized": True,
            "nodes": nodes,
        },
        "edges": {
            "intent_id": intent["intent_id"],
            "materialized": True,
            "edges": [],
        },
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_artifacts(raw: Any, output_dir: str | Path, *, force: bool = False) -> dict[str, str]:
    materialized = materialize_intent(raw)
    destination = Path(output_dir)
    paths = {
        "config": destination / "config.json",
        "profile": destination / "profile.json",
        "nodes": destination / "nodes.json",
        "edges": destination / "edges.json",
    }
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing and not force:
        raise MaterializationError(
            "refusing to overwrite existing artifacts: " + ", ".join(existing)
        )
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("profile", "nodes", "edges", "config"):
        _write_json_atomic(paths[name], materialized[name])
    return {name: _display_path(path) for name, path in paths.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize an AI-structured one-way intent without browsing"
    )
    parser.add_argument("--intent", required=True, help="Path to structured intent JSON")
    parser.add_argument("--output-dir", required=True, help="Directory for planner artifacts")
    parser.add_argument("--force", action="store_true", help="Replace existing artifact files")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = write_artifacts(read_json(args.intent), args.output_dir, force=args.force)
    except MaterializationError as exc:
        parser.exit(2, f"scenario_materializer: {exc}\n")
    print(json.dumps({"version": VERSION, "trip_scope": TRIP_SCOPE, "artifacts": paths}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
