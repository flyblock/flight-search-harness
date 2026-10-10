#!/usr/bin/env python3
"""Deterministic state engine for flexible multi-leg flight-loop searches."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ENGINE_VERSION = "0.6.1"
ROOT_ID = "root"
EARTH_RADIUS_KM = 6371.0088
ANNOTATION_VALUES = {"yes", "no", "unknown"}
AIRPORT_TRANSIT_UNKNOWN_POLICIES = {"allow_with_warning", "reject"}


def _load_provider_policy() -> tuple[dict[str, Any], tuple[str, ...]]:
    registry_path = Path(__file__).resolve().parents[1] / "data" / "source-registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load Provider policy: {registry_path}: {exc}") from exc
    if not isinstance(registry, dict):
        raise RuntimeError("Provider registry must be an object")
    strategy = registry.get("selection_strategy")
    if not isinstance(strategy, dict) or strategy.get("selection_mode") != "ordered":
        raise RuntimeError("Provider registry requires an ordered selection strategy")
    raw_order = strategy.get("ordered_source_ids")
    if not isinstance(raw_order, list) or not raw_order:
        raise RuntimeError("Provider registry ordered_source_ids must be non-empty")
    order = tuple(str(value).strip() for value in raw_order)
    if any(not value for value in order) or len(order) != len(set(order)):
        raise RuntimeError("Provider registry ordered_source_ids must be unique")
    if strategy.get("default_primary_source_id") != order[0]:
        raise RuntimeError("Provider registry primary source must lead ordered_source_ids")
    registered = {
        str(item.get("source_id"))
        for item in registry.get("sources", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    missing = [source_id for source_id in order if source_id not in registered]
    if missing:
        raise RuntimeError(f"Provider registry ordered sources are missing: {missing}")
    return registry, order


SOURCE_REGISTRY, STRICT_SOURCE_ORDER = _load_provider_policy()
PROVIDER_STRATEGY = SOURCE_REGISTRY["selection_strategy"]
PRIMARY_SOURCE_ID = STRICT_SOURCE_ORDER[0]
BUSINESS_CHALLENGER_SOURCE_ID = str(
    PROVIDER_STRATEGY["business_no_match_challenger_source_id"]
)
BUSINESS_CHALLENGER_REQUEST_KINDS = frozenset(
    str(value)
    for value in PROVIDER_STRATEGY["business_no_match_challenger_request_kinds"]
)
if BUSINESS_CHALLENGER_SOURCE_ID not in STRICT_SOURCE_ORDER:
    raise RuntimeError("Provider registry business challenger is outside ordered_source_ids")
ORDERED_OBSERVATION_KINDS = {
    "payable",
    "source_unavailable",
    "scope_unsupported",
    "scope_incomplete",
    "no_market_result",
    "research_only",
}
ORDERED_OBSERVATION_KIND_ALIASES = {
    "blocked": "source_unavailable",
    "market_not_covered": "scope_unsupported",
    "unknown": "scope_incomplete",
    "no_result": "no_market_result",
}
ORDERED_PENDING_KINDS = {"scope_incomplete", "research_only"}
ORDERED_TECHNICAL_FALLBACK_KINDS = {"source_unavailable", "scope_unsupported"}
ORDERED_BUSINESS_NO_MATCH_KINDS = {"no_market_result"}


def _load_airport_transit_module() -> Any:
    module_path = Path(__file__).resolve().with_name("airport_transit.py")
    spec = importlib.util.spec_from_file_location(
        "flight_search_random_loop_airport_transit", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load airport transit module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AIRPORT_TRANSIT = _load_airport_transit_module()


def _load_scope_coverage_module() -> Any:
    module_path = Path(__file__).resolve().with_name("scope_coverage.py")
    spec = importlib.util.spec_from_file_location(
        "flight_search_random_loop_scope_coverage", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load scope coverage module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCOPE_COVERAGE = _load_scope_coverage_module()


class RouteEngineError(ValueError):
    """Raised when input cannot be evaluated without guessing."""


def normalize_ordered_observation_kind(value: Any) -> str:
    kind = str(value or "").strip()
    return ORDERED_OBSERVATION_KIND_ALIASES.get(kind, kind)


def read_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RouteEngineError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RouteEngineError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RouteEngineError(f"Top-level JSON value must be an object: {path}")
    return value


def write_json(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise RouteEngineError(f"{field} must be an ISO-8601 string with UTC offset")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RouteEngineError(f"{field} is not a valid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RouteEngineError(f"{field} must include a UTC offset: {value}")
    return parsed


def iso(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def parse_local_time(value: str, field: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        parsed = time(hour=int(hour_text), minute=int(minute_text))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RouteEngineError(f"{field} must use HH:MM") from exc
    return parsed


def normalized_code(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value.strip()) != 3:
        raise RouteEngineError(f"{field} must be a three-letter airport code")
    return value.strip().upper()


def positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise RouteEngineError(f"{field} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RouteEngineError(f"{field} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise RouteEngineError(f"{field} must be a positive finite number")
    return number


def integer_value(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise RouteEngineError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RouteEngineError(f"{field} must be an integer") from exc
    if number != value or number < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise RouteEngineError(f"{field} must be a {qualifier} integer")
    return number


def validate_airport_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RouteEngineError("airports must be an object keyed by IATA code")
    result: dict[str, dict[str, Any]] = {}
    for raw_code, raw_entry in raw.items():
        code = normalized_code(raw_code, "airport catalog key")
        if not isinstance(raw_entry, dict):
            raise RouteEngineError(f"Airport {code} must be an object")
        entry = copy.deepcopy(raw_entry)
        if "latitude" in entry or "longitude" in entry:
            try:
                latitude = float(entry["latitude"])
                longitude = float(entry["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RouteEngineError(
                    f"Airport {code} requires numeric latitude and longitude"
                ) from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise RouteEngineError(f"Airport {code} has invalid coordinates")
            entry["latitude"] = latitude
            entry["longitude"] = longitude
        for annotation in ("pp_lounge", "overnight"):
            if annotation in entry:
                normalized = str(entry[annotation]).lower()
                if normalized not in ANNOTATION_VALUES:
                    raise RouteEngineError(
                        f"Airport {code} {annotation} must be yes, no, or unknown"
                    )
                entry[annotation] = normalized
        if "timezone" in entry:
            timezone_name = str(entry["timezone"])
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                entry["timezone_runtime_warning"] = (
                    "IANA timezone data is unavailable in this Python runtime; "
                    "search windows will retain the explicit offset from the observed flight."
                )
            entry["timezone"] = timezone_name
        result[code] = entry
    return result


def normalized_http_url(value: Any, field: str) -> str:
    url = str(value or "").strip()
    if not url or any(character.isspace() for character in url):
        raise RouteEngineError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RouteEngineError(f"{field} must be an absolute HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise RouteEngineError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RouteEngineError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def validate_source(raw: Any, parent_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RouteEngineError(f"source for {parent_id} must be an object")
    source = copy.deepcopy(raw)
    name = str(source.get("name", "")).strip()
    url = normalized_http_url(
        source.get("url"), f"source.url for {parent_id}"
    )
    if not name:
        raise RouteEngineError(f"source.name for {parent_id} is required")
    observed_at = parse_datetime(
        source.get("observed_at"), f"source.observed_at for {parent_id}"
    )
    source["name"] = name
    source["url"] = url
    source["observed_at"] = iso(observed_at)
    return source


def validate_airport_context(raw: Any, field: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RouteEngineError(f"{field} must be an object")
    terminal_id = str(raw.get("terminal_id", "")).strip()
    zone_id = str(raw.get("zone_id", "")).strip()
    if not terminal_id or not zone_id:
        raise RouteEngineError(f"{field} requires terminal_id and zone_id")
    return {"terminal_id": terminal_id, "zone_id": zone_id}


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(raw)
    config.pop("completed_limit", None)
    removed_advisory = [
        field for field in ("beam_width", "diversity") if field in config
    ]
    if removed_advisory:
        raise RouteEngineError(
            "advisory configuration no longer affects ordered search and is unsupported: "
            + ", ".join(removed_advisory)
        )
    if "history_limit" in config:
        raise RouteEngineError(
            "history_limit is unsupported because Session audit state is compact"
        )
    defaults = {
        "first_leg_earliest_local_time": {},
        "quality_gate": {
            "minimum_leg_efficiency_km_per_base_currency_unit": None,
            "maximum_leg_price": None,
        },
        "date_anchor_limit_per_destination": 3,
        "early_return_search": {"enabled": True},
        "search_budget": {"max_browser_queries": None},
        "source_strategy": {
            "selection_mode": "ordered",
            "primary_source_id": PRIMARY_SOURCE_ID,
            "fallback_source_ids": list(STRICT_SOURCE_ORDER[1:]),
        },
    }
    for key, value in defaults.items():
        config.setdefault(key, value)

    required_preferences = (
        "min_legs",
        "max_legs",
        "connection_buffer_minutes",
        "max_connection_wait_minutes",
        "preferred_trip_duration_hours",
        "traveler_count",
        "baggage_policy",
    )
    for key in required_preferences:
        if key not in config:
            raise RouteEngineError(f"{key} is required")

    for key in ("departure_airports", "return_airports"):
        raw_codes = config.get(key)
        if not isinstance(raw_codes, list) or not raw_codes:
            raise RouteEngineError(f"{key} must be a non-empty array")
        config[key] = [normalized_code(code, key) for code in raw_codes]

    base_currency = config.get("base_currency")
    if not isinstance(base_currency, str) or not re.fullmatch(
        r"[A-Z]{3}", base_currency.upper()
    ):
        raise RouteEngineError("base_currency must be an explicit three-letter currency code")
    config["base_currency"] = base_currency.upper()
    for key in (
        "min_legs",
        "max_legs",
        "connection_buffer_minutes",
        "max_connection_wait_minutes",
    ):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError) as exc:
            raise RouteEngineError(f"{key} must be an integer") from exc
    if config["min_legs"] < 1 or config["max_legs"] < config["min_legs"]:
        raise RouteEngineError("Require 1 <= min_legs <= max_legs")
    if config["connection_buffer_minutes"] < 0:
        raise RouteEngineError("connection_buffer_minutes cannot be negative")
    if config["max_connection_wait_minutes"] <= config["connection_buffer_minutes"]:
        raise RouteEngineError(
            "max_connection_wait_minutes must exceed connection_buffer_minutes"
        )
    config["preferred_trip_duration_hours"] = positive_number(
        config["preferred_trip_duration_hours"], "preferred_trip_duration_hours"
    )
    config["traveler_count"] = integer_value(
        config["traveler_count"], "traveler_count", minimum=1
    )
    baggage_policy = config["baggage_policy"]
    if not isinstance(baggage_policy, dict):
        raise RouteEngineError("baggage_policy must be an object")
    cabin_baggage = str(baggage_policy.get("cabin_baggage", "")).strip()
    if not cabin_baggage:
        raise RouteEngineError("baggage_policy.cabin_baggage is required")
    checked_bag_count = integer_value(
        baggage_policy.get("checked_bag_count"),
        "baggage_policy.checked_bag_count",
        minimum=0,
    )
    config["baggage_policy"] = {
        "cabin_baggage": cabin_baggage,
        "checked_bag_count": checked_bag_count,
    }

    trip_start = parse_datetime(config.get("trip_start"), "trip_start")
    trip_end = parse_datetime(config.get("trip_end"), "trip_end")
    if utc(trip_end) <= utc(trip_start):
        raise RouteEngineError("trip_end must be later than trip_start")
    config["trip_start"] = iso(trip_start)
    config["trip_end"] = iso(trip_end)

    cutoffs = config["first_leg_earliest_local_time"]
    if not isinstance(cutoffs, dict):
        raise RouteEngineError("first_leg_earliest_local_time must be an object")
    normalized_cutoffs: dict[str, Any] = {}
    for raw_code, cutoff in cutoffs.items():
        code = normalized_code(raw_code, "first-leg cutoff airport")
        if isinstance(cutoff, str):
            parsed_cutoff = parse_local_time(cutoff, f"first-leg cutoff for {code}")
            normalized_cutoffs[code] = parsed_cutoff.strftime("%H:%M")
            continue
        if not isinstance(cutoff, dict) or not cutoff:
            raise RouteEngineError(
                f"First-leg cutoff for {code} must be HH:MM or a date-to-time object"
            )
        dated_cutoffs: dict[str, str] = {}
        for raw_date, raw_time in cutoff.items():
            date_key = str(raw_date)
            if date_key != "default":
                try:
                    date_key = datetime.strptime(date_key, "%Y-%m-%d").date().isoformat()
                except ValueError as exc:
                    raise RouteEngineError(
                        f"First-leg cutoff date for {code} must use YYYY-MM-DD"
                    ) from exc
            parsed_cutoff = parse_local_time(
                raw_time, f"first-leg cutoff for {code} on {date_key}"
            )
            dated_cutoffs[date_key] = parsed_cutoff.strftime("%H:%M")
        normalized_cutoffs[code] = dated_cutoffs
    config["first_leg_earliest_local_time"] = normalized_cutoffs

    rates = config.get("fx_to_base")
    if not isinstance(rates, dict) or not rates:
        raise RouteEngineError("fx_to_base must be a non-empty object")
    normalized_rates: dict[str, float] = {}
    for currency, rate in rates.items():
        normalized_rates[str(currency).upper()] = positive_number(
            rate, f"fx_to_base.{currency}"
        )
    if config["base_currency"] not in normalized_rates:
        raise RouteEngineError(
            f"fx_to_base.{config['base_currency']} is required and must equal 1"
        )
    if not math.isclose(
        normalized_rates[config["base_currency"]], 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise RouteEngineError(
            f"fx_to_base.{config['base_currency']} must equal 1"
        )
    config["fx_to_base"] = normalized_rates

    quality_gate = config.get("quality_gate")
    if not isinstance(quality_gate, dict):
        raise RouteEngineError("quality_gate must be an object")
    quality_gate = copy.deepcopy(quality_gate)
    target_was_provided = "target_complete_loop_efficiency_km_per_base_currency_unit" in quality_gate
    quality_gate.setdefault("minimum_leg_efficiency_km_per_base_currency_unit", None)
    quality_gate.setdefault("maximum_leg_price", None)
    if "minimum_complete_loop_efficiency_km_per_base_currency_unit" not in quality_gate:
        raise RouteEngineError(
            "quality_gate.minimum_complete_loop_efficiency_km_per_base_currency_unit is required"
        )
    leg_floor = quality_gate["minimum_leg_efficiency_km_per_base_currency_unit"]
    if leg_floor is not None:
        leg_floor = positive_number(
            leg_floor, "quality_gate.minimum_leg_efficiency_km_per_base_currency_unit"
        )
    quality_gate["minimum_leg_efficiency_km_per_base_currency_unit"] = leg_floor
    loop_floor = positive_number(
        quality_gate["minimum_complete_loop_efficiency_km_per_base_currency_unit"],
        "quality_gate.minimum_complete_loop_efficiency_km_per_base_currency_unit",
    )
    target = positive_number(
        quality_gate["target_complete_loop_efficiency_km_per_base_currency_unit"]
        if target_was_provided
        else loop_floor,
        "quality_gate.target_complete_loop_efficiency_km_per_base_currency_unit",
    )
    if target < loop_floor:
        raise RouteEngineError(
            "quality_gate.target_complete_loop_efficiency_km_per_base_currency_unit must be at least the complete-loop minimum"
        )
    quality_gate["minimum_complete_loop_efficiency_km_per_base_currency_unit"] = loop_floor
    quality_gate["target_complete_loop_efficiency_km_per_base_currency_unit"] = target
    maximum_leg_price = quality_gate["maximum_leg_price"]
    if maximum_leg_price is not None:
        maximum_leg_price = positive_number(
            maximum_leg_price, "quality_gate.maximum_leg_price"
        )
        maximum_leg_price_currency = str(
            quality_gate.get("maximum_leg_price_currency")
            or config["base_currency"]
        ).upper()
        if maximum_leg_price_currency not in normalized_rates:
            raise RouteEngineError(
                "quality_gate.maximum_leg_price_currency requires a matching fx_to_base rate"
            )
        quality_gate["maximum_leg_price_currency"] = maximum_leg_price_currency
    else:
        quality_gate.pop("maximum_leg_price_currency", None)
    quality_gate["maximum_leg_price"] = maximum_leg_price
    config["quality_gate"] = quality_gate

    config["date_anchor_limit_per_destination"] = integer_value(
        config["date_anchor_limit_per_destination"],
        "date_anchor_limit_per_destination",
        minimum=1,
    )
    early_return = config.get("early_return_search")
    if not isinstance(early_return, dict):
        raise RouteEngineError("early_return_search must be an object")
    early_return = copy.deepcopy(early_return)
    early_return.setdefault("enabled", True)
    minimum_return_depth = max(1, config["min_legs"] - 1)
    early_return.setdefault("start_after_used_legs", minimum_return_depth)
    if not isinstance(early_return["enabled"], bool):
        raise RouteEngineError("early_return_search.enabled must be boolean")
    early_return["start_after_used_legs"] = max(
        minimum_return_depth,
        integer_value(
            early_return["start_after_used_legs"],
            "early_return_search.start_after_used_legs",
            minimum=1,
        ),
    )
    config["early_return_search"] = early_return

    search_budget = config.get("search_budget")
    if not isinstance(search_budget, dict):
        raise RouteEngineError("search_budget must be an object")
    search_budget = copy.deepcopy(search_budget)
    search_budget.setdefault("max_browser_queries", None)
    if search_budget["max_browser_queries"] is not None:
        search_budget["max_browser_queries"] = integer_value(
            search_budget["max_browser_queries"],
            "search_budget.max_browser_queries",
            minimum=1,
        )
    config["search_budget"] = search_budget

    source_strategy = config.get("source_strategy")
    if not isinstance(source_strategy, dict):
        raise RouteEngineError("source_strategy must be an object")
    source_strategy = copy.deepcopy(source_strategy)
    source_strategy.setdefault("selection_mode", "ordered")
    source_strategy.setdefault("primary_source_id", PRIMARY_SOURCE_ID)
    source_strategy.setdefault("fallback_source_ids", list(STRICT_SOURCE_ORDER[1:]))
    selection_mode = str(source_strategy["selection_mode"]).strip().lower()
    if selection_mode != "ordered":
        raise RouteEngineError("source_strategy.selection_mode must be ordered")
    primary_source_id = str(source_strategy["primary_source_id"]).strip()
    if not primary_source_id:
        raise RouteEngineError("source_strategy.primary_source_id is required")
    fallback_source_ids = source_strategy["fallback_source_ids"]
    if not isinstance(fallback_source_ids, list):
        raise RouteEngineError("source_strategy.fallback_source_ids must be an array")
    normalized_fallbacks: list[str] = []
    for index, source_id in enumerate(fallback_source_ids):
        normalized_source_id = str(source_id).strip()
        if not normalized_source_id:
            raise RouteEngineError(
                f"source_strategy.fallback_source_ids[{index}] cannot be empty"
            )
        if normalized_source_id == primary_source_id:
            raise RouteEngineError(
                "source_strategy.fallback_source_ids cannot contain primary_source_id"
            )
        if normalized_source_id not in normalized_fallbacks:
            normalized_fallbacks.append(normalized_source_id)
    ordered_sources = [primary_source_id, *normalized_fallbacks]
    if ordered_sources != list(STRICT_SOURCE_ORDER):
        raise RouteEngineError(
            "ordered source_strategy must be exactly: "
            + " -> ".join(STRICT_SOURCE_ORDER)
        )
    source_strategy["selection_mode"] = selection_mode
    source_strategy["primary_source_id"] = primary_source_id
    source_strategy["fallback_source_ids"] = normalized_fallbacks
    config["source_strategy"] = source_strategy

    airport_transit = config.get("airport_transit", {})
    if not isinstance(airport_transit, dict):
        raise RouteEngineError("airport_transit must be an object")
    unknown_policy = str(
        airport_transit.get("unknown_policy", "allow_with_warning")
    ).lower()
    if unknown_policy not in AIRPORT_TRANSIT_UNKNOWN_POLICIES:
        raise RouteEngineError(
            "airport_transit.unknown_policy must be allow_with_warning or reject"
        )
    airside_only = airport_transit.get("airside_only", False)
    if not isinstance(airside_only, bool):
        raise RouteEngineError("airport_transit.airside_only must be boolean")
    airport_transit["unknown_policy"] = unknown_policy
    airport_transit["airside_only"] = airside_only
    config["airport_transit"] = airport_transit
    return config


def haversine_km(origin: dict[str, Any], destination: dict[str, Any]) -> float:
    try:
        lat1 = math.radians(float(origin["latitude"]))
        lon1 = math.radians(float(origin["longitude"]))
        lat2 = math.radians(float(destination["latitude"]))
        lon2 = math.radians(float(destination["longitude"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteEngineError("Airport coordinates are missing or invalid") from exc
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def route_id(segments: Iterable[dict[str, Any]]) -> str:
    material = "|".join(
        f"{segment['origin']}-{segment['destination']}-"
        f"{segment['departure']}-{segment.get('flight_no', '')}-"
        f"{segment['normalized_price']:.8f}"
        for segment in segments
    )
    return "r_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def remaining_trip_minutes(state: dict[str, Any], config: dict[str, Any]) -> int:
    preferred_minutes = int(round(config["preferred_trip_duration_hours"] * 60))
    if not state.get("segments"):
        trip_start = parse_datetime(config["trip_start"], "trip_start")
        trip_end = parse_datetime(config["trip_end"], "trip_end")
        window_minutes = max(
            0, int((utc(trip_end) - utc(trip_start)).total_seconds() // 60)
        )
        return min(preferred_minutes, window_minutes)
    arrival = parse_datetime(state["arrival"], "state.arrival")
    deadline = preferred_route_deadline(state, config)
    return max(0, int((utc(deadline) - utc(arrival)).total_seconds() // 60))


def apply_resource_audit(
    state: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    remaining_legs = max(0, config["max_legs"] - int(state["used_legs"]))
    state["remaining_legs"] = remaining_legs
    state["remaining_trip_minutes"] = remaining_trip_minutes(state, config)
    state["closure_required"] = bool(
        state.get("segments") and remaining_legs == 1
    )
    state["closure_pool"] = list(config["return_airports"])
    return state


def root_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    state = {
        "id": ROOT_ID,
        "current_airport": None,
        "arrival": None,
        "used_legs": 0,
        "segments": [],
        "total_price": 0.0,
        "total_distance_km": 0.0,
        "efficiency_km_per_base_currency_unit": 0.0,
        "airport_transit_audits": [],
        "completed": False,
    }
    return apply_resource_audit(state, config) if config is not None else state


def new_route_graph() -> dict[str, dict[str, Any]]:
    return {
        ROOT_ID: {
            "route_id": ROOT_ID,
            "parent_id": None,
            "incoming_segment": None,
        }
    }


def _register_route_state(
    route_nodes: dict[str, dict[str, Any]], state: dict[str, Any]
) -> str:
    segments = list(state.get("segments", []))
    if not segments:
        route_nodes.setdefault(ROOT_ID, new_route_graph()[ROOT_ID])
        return ROOT_ID
    transfer_audits = list(state.get("airport_transit_audits", []))
    prefix: list[dict[str, Any]] = []
    parent_id = ROOT_ID
    route_nodes.setdefault(ROOT_ID, new_route_graph()[ROOT_ID])
    for index, segment in enumerate(segments):
        prefix.append(copy.deepcopy(segment))
        identifier = route_id(prefix)
        node = {
            "route_id": identifier,
            "parent_id": parent_id,
            "incoming_segment": copy.deepcopy(segment),
        }
        if index > 0 and index - 1 < len(transfer_audits):
            node["transfer_audit"] = copy.deepcopy(transfer_audits[index - 1])
        existing = route_nodes.get(identifier)
        if existing is not None and existing != node:
            raise RouteEngineError(f"route node {identifier} has conflicting content")
        route_nodes[identifier] = node
        parent_id = identifier
    expected = str(state.get("id") or parent_id)
    if expected != parent_id:
        raise RouteEngineError(
            f"route state ID {expected} does not match reconstructed ID {parent_id}"
        )
    return parent_id


def normalize_route_storage(session: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy full-route lists into a single-copy route graph."""
    raw_nodes = session.get("route_nodes")
    route_nodes = (
        copy.deepcopy(raw_nodes)
        if isinstance(raw_nodes, dict)
        else new_route_graph()
    )
    route_nodes.setdefault(ROOT_ID, new_route_graph()[ROOT_ID])
    for collection_name in ("frontier", "completed"):
        raw_collection = session.get(collection_name, [])
        if not isinstance(raw_collection, list):
            raise RouteEngineError(f"Session {collection_name} must be an array")
        identifiers: list[str] = []
        for value in raw_collection:
            if isinstance(value, dict):
                identifier = _register_route_state(route_nodes, value)
            elif isinstance(value, str) and value:
                identifier = value
            else:
                raise RouteEngineError(
                    f"Session {collection_name} contains an invalid route reference"
                )
            if identifier not in route_nodes:
                raise RouteEngineError(
                    f"Session {collection_name} references unknown route {identifier}"
                )
            if identifier not in identifiers:
                identifiers.append(identifier)
        session[collection_name] = identifiers
    session["route_nodes"] = route_nodes
    return session


def route_state(
    session: dict[str, Any], identifier: str, cache: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    route_nodes = session.get("route_nodes")
    if not isinstance(route_nodes, dict):
        migrated = normalize_route_storage(copy.deepcopy(session))
        route_nodes = migrated["route_nodes"]
        session = migrated
    memo = cache if cache is not None else {}
    if identifier in memo:
        return copy.deepcopy(memo[identifier])
    if identifier == ROOT_ID:
        state = root_state(session["config"])
        memo[identifier] = state
        return copy.deepcopy(state)
    node = route_nodes.get(identifier)
    if not isinstance(node, dict):
        raise RouteEngineError(f"unknown route node: {identifier}")
    parent_id = str(node.get("parent_id") or "")
    if not parent_id or parent_id == identifier:
        raise RouteEngineError(f"route node {identifier} has an invalid parent")
    parent = route_state(session, parent_id, memo)
    segment = copy.deepcopy(node.get("incoming_segment"))
    if not isinstance(segment, dict):
        raise RouteEngineError(f"route node {identifier} has no incoming segment")
    segments = copy.deepcopy(parent["segments"]) + [segment]
    if route_id(segments) != identifier:
        raise RouteEngineError(f"route node {identifier} has inconsistent content")
    arrival = parse_datetime(segment.get("arrival"), "segment.arrival")
    first_departure = parse_datetime(
        segments[0].get("departure"), "first segment departure"
    )
    total_price = sum(float(item["normalized_price"]) for item in segments)
    total_distance = sum(float(item["distance_km"]) for item in segments)
    audits = copy.deepcopy(parent.get("airport_transit_audits", []))
    if isinstance(node.get("transfer_audit"), dict):
        audits.append(copy.deepcopy(node["transfer_audit"]))
    raw_completed = session.get("completed", [])
    completed_ids = {
        str(value.get("id")) if isinstance(value, dict) else str(value)
        for value in raw_completed
    }
    state = {
        "id": identifier,
        "parent_id": parent_id,
        "current_airport": segment["destination"],
        "arrival": iso(arrival),
        "used_legs": len(segments),
        "segments": segments,
        "total_price": round(total_price, 2),
        "base_currency": session["config"]["base_currency"],
        "total_distance_km": round(total_distance, 1),
        "efficiency_km_per_base_currency_unit": round(
            total_distance / total_price, 6
        ),
        "trip_duration_hours": round(
            (utc(arrival) - utc(first_departure)).total_seconds() / 3600, 3
        ),
        "airport_transit_audits": audits,
        "completed": identifier in completed_ids,
    }
    state = apply_resource_audit(state, session["config"])
    memo[identifier] = state
    return copy.deepcopy(state)


def route_states(session: dict[str, Any], collection_name: str) -> list[dict[str, Any]]:
    raw_collection = session.get(collection_name, [])
    if not isinstance(raw_collection, list):
        raise RouteEngineError(f"Session {collection_name} must be an array")
    if raw_collection and isinstance(raw_collection[0], dict):
        return copy.deepcopy(raw_collection)
    normalized = normalize_route_storage(copy.deepcopy(session))
    memo: dict[str, dict[str, Any]] = {}
    return [
        route_state(normalized, str(identifier), memo)
        for identifier in normalized.get(collection_name, [])
    ]


def compact_route_memberships(session: dict[str, Any]) -> None:
    route_nodes = session.get("route_nodes")
    if not isinstance(route_nodes, dict):
        route_nodes = new_route_graph()
    for collection_name in ("frontier", "completed"):
        states = session.get(collection_name, [])
        if not isinstance(states, list):
            raise RouteEngineError(f"Session {collection_name} must be an array")
        identifiers: list[str] = []
        for state in states:
            identifier = (
                _register_route_state(route_nodes, state)
                if isinstance(state, dict)
                else str(state)
            )
            if identifier not in identifiers:
                identifiers.append(identifier)
        session[collection_name] = identifiers
    session["route_nodes"] = route_nodes


def normalize_session_storage(session: dict[str, Any]) -> dict[str, Any]:
    """Migrate the previous expanded Session shape into compact authorities."""
    stored_config = session.get("config")
    if isinstance(stored_config, dict):
        for removed_field in ("beam_width", "diversity", "history_limit"):
            stored_config.pop(removed_field, None)
    normalize_route_storage(session)
    legacy_rounds = session.pop("rounds", [])
    legacy_shadows = session.pop("shadows", [])
    legacy_rejected = session.pop("rejected", [])
    session.pop("ai_decision_ledger", None)
    session.pop("updated_at", None)
    audit = session.setdefault("audit", {})
    if not isinstance(audit, dict):
        raise RouteEngineError("Session audit must be an object")
    audit.setdefault(
        "search_round", len(legacy_rounds) if isinstance(legacy_rounds, list) else 0
    )
    audit.setdefault(
        "shadow_count", len(legacy_shadows) if isinstance(legacy_shadows, list) else 0
    )
    audit.setdefault(
        "rejection_count",
        len(legacy_rejected) if isinstance(legacy_rejected, list) else 0,
    )
    reasons = audit.setdefault("rejection_reasons", {})
    if not isinstance(reasons, dict):
        raise RouteEngineError("Session audit.rejection_reasons must be an object")
    if isinstance(legacy_rejected, list):
        for record in legacy_rejected:
            if not isinstance(record, dict):
                continue
            reason = str(record.get("reason") or "unknown")
            reasons[reason] = int(reasons.get(reason, 0)) + 1
    if isinstance(legacy_rounds, list) and legacy_rounds:
        audit.setdefault("last_round", copy.deepcopy(legacy_rounds[-1]))
    else:
        audit.setdefault("last_round", None)

    ordered_dfs = session.get("ordered_dfs")
    if isinstance(ordered_dfs, dict):
        audit.setdefault(
            "backtrack_count", int(ordered_dfs.pop("backtrack_count", 0))
        )
        ordered_dfs.pop("history", None)
        nodes = ordered_dfs.get("nodes", {})
        if isinstance(nodes, dict):
            for identifier, raw_node in list(nodes.items()):
                if not isinstance(raw_node, dict):
                    raise RouteEngineError(
                        f"ordered DFS node {identifier} must be an object"
                    )
                nodes[identifier] = {
                    key: copy.deepcopy(raw_node.get(key))
                    for key in (
                        "node_id",
                        "parent_id",
                        "status",
                        "children",
                        "sibling_efficiency_rank",
                    )
                    if raw_node.get(key) is not None or key == "parent_id"
                }
    else:
        audit.setdefault("backtrack_count", 0)

    try:
        coverage = SCOPE_COVERAGE.normalize_ledger(session.get("scope_coverage"))
    except SCOPE_COVERAGE.ScopeCoverageError as exc:
        raise RouteEngineError(f"invalid scope coverage state: {exc}") from exc
    legacy_source_order = session.pop("source_order_ledger", None)
    if isinstance(legacy_source_order, dict):
        for attempt in legacy_source_order.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            request_id = str(attempt.get("request_id") or "")
            for record in coverage["requests"].values():
                partition = record.get("partitions", {}).get(request_id)
                if not isinstance(partition, dict):
                    continue
                partition["source_attempt_count"] = int(
                    partition.get("source_attempt_count", 0)
                ) + 1
                next_index = attempt.get("next_source_index")
                if attempt.get("outcome") == "fallback" and isinstance(
                    next_index, int
                ):
                    partition["starting_source_index"] = next_index
                break
    session["scope_coverage"] = coverage
    return session


def new_ordered_dfs_ledger() -> dict[str, Any]:
    return {
        "strategy": "ordered_depth_first_with_backtracking",
        "nodes": {
            ROOT_ID: {
                "node_id": ROOT_ID,
                "parent_id": None,
                "status": "active",
                "children": [],
            }
        },
        "active_path": [ROOT_ID],
        "next_candidate_id": ROOT_ID,
    }


def ensure_ordered_dfs_ledger(session: dict[str, Any]) -> dict[str, Any]:
    ledger = session.get("ordered_dfs")
    if not isinstance(ledger, dict) or not isinstance(ledger.get("nodes"), dict):
        ledger = new_ordered_dfs_ledger()
        session["ordered_dfs"] = ledger
    return ledger


def dfs_node_from_state(state: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "node_id": state["id"],
        "parent_id": state.get("parent_id"),
        "status": status,
        "children": [],
    }


def dfs_path_to(nodes: dict[str, dict[str, Any]], node_id: str | None) -> list[str]:
    path: list[str] = []
    seen: set[str] = set()
    current = node_id
    while current is not None and current in nodes and current not in seen:
        path.append(current)
        seen.add(current)
        current = nodes[current].get("parent_id")
    return list(reversed(path))


def first_unexplored_child(
    nodes: dict[str, dict[str, Any]], parent_id: str
) -> str | None:
    for child_id in nodes[parent_id].get("children", []):
        if nodes.get(child_id, {}).get("status") == "unexplored":
            return child_id
    return None


def choose_dfs_candidate_after(
    ledger: dict[str, Any], focus_id: str, *, keep_focus_open: bool
) -> tuple[str | None, bool]:
    nodes = ledger["nodes"]
    if keep_focus_open:
        return focus_id, False
    child = first_unexplored_child(nodes, focus_id)
    if child is not None:
        return child, False
    cursor = focus_id
    while cursor in nodes:
        parent_id = nodes[cursor].get("parent_id")
        if parent_id is None or parent_id not in nodes:
            return None, cursor != focus_id
        sibling = first_unexplored_child(nodes, parent_id)
        if sibling is not None:
            return sibling, True
        if nodes[parent_id].get("status") == "expanded":
            nodes[parent_id]["status"] = "dead_end"
        cursor = parent_id
    return None, True


def update_ordered_dfs(
    session: dict[str, Any],
    parent_map: dict[str, dict[str, Any]],
    covered_order: list[str],
    retain_parent_ids: set[str],
    open_states: list[dict[str, Any]],
    completed_states: list[dict[str, Any]],
) -> dict[str, Any]:
    ledger = ensure_ordered_dfs_ledger(session)
    nodes = ledger["nodes"]
    state_lookup = {
        state["id"]: state
        for state in [*parent_map.values(), *open_states, *completed_states]
    }
    for parent_id, state in parent_map.items():
        if parent_id not in nodes:
            nodes[parent_id] = dfs_node_from_state(state, "unexplored")

    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for state in open_states:
        children_by_parent.setdefault(str(state["parent_id"]), []).append(state)
        existing_children = nodes.get(state["id"], {}).get("children", [])
        nodes[state["id"]] = dfs_node_from_state(state, "unexplored")
        nodes[state["id"]]["children"] = existing_children
    for state in completed_states:
        children_by_parent.setdefault(str(state["parent_id"]), []).append(state)
        existing_children = nodes.get(state["id"], {}).get("children", [])
        nodes[state["id"]] = dfs_node_from_state(state, "completed")
        nodes[state["id"]]["children"] = existing_children

    for parent_id, states in children_by_parent.items():
        parent = nodes[parent_id]
        child_ids = set(parent.get("children", []))
        child_ids.update(state["id"] for state in states)
        def child_sort_key(child_id: str) -> tuple[Any, ...]:
            state = state_lookup.get(child_id)
            if state is None:
                state = route_state(session, child_id)
            return (
                -float(
                    state.get(
                        "efficiency_km_per_base_currency_unit", 0.0
                    )
                ),
                float(state.get("total_price", 0.0)),
                str(state.get("arrival") or ""),
                str(state["id"]),
            )

        parent["children"] = sorted(child_ids, key=child_sort_key)
        for rank, child_id in enumerate(parent["children"], 1):
            nodes[child_id]["sibling_efficiency_rank"] = rank

    unique_covered = list(dict.fromkeys(covered_order))
    for parent_id in unique_covered:
        if parent_id in retain_parent_ids:
            nodes[parent_id]["status"] = "deferred"
        elif nodes[parent_id].get("children"):
            nodes[parent_id]["status"] = "expanded"
        else:
            nodes[parent_id]["status"] = "dead_end"

    focus_id = unique_covered[-1]
    next_candidate_id, backtracked = choose_dfs_candidate_after(
        ledger,
        focus_id,
        keep_focus_open=focus_id in retain_parent_ids,
    )
    if backtracked:
        audit = session.setdefault("audit", {})
        audit["backtrack_count"] = int(audit.get("backtrack_count", 0)) + 1
    ledger["next_candidate_id"] = next_candidate_id
    ledger["active_path"] = dfs_path_to(nodes, next_candidate_id)
    return ledger


def new_browser_query_ledger(config: dict[str, Any]) -> dict[str, Any]:
    maximum = config["search_budget"]["max_browser_queries"]
    return {
        "max_browser_queries": maximum,
        "queries_used": 0,
        "remaining_queries": maximum,
        "entries": [],
    }


def normalized_browser_query_ledger(
    session: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    maximum = config["search_budget"]["max_browser_queries"]
    raw = session.get("browser_query_ledger")
    if raw is None:
        return new_browser_query_ledger(config)
    if not isinstance(raw, dict) or not isinstance(raw.get("entries", []), list):
        raise RouteEngineError("browser_query_ledger must contain an entries array")
    ledger = copy.deepcopy(raw)
    entries = ledger.get("entries", [])
    seen: dict[str, str] = {}
    normalized_entries: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise RouteEngineError(
                f"browser_query_ledger.entries[{index}] must be an object"
            )
        entry = copy.deepcopy(raw_entry)
        query_id = entry.get("query_id")
        if not isinstance(query_id, str) or not query_id.strip():
            raise RouteEngineError(
                f"browser_query_ledger.entries[{index}].query_id is required"
            )
        query_id = query_id.strip()
        content_sha256 = entry.get("content_sha256")
        legacy_signature = entry.pop("signature", None)
        if content_sha256 is None and isinstance(legacy_signature, str):
            content_sha256 = hashlib.sha256(
                legacy_signature.encode("utf-8")
            ).hexdigest()
        if (
            not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        ):
            raise RouteEngineError(
                f"browser_query_ledger.entries[{index}].content_sha256 is invalid"
            )
        prior = seen.get(query_id)
        if prior is not None:
            if prior != content_sha256:
                raise RouteEngineError(
                    f"browser_query_ledger query_id {query_id} has conflicting content"
                )
            raise RouteEngineError(
                f"browser_query_ledger query_id {query_id} is duplicated"
            )
        seen[query_id] = content_sha256
        entry["query_id"] = query_id
        entry["content_sha256"] = content_sha256
        normalized_entries.append(entry)
    entries = normalized_entries
    used = len(entries)
    if maximum is not None and used > maximum:
        raise RouteEngineError(
            "browser_query_ledger exceeds search_budget.max_browser_queries"
        )
    ledger["max_browser_queries"] = maximum
    ledger["queries_used"] = used
    ledger["remaining_queries"] = None if maximum is None else maximum - used
    ledger["entries"] = entries
    return ledger


def browser_query_identity(group: dict[str, Any]) -> tuple[str, str]:
    """Return an explicit or deterministic query ID plus its content hash."""
    canonical = json.dumps(
        group, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    query_id = group.get("query_id")
    if query_id is None:
        query_id = "query-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    if not isinstance(query_id, str) or not query_id.strip():
        raise RouteEngineError("query_id must be a non-empty string when provided")
    return query_id.strip(), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_session(config_raw: dict[str, Any], airports_raw: Any) -> dict[str, Any]:
    config = validate_config(config_raw)
    airports = validate_airport_catalog(airports_raw)
    transit_catalog = config.pop("_airport_transit_catalog", None)
    if transit_catalog is not None:
        try:
            AIRPORT_TRANSIT.validate_catalog(transit_catalog)
        except AIRPORT_TRANSIT.AirportTransitError as exc:
            raise RouteEngineError(f"invalid airport_transit_catalog: {exc}") from exc
    session = {
        "engine_version": ENGINE_VERSION,
        "config": config,
        "airports": airports,
        "airport_transit_catalog": transit_catalog,
        "route_nodes": new_route_graph(),
        "frontier": [ROOT_ID],
        "completed": [],
        "audit": {
            "search_round": 0,
            "backtrack_count": 0,
            "shadow_count": 0,
            "rejection_count": 0,
            "rejection_reasons": {},
            "last_round": None,
        },
        "ordered_dfs": new_ordered_dfs_ledger(),
        "browser_query_ledger": new_browser_query_ledger(config),
        "scope_coverage": SCOPE_COVERAGE.new_ledger(),
        "finalization_status": "open",
        "finalization_history": [],
    }
    refresh_search_requests(session)
    return session


def load_config_and_airports(config_path: str | Path) -> tuple[dict[str, Any], Any]:
    raw = read_json(config_path)
    airport_value: Any = raw.pop("airports", {})
    catalog_ref = raw.pop("airport_catalog", None)
    if catalog_ref:
        catalog_path = Path(catalog_ref)
        if not catalog_path.is_absolute():
            catalog_path = Path(config_path).resolve().parent / catalog_path
        catalog_json = read_json(catalog_path)
        airport_value = catalog_json.get("airports", catalog_json)
    transit_ref = raw.get("airport_transit_catalog")
    if transit_ref:
        transit_path = Path(str(transit_ref))
        if not transit_path.is_absolute():
            transit_path = Path(config_path).resolve().parent / transit_path
        try:
            raw["_airport_transit_catalog"] = AIRPORT_TRANSIT.load_catalog(transit_path)
        except (AIRPORT_TRANSIT.AirportTransitError, OSError, json.JSONDecodeError) as exc:
            raise RouteEngineError(
                f"invalid airport_transit_catalog {transit_path}: {exc}"
            ) from exc
    return raw, airport_value


def offer_distance(
    offer: dict[str, Any], origin: str, destination: str, airports: dict[str, Any]
) -> float:
    reported_distance = None
    if "distance_km" in offer:
        reported_distance = positive_number(offer["distance_km"], "offer.distance_km")
    origin_entry = airports.get(origin, {})
    destination_entry = airports.get(destination, {})
    coordinates_available = all(
        field in entry
        for entry in (origin_entry, destination_entry)
        for field in ("latitude", "longitude")
    )
    if coordinates_available:
        return positive_number(
            haversine_km(origin_entry, destination_entry),
            "calculated_airport_distance_km",
        )
    if reported_distance is not None:
        return reported_distance
    missing = [
        code
        for code, entry in ((origin, origin_entry), (destination, destination_entry))
        if "latitude" not in entry or "longitude" not in entry
    ]
    raise RouteEngineError("Missing airport coordinates for " + ", ".join(missing))


def extend_state(
    parent: dict[str, Any],
    offer: dict[str, Any],
    source: dict[str, Any],
    config: dict[str, Any],
    airports: dict[str, Any],
) -> tuple[dict[str, Any], datetime, datetime]:
    origin = normalized_code(offer.get("origin"), "offer.origin")
    destination = normalized_code(offer.get("destination"), "offer.destination")
    if origin not in airports:
        raise RouteEngineError(
            f"Offer origin {origin} is missing from the session airport catalog"
        )
    destination_is_observed = destination not in airports
    if destination_is_observed:
        raw_destination = offer.get("destination")
        if (
            not isinstance(raw_destination, str)
            or re.fullmatch(r"[A-Z]{3}", raw_destination) is None
        ):
            raise RouteEngineError(
                "Unknown offer.destination must match IATA ^[A-Z]{3}$"
            )
        observed_distance = positive_number(
            offer.get("distance_km"), "offer.distance_km"
        )
        if observed_distance > 20040:
            raise RouteEngineError(
                "offer.distance_km must not exceed 20040 for an observed destination"
            )
    departure = parse_datetime(offer.get("departure"), "offer.departure")
    arrival = parse_datetime(offer.get("arrival"), "offer.arrival")
    if utc(arrival) <= utc(departure):
        raise RouteEngineError("offer.arrival must be later than offer.departure")

    if parent["used_legs"] == 0:
        if origin not in config["departure_airports"]:
            raise RouteEngineError(f"First-leg origin {origin} is outside departure pool")
    elif origin != parent["current_airport"]:
        raise RouteEngineError(
            f"Offer origin {origin} does not match parent airport "
            f"{parent['current_airport']}"
        )

    currency = str(offer.get("currency", "")).upper()
    if currency not in config["fx_to_base"]:
        raise RouteEngineError("missing_offer_currency_fx")
    price = positive_number(offer.get("price"), "offer.price")
    normalized_price = positive_number(
        price * config["fx_to_base"][currency], "offer.normalized_price"
    )
    distance = offer_distance(offer, origin, destination, airports)
    coordinates_available = all(
        field in airports.get(code, {})
        for code in (origin, destination)
        for field in ("latitude", "longitude")
    )
    distance_source = (
        "catalog_haversine" if coordinates_available else "observed_explicit"
    )
    leg_efficiency_km_per_base_currency_unit = positive_number(
        distance / normalized_price, "offer.efficiency_km_per_base_currency_unit"
    )
    flight_no = str(offer.get("flight_no", "")).strip()
    if not flight_no:
        raise RouteEngineError("offer.flight_no is required")
    booking_url = normalized_http_url(offer.get("booking_url"), "offer.booking_url")

    segment = {
        "origin": origin,
        "destination": destination,
        "departure": iso(departure),
        "arrival": iso(arrival),
        "price": round(price, 2),
        "currency": currency,
        "normalized_price": round(normalized_price, 8),
        "base_currency": config["base_currency"],
        "efficiency_km_per_base_currency_unit": round(leg_efficiency_km_per_base_currency_unit, 6),
        "distance_km": round(distance, 6),
        "distance_source": distance_source,
        "flight_no": flight_no,
        "booking_url": booking_url,
        "notes": offer.get("notes", []),
        "source": copy.deepcopy(source),
    }
    for context_field in ("departure_context", "arrival_context"):
        context = validate_airport_context(
            offer.get(context_field), f"offer.{context_field}"
        )
        if context is not None:
            segment[context_field] = context
    if not isinstance(segment["notes"], list):
        raise RouteEngineError("offer.notes must be an array")

    previous_segments = copy.deepcopy(parent["segments"])
    for previous_segment in previous_segments:
        previous_normalized_price = positive_number(
            previous_segment.get("normalized_price"),
            "existing_segment.normalized_price",
        )
        previous_distance = positive_number(
            previous_segment.get("distance_km"), "existing_segment.distance_km"
        )
        previous_efficiency = positive_number(
            previous_distance / previous_normalized_price,
            "existing_segment.efficiency_km_per_base_currency_unit",
        )
        previous_segment["efficiency_km_per_base_currency_unit"] = round(
            previous_efficiency, 6
        )
    segments = previous_segments + [segment]
    total_price = positive_number(
        sum(float(item["normalized_price"]) for item in segments),
        "route.total_price",
    )
    total_distance = positive_number(
        sum(float(item["distance_km"]) for item in segments),
        "route.total_distance_km",
    )
    efficiency_in_base = positive_number(
        total_distance / total_price,
        "route.efficiency_km_per_base_currency_unit",
    )
    first_departure = parse_datetime(
        segments[0]["departure"], "first segment departure"
    )
    trip_duration_hours = (
        utc(arrival) - utc(first_departure)
    ).total_seconds() / 3600
    state = {
        "id": route_id(segments),
        "parent_id": parent["id"],
        "current_airport": destination,
        "arrival": iso(arrival),
        "used_legs": len(segments),
        "segments": segments,
        "total_price": round(total_price, 2),
        "base_currency": config["base_currency"],
        "total_distance_km": round(total_distance, 1),
        "efficiency_km_per_base_currency_unit": round(efficiency_in_base, 6),
        "trip_duration_hours": round(trip_duration_hours, 3),
        "airport_transit_audits": copy.deepcopy(
            parent.get("airport_transit_audits", [])
        ),
        "completed": False,
    }
    return apply_resource_audit(state, config), departure, arrival


def airport_transfer_audit(
    parent: dict[str, Any],
    state: dict[str, Any],
    catalog: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not parent.get("segments"):
        return None
    previous_segment = parent["segments"][-1]
    next_segment = state["segments"][-1]
    if previous_segment["destination"] != next_segment["origin"]:
        return None
    airport_id = previous_segment["destination"]
    settings = config["airport_transit"]
    unknown_policy = settings["unknown_policy"]
    base = {
        "previous_flight_no": previous_segment.get("flight_no", ""),
        "next_flight_no": next_segment.get("flight_no", ""),
        "airport_id": airport_id,
        "unknown_policy": unknown_policy,
        "eligibility_assessed": False,
    }

    def unknown(reason: str) -> dict[str, Any]:
        return {
            **base,
            "status": "unknown",
            "allowed": unknown_policy == "allow_with_warning",
            "reason_codes": [reason],
            "evaluation": None,
        }

    if catalog is None:
        return unknown("AIRPORT_TRANSIT_CATALOG_MISSING")
    arrival_context = previous_segment.get("arrival_context")
    departure_context = next_segment.get("departure_context")
    if not arrival_context or not departure_context:
        return unknown("AIRPORT_TRANSIT_CONTEXT_MISSING")
    request = {
        "airport_id": airport_id,
        "arrival": {
            **arrival_context,
            "local_datetime": previous_segment["arrival"],
        },
        "departure": {
            **departure_context,
            "local_datetime": next_segment["departure"],
        },
        "airside_only": settings["airside_only"],
    }
    try:
        evaluation = AIRPORT_TRANSIT.evaluate_transfer(catalog, request)
    except AIRPORT_TRANSIT.AirportTransitError:
        return unknown("AIRPORT_TRANSIT_EVALUATION_UNKNOWN")
    return {
        **base,
        "status": "feasible" if evaluation["feasible"] else "infeasible",
        "allowed": bool(evaluation["feasible"]),
        "reason_codes": list(evaluation["reason_codes"]),
        "evaluation": evaluation,
    }


def classify_timing(
    parent: dict[str, Any],
    state: dict[str, Any],
    departure: datetime,
    arrival: datetime,
    config: dict[str, Any],
) -> tuple[str, str | None]:
    trip_start = parse_datetime(config["trip_start"], "trip_start")
    trip_end = parse_datetime(config["trip_end"], "trip_end")
    if parent["used_legs"] == 0:
        if utc(departure) < utc(trip_start):
            return "rejected", "departure_before_trip_start"
        if utc(departure) > utc(trip_end):
            return "rejected", "first_departure_after_trip_end"
        origin = state["segments"][-1]["origin"]
        cutoff_rule = config["first_leg_earliest_local_time"].get(origin)
        cutoff_text = cutoff_rule
        if isinstance(cutoff_rule, dict):
            cutoff_text = cutoff_rule.get(
                departure.date().isoformat(), cutoff_rule.get("default")
            )
        if cutoff_text and departure.time().replace(tzinfo=None) < parse_local_time(
            cutoff_text, f"first-leg cutoff for {origin}"
        ):
            return "shadow", "first_leg_before_ground_access_cutoff"
        if state["trip_duration_hours"] > config["preferred_trip_duration_hours"]:
            return "shadow", "trip_duration_exceeds_preferred_limit"
        return "frontier", None

    parent_arrival = parse_datetime(parent["arrival"], "parent.arrival")
    earliest = parent_arrival + timedelta(
        minutes=config["connection_buffer_minutes"]
    )
    latest = parent_arrival + timedelta(
        minutes=config["max_connection_wait_minutes"]
    )
    if utc(departure) < utc(earliest):
        return "rejected", "connection_buffer_too_short"
    if utc(departure) > utc(latest):
        return "rejected", "connection_wait_too_long"
    if state["trip_duration_hours"] > config["preferred_trip_duration_hours"]:
        return "shadow", "trip_duration_exceeds_preferred_limit"
    return "frontier", None


def rejection_record(
    parent_id: str,
    offer: Any,
    reason: str,
    state: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "parent_id": parent_id,
        "reason": reason,
        "offer": copy.deepcopy(offer),
    }
    if state is not None:
        value["scored_state"] = state
    if source is not None:
        value["source"] = copy.deepcopy(source)
    return value


def state_sort_key(state: dict[str, Any]) -> tuple[Any, ...]:
    segments = state.get("segments", [])
    exact_price = sum(float(segment["normalized_price"]) for segment in segments)
    exact_distance = sum(float(segment["distance_km"]) for segment in segments)
    exact_efficiency = exact_distance / exact_price if exact_price else 0.0
    return (
        -exact_efficiency,
        exact_price,
        state["arrival"] or "",
        state["id"],
    )


def deduplicate_states(states: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for state in states:
        existing = unique.get(state["id"])
        if existing is None or state_sort_key(state) < state_sort_key(existing):
            unique[state["id"]] = state
    return sorted(unique.values(), key=state_sort_key)


def limit_date_anchors_per_destination(
    ranked_states: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the best configured number of dated arrival anchors per parent/destination."""
    limit = int(config["date_anchor_limit_per_destination"])
    accepted_dates: dict[tuple[str, str], list[str]] = {}
    kept: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    for state in ranked_states:
        key = (str(state.get("parent_id") or ""), str(state.get("current_airport") or ""))
        arrival_date = (
            parse_datetime(state["arrival"], "state.arrival").date().isoformat()
            if state.get("arrival")
            else ""
        )
        dates = accepted_dates.setdefault(key, [])
        if arrival_date in dates or len(dates) < limit:
            if arrival_date not in dates:
                dates.append(arrival_date)
            kept.append(state)
        else:
            pruned.append(state)
    return kept, pruned


def preferred_route_deadline(
    state: dict[str, Any], config: dict[str, Any]
) -> datetime:
    if not state.get("segments"):
        return parse_datetime(config["trip_end"], "trip_end")
    first_departure = parse_datetime(
        state["segments"][0]["departure"], "first segment departure"
    )
    return first_departure + timedelta(
        hours=config["preferred_trip_duration_hours"]
    )


def no_search_window_reason(state: dict[str, Any], config: dict[str, Any]) -> str:
    return "no_search_window_before_preferred_duration_limit"


def expand_session(
    session_raw: dict[str, Any], candidates_raw: dict[str, Any]
) -> dict[str, Any]:
    session = copy.deepcopy(session_raw)
    normalize_session_storage(session)
    config = validate_config(session.get("config", {}))
    airports = validate_airport_catalog(session.get("airports", {}))
    airports.update(validate_airport_catalog(candidates_raw.get("airports", {})))
    session["airports"] = airports
    session["config"] = config
    ledger = normalized_browser_query_ledger(session, config)
    try:
        scope_coverage = SCOPE_COVERAGE.normalize_ledger(
            session.get("scope_coverage")
        )
    except SCOPE_COVERAGE.ScopeCoverageError as exc:
        raise RouteEngineError(f"invalid scope coverage state: {exc}") from exc
    session["scope_coverage"] = scope_coverage
    ordered_sources = config["source_strategy"]["selection_mode"] == "ordered"

    frontier = route_states(session, "frontier")
    if not frontier:
        raise RouteEngineError("Session has no open frontier to expand")
    session["frontier"] = frontier
    session["completed"] = route_states(session, "completed")
    parent_map = {state["id"]: state for state in frontier}
    published_requests = build_search_requests(session)
    actionable_requests = {
        request["request_id"]: request
        for request in published_requests
        if isinstance(request, dict) and isinstance(request.get("request_id"), str)
    }
    groups_raw = candidates_raw.get("search_results")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise RouteEngineError("search_results must be a non-empty array")

    processed_queries = {
        entry["query_id"]: entry["content_sha256"]
        for entry in ledger["entries"]
    }
    seen_queries: dict[str, str] = {}
    groups: list[dict[str, Any]] = []
    group_identities: list[tuple[str, str]] = []
    coverage_reports: dict[str, dict[str, Any]] = {}
    for raw_group in groups_raw:
        if not isinstance(raw_group, dict):
            raise RouteEngineError("Each search_results entry must be an object")
        group = copy.deepcopy(raw_group)
        query_id, content_sha256 = browser_query_identity(group)
        if query_id in seen_queries and seen_queries[query_id] != content_sha256:
            raise RouteEngineError(f"query_id {query_id} conflicts inside one round")
        seen_queries[query_id] = content_sha256
        prior_content_sha256 = processed_queries.get(query_id)
        if prior_content_sha256 is not None:
            if prior_content_sha256 != content_sha256:
                raise RouteEngineError(
                    f"query_id {query_id} was replayed with different content"
                )
            continue
        groups.append(group)
        group_identities.append((query_id, content_sha256))

    # Exact replay is a no-op: do not charge the browser budget and do not
    # attempt to expand parents that have already advanced to the next layer.
    if not groups:
        session["browser_query_ledger"] = ledger
        compact_route_memberships(session)
        refresh_search_requests(session)
        return session

    invalidate_finalization(session, "new browser evidence was ingested")

    covered: set[str] = set()
    covered_order: list[str] = []
    retain_parent_ids: set[str] = set()
    validated_sources: list[dict[str, Any]] = []
    for group in groups:
        parent_id = str(group.get("parent_id", ""))
        if parent_id not in parent_map:
            raise RouteEngineError(f"Unknown or pruned parent_id: {parent_id}")
        covered.add(parent_id)
        covered_order.append(parent_id)
        keep_parent_open = group.get("keep_parent_open", False)
        if not isinstance(keep_parent_open, bool):
            raise RouteEngineError(
                f"keep_parent_open for {parent_id} must be boolean"
            )
        if keep_parent_open and not ordered_sources:
            retain_parent_ids.add(parent_id)
        if not isinstance(group.get("offers"), list):
            raise RouteEngineError(f"offers for {parent_id} must be an array")
        source = validate_source(group.get("source"), parent_id)
        if ordered_sources:
            request_id = str(group.get("request_id", "")).strip()
            if not request_id or request_id not in actionable_requests:
                raise RouteEngineError(
                    f"ordered search result for {parent_id} requires an actionable request_id"
                )
            request = actionable_requests[request_id]
            if request["parent_id"] != parent_id:
                raise RouteEngineError(
                    f"request_id {request_id} does not belong to parent {parent_id}"
                )
            source_id = str(group.get("source_id", "")).strip()
            if source_id != request["required_source_id"]:
                raise RouteEngineError(
                    f"request_id {request_id} requires source_id "
                    f"{request['required_source_id']}, received {source_id or '<missing>'}"
                )
            observation_kind = normalize_ordered_observation_kind(
                group.get("observation_kind")
            )
            if observation_kind not in ORDERED_OBSERVATION_KINDS:
                raise RouteEngineError(
                    f"ordered search result for {request_id} requires observation_kind"
                )
            group["observation_kind"] = observation_kind
            if observation_kind == "payable" and not group["offers"]:
                raise RouteEngineError(
                    f"payable observation for {request_id} requires at least one offer"
                )
            if observation_kind != "payable" and group["offers"]:
                raise RouteEngineError(
                    f"{observation_kind} observation for {request_id} cannot contain offers"
                )
            try:
                coverage_report = SCOPE_COVERAGE.normalize_observation(request, group)
            except SCOPE_COVERAGE.ScopeCoverageError as exc:
                raise RouteEngineError(
                    f"invalid scope coverage for {request_id}: {exc}"
                ) from exc
            group["submitted_scope"] = coverage_report["submitted_scope"]
            group["coverage_complete"] = coverage_report["coverage_complete"]
            group["scope_partitions"] = coverage_report["scope_partitions"]
            group["acquisition"] = coverage_report["acquisition"]
            coverage_reports[query_id] = coverage_report
            source["source_id"] = source_id
        validated_sources.append(source)
    missing = sorted(set(parent_map) - covered)
    if (
        ledger["remaining_queries"] is not None
        and len(groups) > ledger["remaining_queries"]
    ):
        raise RouteEngineError(
            "Browser query budget exceeded: "
            f"submitted {len(groups)} search result groups with only "
            f"{ledger['remaining_queries']} remaining"
        )

    open_states: list[dict[str, Any]] = []
    newly_completed: list[dict[str, Any]] = []
    new_shadows: list[dict[str, Any]] = []
    new_rejected: list[dict[str, Any]] = []
    evaluated_count = 0
    ordered_request_stats: dict[str, dict[str, Any]] = {}

    for group, source, identity in zip(
        groups, validated_sources, group_identities, strict=True
    ):
        parent_id = str(group["parent_id"])
        parent = parent_map[parent_id]
        usable_before = len(open_states) + len(newly_completed)
        if not group["offers"]:
            new_rejected.append(
                {
                    "parent_id": parent_id,
                    "reason": "no_observed_offers",
                    "source": source,
                }
            )
        for offer in group["offers"]:
            evaluated_count += 1
            if not isinstance(offer, dict):
                new_rejected.append(
                    rejection_record(
                        parent_id,
                        offer,
                        "offer_must_be_an_object",
                        source=source,
                    )
                )
                continue
            state: dict[str, Any] | None = None
            try:
                if ordered_sources:
                    SCOPE_COVERAGE.validate_offer_in_submitted_scope(
                        offer, coverage_reports[identity[0]]
                    )
                state, departure, arrival = extend_state(
                    parent, offer, source, config, airports
                )
                maximum_leg_price = config["quality_gate"]["maximum_leg_price"]
                if maximum_leg_price is not None:
                    cap_currency = config["quality_gate"][
                        "maximum_leg_price_currency"
                    ]
                    segment = state["segments"][-1]
                    actual_price = float(segment["normalized_price"]) / float(
                        config["fx_to_base"][cap_currency]
                    )
                    if actual_price >= maximum_leg_price:
                        record = rejection_record(
                            parent_id,
                            offer,
                            "leg_price_at_or_above_hard_cap",
                            state,
                            source,
                        )
                        record["quality_gate_audit"] = {
                            "scope": "leg_price",
                            "comparison": "strictly_less_than",
                            "actual_price": actual_price,
                            "maximum_price": maximum_leg_price,
                            "currency": cap_currency,
                        }
                        new_rejected.append(record)
                        continue
                minimum_leg_efficiency = config["quality_gate"][
                    "minimum_leg_efficiency_km_per_base_currency_unit"
                ]
                failed_leg_index: int | None = None
                segment: dict[str, Any] | None = None
                actual_leg_efficiency = 0.0
                for index, candidate_segment in enumerate(state["segments"]):
                    candidate_efficiency = float(
                        candidate_segment[
                            "efficiency_km_per_base_currency_unit"
                        ]
                    )
                    if (
                        minimum_leg_efficiency is not None
                        and candidate_efficiency < minimum_leg_efficiency
                    ):
                        failed_leg_index = index
                        segment = candidate_segment
                        actual_leg_efficiency = candidate_efficiency
                        break
                if segment is None:
                    segment = state["segments"][-1]
                    actual_leg_efficiency = float(
                        segment["efficiency_km_per_base_currency_unit"]
                    )
                if failed_leg_index is not None:
                    record = rejection_record(
                        parent_id,
                        offer,
                        "leg_efficiency_below_minimum",
                        state,
                        source,
                    )
                    record["quality_gate_audit"] = {
                        "scope": "leg",
                        "segment_index": failed_leg_index,
                        "base_currency": config["base_currency"],
                        "actual_efficiency_km_per_base_currency_unit": actual_leg_efficiency,
                        "minimum_efficiency_km_per_base_currency_unit": minimum_leg_efficiency,
                        "normalized_price": float(segment["normalized_price"]),
                        "distance_km": float(segment["distance_km"]),
                    }
                    new_rejected.append(record)
                    continue
                transit_audit = airport_transfer_audit(
                    parent,
                    state,
                    session.get("airport_transit_catalog"),
                    config,
                )
                if transit_audit is not None:
                    state["airport_transit_audits"].append(transit_audit)
                    if not transit_audit["allowed"]:
                        new_rejected.append(
                            rejection_record(
                                parent_id,
                                offer,
                                "airport_transit_infeasible"
                                if transit_audit["status"] == "infeasible"
                                else "airport_transit_unknown",
                                state,
                                source,
                            )
                        )
                        continue
                classification, reason = classify_timing(
                    parent, state, departure, arrival, config
                )
                if classification == "shadow":
                    state["status_reason"] = reason
                    new_shadows.append(state)
                    continue
                if classification == "rejected":
                    new_rejected.append(
                        rejection_record(
                            parent_id,
                            offer,
                            reason or "time_invalid",
                            state,
                            source,
                        )
                    )
                    continue

                used_legs = int(state["used_legs"])
                at_home = state["current_airport"] in config["return_airports"]
                if at_home and used_legs >= config["min_legs"]:
                    minimum_loop_efficiency = config["quality_gate"][
                        "minimum_complete_loop_efficiency_km_per_base_currency_unit"
                    ]
                    actual_loop_efficiency = float(
                        state["efficiency_km_per_base_currency_unit"]
                    )
                    if actual_loop_efficiency < minimum_loop_efficiency:
                        record = rejection_record(
                            parent_id,
                            offer,
                            "complete_loop_efficiency_below_minimum",
                            state,
                            source,
                        )
                        record["quality_gate_audit"] = {
                            "scope": "complete_loop",
                            "base_currency": config["base_currency"],
                            "actual_efficiency_km_per_base_currency_unit": actual_loop_efficiency,
                            "minimum_efficiency_km_per_base_currency_unit": minimum_loop_efficiency,
                            "total_price": sum(
                                float(segment["normalized_price"])
                                for segment in state["segments"]
                            ),
                            "distance_km": sum(
                                float(segment["distance_km"])
                                for segment in state["segments"]
                            ),
                        }
                        new_rejected.append(record)
                        continue
                    destination = state["current_airport"]
                    if destination not in airports:
                        airports[destination] = {
                            "code": destination,
                            "name": destination,
                            "discovery_scope": "observed_destination",
                        }
                    state["completed"] = True
                    state["closure_required"] = False
                    newly_completed.append(state)
                elif used_legs >= config["max_legs"]:
                    new_rejected.append(
                        rejection_record(
                            parent_id,
                            offer,
                            "max_legs_reached_outside_return_pool",
                            state,
                            source,
                        )
                    )
                else:
                    destination = state["current_airport"]
                    if destination not in airports:
                        airports[destination] = {
                            "code": destination,
                            "name": destination,
                            "discovery_scope": "observed_destination",
                        }
                    open_states.append(state)
            except (RouteEngineError, SCOPE_COVERAGE.ScopeCoverageError) as exc:
                new_rejected.append(
                    rejection_record(parent_id, offer, str(exc), state, source)
                )

        if ordered_sources:
            request_id = str(group["request_id"])
            request = actionable_requests[request_id]
            stats = ordered_request_stats.setdefault(
                request_id,
                {
                    "request": request,
                    "source_id": str(group["source_id"]),
                    "source_index": int(request["required_source_index"]),
                    "observation_kinds": [],
                    "query_ids": [],
                    "usable_count": 0,
                    "coverage_reports": [],
                },
            )
            stats["observation_kinds"].append(str(group["observation_kind"]))
            stats["query_ids"].append(identity[0])
            stats["coverage_reports"].append(coverage_reports[identity[0]])
            stats["usable_count"] += (
                len(open_states) + len(newly_completed) - usable_before
            )

    source_attempts_added = 0
    if ordered_sources:
        for request_id, stats in ordered_request_stats.items():
            request = stats["request"]
            kinds = set(stats["observation_kinds"])
            source_index = int(stats["source_index"])
            source_id = str(stats["source_id"])
            next_source_index: int | None = None
            reports = list(stats["coverage_reports"])
            partition_required = any(
                report.get("scope_partitions") for report in reports
            )
            coverage_complete = bool(reports) and all(
                report.get("coverage_complete") is True for report in reports
            )
            if partition_required:
                outcome = "partitioned"
                reason = "scope_partitioned_without_losing_supported_subscope"
                retain_parent_ids.add(request["parent_id"])
            elif stats["usable_count"] > 0 and not coverage_complete:
                outcome = "pending"
                reason = "usable_offer_admitted_but_scope_remains_incomplete"
                retain_parent_ids.add(request["parent_id"])
            elif stats["usable_count"] > 0:
                outcome = "satisfied"
                reason = "usable_offer_admitted"
            elif kinds & ORDERED_PENDING_KINDS:
                outcome = "pending"
                reason = "scope_not_conclusively_searched"
                retain_parent_ids.add(request["parent_id"])
            elif kinds & ORDERED_TECHNICAL_FALLBACK_KINDS:
                if source_index + 1 < len(STRICT_SOURCE_ORDER):
                    outcome = "fallback"
                    next_source_index = source_index + 1
                    reason = (
                        "source_unavailable"
                        if "source_unavailable" in kinds
                        else "scope_unsupported"
                    )
                    retain_parent_ids.add(request["parent_id"])
                else:
                    outcome = "exhausted"
                    reason = "technical_fallback_chain_exhausted"
            else:
                challenger_eligible = (
                    source_id == PRIMARY_SOURCE_ID
                    and request.get("request_kind")
                    in BUSINESS_CHALLENGER_REQUEST_KINDS
                )
                challenger_index = STRICT_SOURCE_ORDER.index(
                    BUSINESS_CHALLENGER_SOURCE_ID
                )
                if challenger_eligible and challenger_index > source_index:
                    outcome = "fallback"
                    next_source_index = challenger_index
                    reason = (
                        "challenger_after_no_market_result"
                        if kinds & ORDERED_BUSINESS_NO_MATCH_KINDS
                        else "challenger_after_hard_rejection"
                    )
                    retain_parent_ids.add(request["parent_id"])
                else:
                    outcome = "exhausted"
                    reason = (
                        "no_market_result"
                        if kinds & ORDERED_BUSINESS_NO_MATCH_KINDS
                        else "offers_hard_rejected"
                    )
                if (
                    outcome == "exhausted"
                    and request.get("request_kind") == "closure_probe"
                    and int(request.get("remaining_legs", 0)) > 1
                ):
                    retain_parent_ids.add(request["parent_id"])
            try:
                coverage_outcome = (
                    "unresolved"
                    if outcome == "exhausted"
                    and reason == "technical_fallback_chain_exhausted"
                    else outcome
                )
                coverage_update = SCOPE_COVERAGE.record_outcome(
                    scope_coverage,
                    request,
                    reports[0],
                    outcome=coverage_outcome,
                    reason=reason,
                    source_index=source_index,
                    source_count=len(STRICT_SOURCE_ORDER),
                    next_source_index=next_source_index,
                )
            except SCOPE_COVERAGE.ScopeCoverageError as exc:
                raise RouteEngineError(
                    f"cannot record scope coverage for {request_id}: {exc}"
                ) from exc
            if coverage_update["logical_status"] == "pending":
                retain_parent_ids.add(request["parent_id"])
            source_attempts_added += 1

    deferred_parent_ids = set(missing) | retain_parent_ids

    ranked_open = deduplicate_states(open_states)
    requestable_ids = {
        request["parent_id"]
        for request in build_search_requests(
            {"config": config, "airports": airports, "frontier": ranked_open}
        )
    }
    no_window = [state for state in ranked_open if state["id"] not in requestable_ids]
    for state in no_window:
        new_rejected.append(
            {
                "parent_id": state["parent_id"],
                "reason": no_search_window_reason(state, config),
                "scored_state": state,
            }
        )
    ranked_requestable = [
        state for state in ranked_open if state["id"] in requestable_ids
    ]
    for group, matching_source, identity in zip(
        groups, validated_sources, group_identities, strict=True
    ):
        query_id, content_sha256 = identity
        entry = {
            "query_id": query_id,
            "content_sha256": content_sha256,
            "round": int(session.get("audit", {}).get("search_round", 0)) + 1,
            "parent_id": str(group["parent_id"]),
            "source_name": matching_source["name"],
            "source_url": matching_source["url"],
            "observed_at": matching_source["observed_at"],
        }
        if ordered_sources:
            entry.update(
                {
                    "request_id": str(group["request_id"]),
                    "source_id": str(group["source_id"]),
                    "observation_kind": str(group["observation_kind"]),
                    "submitted_scope": copy.deepcopy(group["submitted_scope"]),
                    "coverage_complete": bool(group["coverage_complete"]),
                    "scope_partitions": copy.deepcopy(group["scope_partitions"]),
                    "acquisition": copy.deepcopy(group["acquisition"]),
                }
            )
        ledger["entries"].append(entry)
    ledger["queries_used"] = len(ledger["entries"])
    ledger["remaining_queries"] = (
        None
        if ledger["max_browser_queries"] is None
        else ledger["max_browser_queries"] - ledger["queries_used"]
    )

    deferred_states = [
        copy.deepcopy(parent_map[parent_id])
        for parent_id in deferred_parent_ids
    ]
    selected_candidates = deduplicate_states(deferred_states + ranked_requestable)
    selected, date_anchor_pruned = limit_date_anchors_per_destination(
        selected_candidates, config
    )
    for state in date_anchor_pruned:
        new_rejected.append(
            {
                "parent_id": state.get("parent_id"),
                "reason": "date_anchor_limit_per_destination",
                "scored_state": state,
                "limit": config["date_anchor_limit_per_destination"],
            }
        )
    selected_ids = {state["id"] for state in selected}
    dfs_open_states = [
        state for state in ranked_requestable if state["id"] in selected_ids
    ]
    route_nodes = session.setdefault("route_nodes", new_route_graph())
    for state in [*ranked_open, *newly_completed]:
        _register_route_state(route_nodes, state)
    dfs_ledger = ensure_ordered_dfs_ledger(session)
    for state in date_anchor_pruned:
        if state["id"] in dfs_ledger["nodes"]:
            dfs_ledger["nodes"][state["id"]]["status"] = "pruned"
    ordered_dfs = update_ordered_dfs(
        session,
        parent_map,
        covered_order,
        retain_parent_ids,
        dfs_open_states,
        newly_completed,
    )
    completed = deduplicate_states(
        list(session.get("completed", [])) + newly_completed
    )

    session["frontier"] = selected
    session["completed"] = completed
    session["browser_query_ledger"] = ledger
    session["scope_coverage"] = scope_coverage
    audit = session.setdefault("audit", {})
    search_round = int(audit.get("search_round", 0)) + 1
    rejection_reasons = audit.setdefault("rejection_reasons", {})
    for record in new_rejected:
        reason = str(record.get("reason") or "unknown")
        rejection_reasons[reason] = int(rejection_reasons.get(reason, 0)) + 1
    audit["search_round"] = search_round
    audit["shadow_count"] = int(audit.get("shadow_count", 0)) + len(new_shadows)
    audit["rejection_count"] = int(audit.get("rejection_count", 0)) + len(
        new_rejected
    )
    audit["last_round"] = {
        "round": search_round,
        "parents_expanded": len(covered),
        "parents_deferred": len(deferred_parent_ids),
        "offers_evaluated": evaluated_count,
        "frontier_selected": len(selected),
        "completed_added": len(newly_completed),
        "browser_queries_used": ledger["queries_used"],
        "browser_queries_remaining": ledger["remaining_queries"],
        "source_attempts_added": source_attempts_added,
    }
    compact_route_memberships(session)
    refresh_search_requests(session)
    return session


def airport_context(airports: dict[str, Any], code: str) -> dict[str, str]:
    entry = airports.get(code, {})
    return {
        "pp_lounge": str(entry.get("pp_lounge", "unknown")),
        "overnight": str(entry.get("overnight", "unknown")),
        "timezone": str(entry.get("timezone", "unknown")),
    }


def closure_price_budgets(
    state: dict[str, Any], config: dict[str, Any], airports: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    current = state["current_airport"]
    if current not in airports:
        return {}
    current_distance = sum(float(item["distance_km"]) for item in state["segments"])
    current_price = sum(
        float(item["normalized_price"]) for item in state["segments"]
    )
    floor = float(
        config["quality_gate"]["minimum_complete_loop_efficiency_km_per_base_currency_unit"]
    )
    target = float(
        config["quality_gate"]["target_complete_loop_efficiency_km_per_base_currency_unit"]
    )
    budgets: dict[str, dict[str, Any]] = {}
    for destination in config["return_airports"]:
        if destination not in airports:
            continue
        if any(
            field not in entry
            for entry in (airports[current], airports[destination])
            for field in ("latitude", "longitude")
        ):
            continue
        final_distance = haversine_km(airports[current], airports[destination])
        projected_distance = current_distance + final_distance
        budgets[destination] = {
            "base_currency": config["base_currency"],
            "projected_total_distance_km": round(projected_distance, 1),
            "maximum_final_price_for_minimum": round(
                max(projected_distance / floor - current_price, 0.0), 2
            ),
            "maximum_final_price_for_target": round(
                max(projected_distance / target - current_price, 0.0), 2
            ),
        }
    return budgets


def per_leg_price_cap(config: dict[str, Any]) -> dict[str, Any] | None:
    amount = config["quality_gate"]["maximum_leg_price"]
    if amount is None:
        return None
    return {
        "comparison": "strictly_less_than",
        "amount": amount,
        "currency": config["quality_gate"]["maximum_leg_price_currency"],
    }


def build_search_requests(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a pure projection of the next requests without mutating Session."""
    normalized = normalize_session_storage(copy.deepcopy(session))
    try:
        coverage_ledger = SCOPE_COVERAGE.normalize_ledger(
            normalized.get("scope_coverage")
        )
    except SCOPE_COVERAGE.ScopeCoverageError as exc:
        raise RouteEngineError(f"invalid scope coverage state: {exc}") from exc
    return _project_search_requests(normalized, coverage_ledger)


def refresh_search_requests(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Register new logical scopes, then refresh the persisted request projection."""
    normalize_session_storage(session)
    try:
        coverage_ledger = SCOPE_COVERAGE.normalize_ledger(
            session.get("scope_coverage")
        )
        requests = _project_search_requests(session, coverage_ledger)
    except SCOPE_COVERAGE.ScopeCoverageError as exc:
        raise RouteEngineError(f"invalid scope coverage state: {exc}") from exc
    session["scope_coverage"] = coverage_ledger
    session.pop("next_search_requests", None)
    return requests


def _project_search_requests(
    session: dict[str, Any], coverage_ledger: dict[str, Any]
) -> list[dict[str, Any]]:
    config = session["config"]
    airports = session.get("airports", {})
    ledger = normalized_browser_query_ledger(session, config)
    if (
        ledger["remaining_queries"] is not None
        and ledger["remaining_queries"] <= 0
    ):
        return []
    budget_echo = {
        "max_browser_queries": ledger["max_browser_queries"],
        "queries_used": ledger["queries_used"],
        "remaining_queries": ledger["remaining_queries"],
    }
    primary_requests: list[dict[str, Any]] = []
    closure_probes: list[dict[str, Any]] = []
    for state in route_states(session, "frontier"):
        if (
            ledger["remaining_queries"] is not None
            and len(primary_requests) >= ledger["remaining_queries"]
        ):
            break
        if state["id"] == ROOT_ID:
            primary_requests.append(
                {
                    "request_id": "root:initial",
                    "request_kind": "initial",
                    "parent_id": ROOT_ID,
                    "origins": config["departure_airports"],
                    "destinations": ["ANYWHERE"],
                    "earliest_departure": config["trip_start"],
                    "latest_departure": config["trip_end"],
                    "first_leg_earliest_local_time": config[
                        "first_leg_earliest_local_time"
                    ],
                    "must_return": False,
                    "remaining_legs": config["max_legs"],
                    "remaining_trip_minutes": remaining_trip_minutes(state, config),
                    "closure_required": False,
                    "closure_pool": list(config["return_airports"]),
                    "traveler_count": config["traveler_count"],
                    "baggage_policy": copy.deepcopy(config["baggage_policy"]),
                    "search_budget": copy.deepcopy(budget_echo),
                    "source_strategy": copy.deepcopy(config["source_strategy"]),
                    "per_leg_price_cap": per_leg_price_cap(config),
                    "preferred_trip_duration_hours": config[
                        "preferred_trip_duration_hours"
                    ],
                    "browser_offer_collection_instruction": "Submit every complete offer visible in the executed result scope; do not preselect by origin, destination, date, price, or efficiency.",
                }
            )
            continue

        remaining = config["max_legs"] - int(state["used_legs"])
        if remaining <= 0:
            continue
        arrival = parse_datetime(state["arrival"], "state.arrival")
        duration_deadline = preferred_route_deadline(state, config)
        earliest = arrival + timedelta(
            minutes=config["connection_buffer_minutes"]
        )
        latest = min(
            arrival + timedelta(minutes=config["max_connection_wait_minutes"]),
            duration_deadline,
            key=utc,
        )
        if utc(latest) < utc(earliest):
            continue
        timezone_name = airports.get(state["current_airport"], {}).get("timezone")
        local_arrival = arrival
        local_duration_deadline = duration_deadline
        time_basis = "fixed_offset_from_arrival"
        time_warning = (
            "Airport timezone is unknown; verify local clock times across DST changes."
        )
        airport_timezone = None
        if timezone_name:
            try:
                airport_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                time_warning = (
                    "Airport timezone data is unavailable in this Python runtime; "
                    "verify local clock times across DST changes."
                )
        if airport_timezone is not None:
            local_arrival = arrival.astimezone(airport_timezone)
            earliest = earliest.astimezone(airport_timezone)
            latest = latest.astimezone(airport_timezone)
            local_duration_deadline = duration_deadline.astimezone(airport_timezone)
            time_basis = timezone_name
            time_warning = ""
        must_return = remaining == 1
        request = {
                "request_id": f"{state['id']}:{'closure' if must_return else 'continuation'}",
                "request_kind": "closure" if must_return else "continuation",
                "parent_id": state["id"],
                "route": [
                    state["segments"][0]["origin"],
                    *[segment["destination"] for segment in state["segments"]],
                ],
                "origins": [state["current_airport"]],
                "destinations": config["return_airports"]
                if must_return
                else ["ANYWHERE"],
                "earliest_departure": iso(earliest),
                "latest_departure": iso(latest),
                "must_return": must_return,
                "remaining_legs": remaining,
                "remaining_trip_minutes": remaining_trip_minutes(state, config),
                "closure_required": must_return,
                "closure_pool": list(config["return_airports"]),
                "traveler_count": config["traveler_count"],
                "baggage_policy": copy.deepcopy(config["baggage_policy"]),
                "search_budget": copy.deepcopy(budget_echo),
                "source_strategy": copy.deepcopy(config["source_strategy"]),
                "per_leg_price_cap": per_leg_price_cap(config),
                "preferred_trip_duration_hours": config[
                    "preferred_trip_duration_hours"
                ],
                "route_deadline": iso(local_duration_deadline),
                "time_basis": time_basis,
                "time_warning": time_warning,
                "current_airport_context": airport_context(
                    airports, state["current_airport"]
                ),
                "may_cross_night": latest.date() > local_arrival.date(),
                "browser_offer_collection_instruction": "Submit every complete offer visible in the executed result scope; do not preselect by origin, destination, date, price, or efficiency.",
            }
        if must_return:
            request["final_price_budgets"] = closure_price_budgets(
                state, config, airports
            )
        primary_requests.append(request)

        early = config["early_return_search"]
        if (
            not must_return
            and early["enabled"]
            and int(state["used_legs"]) >= early["start_after_used_legs"]
        ):
            closure = copy.deepcopy(request)
            closure["request_id"] = f"{state['id']}:closure"
            closure["request_kind"] = "closure_probe"
            closure["destinations"] = list(config["return_airports"])
            closure["must_return"] = True
            closure["closure_required"] = False
            closure["final_price_budgets"] = closure_price_budgets(
                state, config, airports
            )
            closure_probes.append(closure)
    if ledger["remaining_queries"] is None:
        requests = primary_requests + closure_probes
    else:
        remaining_after_primary = max(
            ledger["remaining_queries"] - len(primary_requests), 0
        )
        requests = primary_requests + closure_probes[:remaining_after_primary]
    partitioned_requests: list[dict[str, Any]] = []
    for request in requests:
        partitioned_requests.extend(
            SCOPE_COVERAGE.materialize_requests(coverage_ledger, request)
        )
    requests = partitioned_requests
    ordered_dfs = copy.deepcopy(session.get("ordered_dfs"))
    if not isinstance(ordered_dfs, dict) or not isinstance(
        ordered_dfs.get("nodes"), dict
    ):
        ordered_dfs = new_ordered_dfs_ledger()
    next_candidate_id = ordered_dfs.get("next_candidate_id")
    active_path = ordered_dfs.get("active_path", [])
    actionable_requests: list[dict[str, Any]] = []
    ordered_sources = config["source_strategy"]["selection_mode"] == "ordered"
    for request in requests:
        if ordered_sources:
            source_index = int(request.get("starting_source_index", 0))
            if source_index < 0:
                raise RouteEngineError(
                    f"invalid starting_source_index for {request['request_id']}"
                )
            if source_index >= len(STRICT_SOURCE_ORDER):
                continue
            source_id = STRICT_SOURCE_ORDER[source_index]
            request["required_source_id"] = source_id
            request["required_source_index"] = source_index
            request["required_source_order"] = list(STRICT_SOURCE_ORDER)
        dfs_node = ordered_dfs["nodes"].get(request["parent_id"], {})
        request["decision_owner"] = "engine" if ordered_sources else "ai"
        request["execution_required"] = ordered_sources
        request["dfs_recommended_parent"] = (
            request["parent_id"] == next_candidate_id
        )
        request["dfs_active_path"] = copy.deepcopy(active_path)
        request["dfs_sibling_efficiency_rank"] = dfs_node.get(
            "sibling_efficiency_rank"
        )
        if ordered_sources:
            request["selection_instruction"] = (
                f"Execute only source_id={request['required_source_id']} for this request. "
                "If only part of the airport or date scope is representable, publish exact "
                "scope_partitions: retry the supported partition on this source first and advance "
                "only the unsupported remainder. A whole-scope technical source_unavailable or "
                "scope_unsupported advances through the fallback chain. A business "
                "no_market_result or hard-rejected offer ends an ordinary "
                "continuation; only root initial and closure probes may receive one Google "
                "Flights challenger after an AZair business miss."
            )
        else:
            request["selection_instruction"] = (
                "Continue down the highest-ranked child of the current DFS parent. Backtrack to its next sibling only after the active child subtree is a hard dead end. AI may override with a recorded reason."
            )
        actionable_requests.append(request)
    return sorted(
        actionable_requests,
        key=lambda request: (
            not request["dfs_recommended_parent"],
            0 if request["request_kind"] == "continuation" else 1,
            request["parent_id"],
        ),
    )


def route_label(state: dict[str, Any]) -> str:
    if not state.get("segments"):
        return "ROOT"
    return " -> ".join(
        [state["segments"][0]["origin"]]
        + [segment["destination"] for segment in state["segments"]]
    )


def report_text(session: dict[str, Any]) -> str:
    session = normalize_session_storage(copy.deepcopy(session))
    config = session["config"]
    completed = route_states(session, "completed")
    audit_state = session.get("audit", {})
    lines = [
        f"Rounds: {int(audit_state.get('search_round', 0))}",
        f"Open frontier: {len(route_states(session, 'frontier'))}",
        f"Completed routes: {len(completed)}",
        f"Shadows: {int(audit_state.get('shadow_count', 0))}",
        f"Rejected/pruned records: {int(audit_state.get('rejection_count', 0))}",
    ]
    audit = finalization_audit(session)
    coverage = audit.get("scope_coverage_audit", {})
    lines.extend(
        [
            f"Finalization outcome: {audit.get('outcome')}",
            f"Scope coverage complete: {bool(coverage.get('complete'))}",
            f"Pending coverage requests: {len(coverage.get('pending_request_ids', []))}",
            f"Unresolved coverage requests: {len(coverage.get('unresolved_request_ids', []))}",
        ]
    )
    if completed:
        lines.append("\nCompleted:")
        for index, state in enumerate(completed, start=1):
            lines.append(
                f"  {index}. {route_label(state)} | {state['total_price']:.2f} "
                f"{config['base_currency']} | {state['total_distance_km']:.1f} km | "
                f"{state['efficiency_km_per_base_currency_unit']:.3f} km per "
                f"{config['base_currency']} | {state['trip_duration_hours']:.1f} h"
            )
            for leg_index, segment in enumerate(state["segments"], start=1):
                flight_no = str(segment.get("flight_no") or "UNKNOWN")
                lines.append(
                    f"      Leg {leg_index}: {flight_no} | "
                    f"{segment['origin']}->{segment['destination']} | "
                    f"{float(segment['distance_km']):.1f} km | "
                    f"{float(segment['normalized_price']):.2f} "
                    f"{config['base_currency']} | "
                    f"{float(segment['efficiency_km_per_base_currency_unit']):.3f} "
                    f"km per {config['base_currency']}"
                )
    return "\n".join(lines)


def invalidate_finalization(session: dict[str, Any], reason: str) -> None:
    if session.get("finalization_status") == "final":
        session.setdefault("finalization_history", []).append(
            {
                "event": "invalidated",
                "reason": reason,
                "round": int(session.get("audit", {}).get("search_round", 0)),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
    session["finalization_status"] = "open"


def finalization_audit(session: dict[str, Any]) -> dict[str, Any]:
    session = normalize_session_storage(copy.deepcopy(session))
    blockers: list[str] = []
    completed = route_states(session, "completed")
    selected = sorted(completed, key=state_sort_key)[0] if completed else None
    try:
        coverage = SCOPE_COVERAGE.audit(
            session.get("scope_coverage") or SCOPE_COVERAGE.new_ledger()
        )
    except SCOPE_COVERAGE.ScopeCoverageError as exc:
        coverage = {
            "complete": False,
            "pending_request_ids": [],
            "unresolved_request_ids": [],
            "error": str(exc),
        }
    open_frontier = len(route_states(session, "frontier"))
    next_requests = len(build_search_requests(session))
    next_candidate_id = session.get("ordered_dfs", {}).get("next_candidate_id")
    exhausted = open_frontier == 0 and next_requests == 0 and next_candidate_id is None
    if selected is not None:
        outcome = "route_found"
    elif exhausted and coverage.get("complete"):
        outcome = "bounded_no_result"
    else:
        outcome = "open"
        blockers.append("no_quality_qualified_complete_loop")
        if coverage.get("unresolved_request_ids"):
            blockers.append("unresolved_scope_coverage")
        elif coverage.get("pending_request_ids"):
            blockers.append("incomplete_scope_coverage")
        elif not exhausted:
            blockers.append("search_work_remains")
    return {
        "can_finalize": not blockers,
        "blockers": blockers,
        "outcome": outcome,
        "completed_route_count": len(completed),
        "selected_route_id": selected.get("id") if selected else None,
        "open_frontier_count": open_frontier,
        "next_request_count": next_requests,
        "next_candidate_id": next_candidate_id,
        "scope_coverage_audit": coverage,
        "rule": (
            "A final route requires a rebuilt, quality-qualified complete loop. A finalized "
            "bounded no-result requires an exhausted tree and complete persisted coverage for "
            "every required scope partition. Open siblings remain disclosed and do not imply "
            "global optimality."
        ),
    }


def export_result(
    session: dict[str, Any], *, search_status: str, reason: str | None = None
) -> dict[str, Any]:
    session = normalize_session_storage(copy.deepcopy(session))
    if search_status not in {"draft", "final"}:
        raise RouteEngineError("search_status must be draft or final")
    audit = finalization_audit(session)
    if search_status == "final" and not audit["can_finalize"]:
        raise RouteEngineError(
            "finalize-result blocked: " + ", ".join(audit["blockers"])
        )
    completed = route_states(session, "completed")
    selected = next(
        (
            route
            for route in completed
            if route.get("id") == audit["selected_route_id"]
        ),
        None,
    )
    return {
        "result_version": ENGINE_VERSION,
        "product_id": "random_loop",
        "search_status": search_status,
        "incomplete": search_status != "final",
        "outcome": audit["outcome"],
        "reason": reason,
        "selected_route": copy.deepcopy(selected),
        "completed_routes": copy.deepcopy(completed),
        "finalization_audit": audit,
        "search_audit": copy.deepcopy(session.get("audit", {})),
        "browser_query_ledger": copy.deepcopy(session.get("browser_query_ledger", {})),
        "scope_coverage": copy.deepcopy(session.get("scope_coverage", {})),
        "ordered_dfs": copy.deepcopy(session.get("ordered_dfs", {})),
        "unsearched_scope": (
            "Every required persisted scope partition reached a terminal coverage disposition; "
            "Provider inventory remains bounded by the observed sources and is not a global "
            "market guarantee."
            if audit["outcome"] == "bounded_no_result"
            else "Any unvisited DFS sibling, uncovered date/source cell, or unobserved airport "
            "offer remains unsearched; this result is not a global optimum."
        ),
    }


def finalize_result(
    session_raw: dict[str, Any], reason: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = copy.deepcopy(session_raw)
    normalize_session_storage(session)
    reason = str(reason).strip()
    if not reason:
        raise RouteEngineError("finalization reason is required")
    result = export_result(session, search_status="final", reason=reason)
    session["finalization_status"] = "final"
    session.setdefault("finalization_history", []).append(
        {
            "event": "finalized",
            "reason": reason,
            "round": int(session.get("audit", {}).get("search_round", 0)),
            "route_id": result["finalization_audit"]["selected_route_id"],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    return session, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate and prune multi-leg flexible flight routes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a new Product Session")
    init_parser.add_argument("--config", required=True)
    init_parser.add_argument("--output", required=True)

    expand_parser = subparsers.add_parser(
        "expand", help="Score one complete layer of gathered offers"
    )
    expand_parser.add_argument("--session", required=True)
    expand_parser.add_argument("--candidates", required=True)
    expand_parser.add_argument("--output", required=True)

    report_parser = subparsers.add_parser("report", help="Print a compact summary")
    report_parser.add_argument("--session", required=True)

    requests_parser = subparsers.add_parser(
        "requests", help="Print the next browser-search requests"
    )
    requests_parser.add_argument("--session", required=True)

    draft_parser = subparsers.add_parser(
        "export-draft", help="Write an explicitly incomplete structured result"
    )
    draft_parser.add_argument("--session", required=True)
    draft_parser.add_argument("--output", required=True)

    finalize_parser = subparsers.add_parser(
        "finalize-result", help="Persist a final result after the completion gate"
    )
    finalize_parser.add_argument("--session", required=True)
    finalize_parser.add_argument("--output", required=True)
    finalize_parser.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            config, airports = load_config_and_airports(args.config)
            write_json(args.output, init_session(config, airports))
        elif args.command == "expand":
            session = read_json(args.session)
            candidates = read_json(args.candidates)
            write_json(args.output, expand_session(session, candidates))
        elif args.command == "report":
            print(report_text(read_json(args.session)))
        elif args.command == "requests":
            session = read_json(args.session)
            print(
                json.dumps(
                    build_search_requests(session),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "export-draft":
            write_json(
                args.output,
                export_result(read_json(args.session), search_status="draft"),
            )
        elif args.command == "finalize-result":
            session, result = finalize_result(read_json(args.session), args.reason)
            write_json(args.session, session)
            write_json(args.output, result)
        return 0
    except RouteEngineError as exc:
        print(f"route_engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
