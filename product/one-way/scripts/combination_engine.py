from __future__ import annotations

import argparse
import copy
import hashlib
import heapq
import importlib.util
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


ENGINE_VERSION = "0.7.1"
TRIP_MODE = "fixed_destination"
NODE_TYPES = {"airport", "railway_station", "bus_terminal", "ferry_terminal", "city_destination"}
MODES = {"flight", "rail", "bus", "coach", "ferry", "local_transit", "walk", "rideshare", "car"}
POOL_POLICIES = {"exact", "city_region", "extended_region"}
COST_CATEGORIES = {
    "origin_ground_cost",
    "airfare",
    "intermediate_ground_cost",
    "destination_ground_cost",
    "required_accommodation_cost",
    "other_mandatory_costs",
}
COST_CATEGORY_ALIASES = {
    "origin_ground": "origin_ground_cost",
    "intermediate_ground": "intermediate_ground_cost",
    "destination_ground": "destination_ground_cost",
    "required_accommodation": "required_accommodation_cost",
    "other_mandatory": "other_mandatory_costs",
}
AIRPORT_TRANSIT_UNKNOWN_POLICIES = {"allow_with_warning", "reject"}
ACCOMMODATION_COST_POLICIES = {
    "include_required",
    "exclude_from_optimization",
}


def display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


SURFACE_MODES = MODES - {"flight"}
MODE_TRANSITION_BUFFER_KEYS = (
    "surface_to_flight",
    "self_transfer_to_flight",
    "flight_to_flight",
    "flight_to_surface",
    "surface_to_surface",
)
CORE_MODE_TRANSITION_BUFFER_DEFAULTS = {
    key: 0 for key in MODE_TRANSITION_BUFFER_KEYS
}


def _load_airport_transit_module() -> Any:
    module_path = Path(__file__).resolve().with_name("airport_transit.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_airport_transit", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load airport transit module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AIRPORT_TRANSIT = _load_airport_transit_module()


def _load_frontier_search_module() -> Any:
    module_path = Path(__file__).resolve().with_name("frontier_search.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_frontier_search", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load frontier search module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FRONTIER_SEARCH = _load_frontier_search_module()


def _load_residual_budget_module() -> Any:
    module_path = Path(__file__).resolve().with_name("residual_budget_search.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_residual_budget_search", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load residual-budget module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RESIDUAL_BUDGET_SEARCH = _load_residual_budget_module()


def _load_temporal_corridor_module() -> Any:
    module_path = Path(__file__).resolve().with_name("temporal_corridor_scheduler.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_temporal_corridor_scheduler", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load temporal corridor module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEMPORAL_CORRIDOR_SCHEDULER = _load_temporal_corridor_module()


class PlannerError(ValueError):
    pass


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlannerError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PlannerError(f"{field} must be an ISO date-time")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PlannerError(f"{field} must be an ISO date-time with offset") from exc
    if parsed.tzinfo is None:
        raise PlannerError(f"{field} must include a UTC offset")
    return parsed


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise PlannerError(f"{field} must be non-empty")
    return result


def http_url(value: Any, field: str) -> str:
    url = text(value, field)
    if any(character.isspace() for character in url):
        raise PlannerError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise PlannerError(f"{field} must be an absolute HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise PlannerError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise PlannerError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PlannerError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < minimum:
        raise PlannerError(f"{field} must be at least {minimum}")
    return result


def integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise PlannerError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PlannerError(f"{field} must be an integer") from exc
    if result < minimum:
        raise PlannerError(f"{field} must be at least {minimum}")
    return result


def normalize_mode_transition_buffers(raw: Any, field: str) -> dict[str, int]:
    """Normalize explicit boundary buffers without changing existing configurations.

    The core defaults to zero and still applies ``minimum_connection_minutes``.
    New real scenarios are materialized with safer non-zero defaults.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PlannerError(f"{field} must be an object")
    unknown = sorted(set(raw) - set(MODE_TRANSITION_BUFFER_KEYS))
    if unknown:
        raise PlannerError(f"{field} has unsupported keys: {', '.join(unknown)}")
    result = dict(CORE_MODE_TRANSITION_BUFFER_DEFAULTS)
    for key, value in raw.items():
        result[key] = integer(value, f"{field}.{key}", minimum=0)
    return result


def mode_transition_kind(previous_edge: dict[str, Any], next_edge: dict[str, Any]) -> str:
    previous_mode = str(previous_edge.get("mode", "unknown")).lower()
    next_mode = str(next_edge.get("mode", "unknown")).lower()
    if previous_mode == "flight" and next_mode == "flight":
        previous_group = previous_edge.get("independent_ticket_group")
        next_group = next_edge.get("independent_ticket_group")
        if previous_group and next_group and previous_group == next_group:
            return "flight_to_flight"
        return "self_transfer_to_flight"
    if previous_mode in SURFACE_MODES and next_mode == "flight":
        return "surface_to_flight"
    if previous_mode == "flight" and next_mode in SURFACE_MODES:
        return "flight_to_surface"
    return "surface_to_surface"


def mode_transition_buffer_minutes(
    previous_edge: dict[str, Any], next_edge: dict[str, Any], config: dict[str, Any]
) -> tuple[int, str]:
    kind = mode_transition_kind(previous_edge, next_edge)
    configured = config["search"]["mode_transition_buffers_minutes"][kind]
    return max(config["search"]["minimum_connection_minutes"], configured), kind


def conservative_transition_buffer_minutes(
    previous_modes: Iterable[str],
    next_edge: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, list[str]]:
    candidates = []
    for mode in previous_modes:
        minutes, kind = mode_transition_buffer_minutes(
            {"mode": mode}, next_edge, config
        )
        candidates.append((minutes, kind))
    if not candidates:
        return config["search"]["minimum_connection_minutes"], []
    required = max(minutes for minutes, _ in candidates)
    return required, sorted({kind for minutes, kind in candidates if minutes == required})


def string_list(value: Any, field: str, *, required: bool = True, upper: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise PlannerError(f"{field} must be an array")
    items: list[str] = []
    for raw in value:
        item = text(raw, field)
        item = item.upper() if upper else item.lower()
        if item not in items:
            items.append(item)
    if required and not items:
        raise PlannerError(f"{field} must not be empty")
    return items


def money(value: Any, field: str, default_currency: str) -> dict[str, Any]:
    if value is None:
        value = {"amount": 0, "currency": default_currency}
    if not isinstance(value, dict):
        raise PlannerError(f"{field} must be an object")
    return {
        "amount": number(value.get("amount", 0), f"{field}.amount"),
        "currency": text(value.get("currency", default_currency), f"{field}.currency").upper(),
    }


def validate_fx_snapshot(
    raw: Any, base_currency: str, field: str = "fx_snapshot"
) -> dict[str, Any]:
    """Validate an AI-supplied, source-attributed FX snapshot without fetching it."""
    if not isinstance(raw, dict):
        raise PlannerError(f"{field} must be an object")
    snapshot = copy.deepcopy(raw)
    snapshot_base = text(
        snapshot.get("base_currency"), f"{field}.base_currency"
    ).upper()
    if snapshot_base != base_currency:
        raise PlannerError(
            f"{field}.base_currency must equal config.base_currency {base_currency}"
        )
    raw_rates = snapshot.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        raise PlannerError(f"{field}.rates must be a non-empty object")
    rates = {
        text(code, f"{field}.rates currency").upper(): number(
            rate, f"{field}.rates.{code}", minimum=0.0000001
        )
        for code, rate in raw_rates.items()
    }
    if snapshot_base not in rates:
        raise PlannerError(
            f"{field}.rates must explicitly include base currency {snapshot_base}"
        )
    if not math.isclose(rates[snapshot_base], 1.0, rel_tol=0, abs_tol=1e-12):
        raise PlannerError(f"{field}.rates.{snapshot_base} must equal 1")
    source_url = http_url(snapshot.get("source_url"), f"{field}.source_url")
    observed_at = parse_datetime(snapshot.get("observed_at"), f"{field}.observed_at")
    expires_at = parse_datetime(snapshot.get("expires_at"), f"{field}.expires_at")
    if utc(expires_at) <= utc(observed_at):
        raise PlannerError(f"{field}.expires_at must be after observed_at")
    return {
        "base_currency": snapshot_base,
        "rates": rates,
        "source_url": source_url,
        "provider": text(snapshot.get("provider"), f"{field}.provider"),
        "observed_at": iso(observed_at),
        "expires_at": iso(expires_at),
    }


def fx_audit(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = config.get("fx_snapshot")
    live = config.get("live_provider_session", {})
    strict = bool(live.get("require_fresh_fx_snapshot", False))
    if snapshot is None:
        return {
            "mode": "static_fx_map_unauditable",
            "base_currency": config["base_currency"],
            "rates": copy.deepcopy(config["fx_to_base"]),
            "freshness_status": "not_auditable",
            "strict_live_required": strict,
            "usable_for_new_evidence": not strict,
        }
    as_of_raw = live.get("fx_as_of") or config.get("frontier_search", {}).get(
        "search_as_of"
    )
    as_of = (
        parse_datetime(as_of_raw, "live_provider_session.fx_as_of")
        if as_of_raw is not None
        else datetime.now(timezone.utc)
    )
    observed_at = utc(
        parse_datetime(snapshot["observed_at"], "fx_snapshot.observed_at")
    )
    expires_at = utc(
        parse_datetime(snapshot["expires_at"], "fx_snapshot.expires_at")
    )
    future_observation = observed_at > utc(as_of)
    expired = expires_at <= utc(as_of)
    if future_observation:
        freshness_status = "future_observation"
    elif expired:
        freshness_status = "expired"
    else:
        freshness_status = "fresh"
    return {
        "mode": "source_attributed_snapshot",
        "base_currency": snapshot["base_currency"],
        "rates": copy.deepcopy(snapshot["rates"]),
        "provider": snapshot["provider"],
        "source_url": snapshot["source_url"],
        "observed_at": snapshot["observed_at"],
        "expires_at": snapshot["expires_at"],
        "as_of": iso(as_of),
        "freshness_status": freshness_status,
        "strict_live_required": strict,
        "usable_for_new_evidence": not future_observation and not expired,
    }


def validate_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError("profile must be an object")
    profile = copy.deepcopy(raw)
    profile["profile_version"] = ENGINE_VERSION
    profile["profile_id"] = text(profile.get("profile_id"), "profile_id")
    profile["status"] = str(profile.get("status", "draft")).lower()
    if profile["status"] not in {"draft", "confirmed"}:
        raise PlannerError("status must be draft or confirmed")
    profile["trip_mode"] = str(profile.get("trip_mode", TRIP_MODE)).lower()
    if profile["trip_mode"] != TRIP_MODE:
        raise PlannerError(f"trip_mode must be {TRIP_MODE}")
    profile["currency"] = text(profile.get("currency"), "currency").upper()
    profile["traveler_count"] = integer(
        profile.get("traveler_count"), "traveler_count", minimum=1
    )
    for field in (
        "origin_start_nodes",
        "destination_end_nodes",
        "origin_airport_nodes",
        "destination_airport_nodes",
    ):
        profile[field] = string_list(profile.get(field, []), field, required=False)
    profile["allowed_modes"] = string_list(profile.get("allowed_modes", sorted(MODES)), "allowed_modes", upper=False)
    unsupported = sorted(set(profile["allowed_modes"]) - MODES)
    if unsupported:
        raise PlannerError("unsupported allowed_modes: " + ", ".join(unsupported))
    window = profile.get("date_window")
    if not isinstance(window, dict):
        raise PlannerError("date_window must be an object")
    start = parse_datetime(window.get("start"), "date_window.start")
    end = parse_datetime(window.get("end"), "date_window.end")
    if utc(end) <= utc(start):
        raise PlannerError("date_window.end must be after date_window.start")
    profile["date_window"] = {"start": iso(start), "end": iso(end)}
    raw_date_flexibility = profile.get(
        "date_flexibility", {"mode": "bounded_window"}
    )
    if not isinstance(raw_date_flexibility, dict):
        raise PlannerError("date_flexibility must be an object")
    date_flexibility = copy.deepcopy(raw_date_flexibility)
    date_mode = str(date_flexibility.get("mode", "bounded_window")).lower()
    if date_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise PlannerError(
            "date_flexibility.mode must be exact_dates, bounded_window, or fully_flexible"
        )
    date_flexibility["mode"] = date_mode
    if date_mode == "exact_dates":
        raw_dates = date_flexibility.get(
            "exact_departure_dates", date_flexibility.get("dates")
        )
        if not isinstance(raw_dates, list) or not raw_dates:
            raise PlannerError("exact_dates requires exact_departure_dates")
        normalized_dates: list[str] = []
        start_date, end_date = utc(start).date(), utc(end).date()
        for index, value in enumerate(raw_dates):
            try:
                exact = datetime.strptime(str(value), "%Y-%m-%d").date()
            except ValueError as exc:
                raise PlannerError(
                    f"exact_departure_dates[{index}] must use YYYY-MM-DD"
                ) from exc
            if exact < start_date or exact > end_date:
                raise PlannerError(
                    f"exact_departure_dates[{index}] is outside date_window"
                )
            normalized = exact.isoformat()
            if normalized not in normalized_dates:
                normalized_dates.append(normalized)
        date_flexibility = {
            "mode": "exact_dates",
            "exact_departure_dates": normalized_dates,
        }
    profile["date_flexibility"] = date_flexibility
    raw_budget = profile.get("budget")
    profile["budget"] = (
        None
        if raw_budget is None
        else money(raw_budget, "budget", profile["currency"])
    )
    baggage = profile.get("baggage")
    if not isinstance(baggage, dict):
        raise PlannerError("baggage must be an object")
    baggage["mode"] = text(baggage.get("mode"), "baggage.mode")
    if not isinstance(baggage.get("paid_baggage"), bool):
        raise PlannerError("baggage.paid_baggage must be boolean")
    profile["baggage"] = baggage
    profile["overnight_allowed"] = profile.get("overnight_allowed")
    if not isinstance(profile["overnight_allowed"], bool):
        raise PlannerError("overnight_allowed must be boolean")
    profile["accommodation_cost_policy"] = str(
        profile.get("accommodation_cost_policy", "include_required")
    ).lower()
    if profile["accommodation_cost_policy"] not in ACCOMMODATION_COST_POLICIES:
        raise PlannerError(
            "accommodation_cost_policy must be include_required or "
            "exclude_from_optimization"
        )
    profile["self_transfer_policy"] = str(
        profile.get("self_transfer_policy") or ""
    ).lower()
    if profile["self_transfer_policy"] not in {"allow", "avoid", "never"}:
        raise PlannerError("self_transfer_policy must be allow, avoid, or never")
    entitlements = profile.get("transport_entitlements", [])
    if not isinstance(entitlements, list):
        raise PlannerError("transport_entitlements must be an array")
    normalized_entitlements = []
    for index, raw_entitlement in enumerate(entitlements):
        if not isinstance(raw_entitlement, dict):
            raise PlannerError(f"transport_entitlements[{index}] must be an object")
        entitlement = copy.deepcopy(raw_entitlement)
        entitlement["entitlement_id"] = text(entitlement.get("entitlement_id"), f"transport_entitlements[{index}].entitlement_id")
        entitlement["covered_modes"] = string_list(
            entitlement.get("covered_modes", [mode for mode in profile["allowed_modes"] if mode != "flight"]),
            f"transport_entitlements[{index}].covered_modes",
            required=False,
            upper=False,
        )
        entitlement["coverage_tags"] = string_list(
            entitlement.get("coverage_tags", entitlement.get("covered_tags", [])),
            f"transport_entitlements[{index}].coverage_tags",
            required=False,
            upper=False,
        )
        raw_override = entitlement.get("cost_override", 0)
        if isinstance(raw_override, dict):
            raw_override = raw_override.get("amount", 0)
        entitlement["cost_override"] = number(raw_override, f"transport_entitlements[{index}].cost_override")
        normalized_entitlements.append(entitlement)
    profile["transport_entitlements"] = normalized_entitlements
    preferences = profile.get("scoring_preferences", {})
    if not isinstance(preferences, dict):
        raise PlannerError("scoring_preferences must be an object")
    preferences.setdefault("primary_strategy", "balanced")
    profile["scoring_preferences"] = preferences
    return profile


def validate_region_pool(raw: Any, field: str, default_currency: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError(f"{field} must be an object")
    pool = copy.deepcopy(raw)
    pool["pool_id"] = text(pool.get("pool_id"), f"{field}.pool_id")
    pool["label"] = text(pool.get("label", pool["pool_id"]), f"{field}.label")
    pool["policy"] = str(pool.get("policy", "exact")).lower()
    if pool["policy"] not in POOL_POLICIES:
        raise PlannerError(f"{field}.policy must be exact, city_region, or extended_region")
    pool["seed_nodes"] = string_list(pool.get("seed_nodes"), f"{field}.seed_nodes")
    pool["max_access_time_minutes"] = integer(
        pool.get("max_access_time_minutes", 0), f"{field}.max_access_time_minutes"
    )
    pool["max_egress_time_minutes"] = integer(
        pool.get("max_egress_time_minutes", 0), f"{field}.max_egress_time_minutes"
    )
    if "max_cost" not in pool:
        raise PlannerError(f"{field}.max_cost must be money or null")
    pool["max_cost"] = (
        None
        if pool.get("max_cost") is None
        else money(pool.get("max_cost"), f"{field}.max_cost", default_currency)
    )
    pool["allowed_modes"] = string_list(
        pool.get("allowed_modes", ["local_transit"]), f"{field}.allowed_modes", upper=False
    )
    unsupported = sorted(set(pool["allowed_modes"]) - MODES)
    if unsupported:
        raise PlannerError(f"{field} has unsupported allowed_modes: " + ", ".join(unsupported))
    expansion = pool.get("expansion_by_provider", {})
    if not isinstance(expansion, dict):
        raise PlannerError(f"{field}.expansion_by_provider must be an object")
    expansion["enabled"] = bool(expansion.get("enabled", False))
    expansion.setdefault("provider_kinds", [])
    expansion.setdefault(
        "result_scope",
        "exact_seed_nodes" if pool["policy"] == "exact" else "within_constraints",
    )
    expansion["require_observed_evidence"] = bool(
        expansion.get("require_observed_evidence", True)
    )
    if pool["policy"] == "exact" and expansion["enabled"]:
        raise PlannerError(f"{field} exact policy cannot enable provider expansion")
    pool["expansion_by_provider"] = expansion
    return pool


def load_region_pool_scenario(
    base: Path, raw_config: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    catalog_ref = raw_config.get("region_pools_file")
    scenario_id = raw_config.get("scenario_id", raw_config.get("region_pool_scenario_id"))
    inline_origin = profile.get("origin_pool")
    inline_destination = profile.get("destination_pool")
    if not catalog_ref:
        if inline_origin:
            profile["origin_pool"] = validate_region_pool(inline_origin, "origin_pool", profile["currency"])
        if inline_destination:
            profile["destination_pool"] = validate_region_pool(
                inline_destination, "destination_pool", profile["currency"]
            )
        return profile
    catalog_path = Path(str(catalog_ref))
    if not catalog_path.is_absolute():
        catalog_path = base / catalog_path
    catalog = read_json(catalog_path)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("pools"), list):
        raise PlannerError("region_pools_file must contain a pools array")
    pools: dict[str, dict[str, Any]] = {}
    for index, raw_pool in enumerate(catalog["pools"]):
        normalized = validate_region_pool(raw_pool, f"pools[{index}]", profile["currency"])
        if normalized["pool_id"] in pools:
            raise PlannerError(f"duplicate region pool {normalized['pool_id']}")
        pools[normalized["pool_id"]] = normalized
    scenarios = catalog.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise PlannerError("region pool scenarios must be an array")
    if not scenario_id:
        raise PlannerError("scenario_id is required with region_pools_file")
    selected = next(
        (scenario for scenario in scenarios if isinstance(scenario, dict) and scenario.get("scenario_id") == scenario_id),
        None,
    )
    if selected is None:
        raise PlannerError(f"unknown scenario_id {scenario_id}")
    for side in ("origin", "destination"):
        pool_id = text(selected.get(f"{side}_pool_id"), f"scenario.{side}_pool_id")
        if pool_id not in pools:
            raise PlannerError(f"scenario references unknown region pool {pool_id}")
        inline_pool = profile.get(f"{side}_pool")
        if inline_pool and inline_pool.get("pool_id") != pool_id:
            raise PlannerError(
                f"{side}_pool {inline_pool.get('pool_id')} conflicts with scenario pool {pool_id}"
            )
        profile[f"{side}_pool"] = copy.deepcopy(pools[pool_id])
    profile["region_pool_scenario_id"] = str(scenario_id)
    return profile


def pool_airport_nodes(profile: dict[str, Any], side: str) -> list[str]:
    explicit = list(profile[f"{side}_airport_nodes"])
    pool = profile.get(f"{side}_pool")
    if not pool:
        return explicit
    seeds = [node for node in pool["seed_nodes"] if len(node) == 3 and node.isalnum()]
    if pool["policy"] == "exact":
        return seeds
    return list(dict.fromkeys([*seeds, *explicit]))


def materialize_pool_endpoints(profile: dict[str, Any]) -> dict[str, Any]:
    for side, endpoint_field in (("origin", "origin_start_nodes"), ("destination", "destination_end_nodes")):
        airport_field = f"{side}_airport_nodes"
        pool = profile.get(f"{side}_pool")
        if pool and not profile[endpoint_field]:
            profile[endpoint_field] = list(pool["seed_nodes"])
        if pool and not profile[airport_field]:
            profile[airport_field] = [
                node for node in pool["seed_nodes"] if len(node) == 3 and node.isalnum()
            ]
        if not profile[endpoint_field] or not profile[airport_field]:
            raise PlannerError(
                f"{side} requires a region pool or explicit endpoint and airport nodes"
            )
    return profile


def validate_config(raw: Any, profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError("config must be an object")
    config = copy.deepcopy(raw)
    config["product_version"] = str(config.get("product_version", ENGINE_VERSION))
    config["trip_mode"] = str(config.get("trip_mode", TRIP_MODE)).lower()
    if config["trip_mode"] != TRIP_MODE:
        raise PlannerError(f"trip_mode must be {TRIP_MODE}")
    config["accommodation_cost_policy"] = str(
        config.get(
            "accommodation_cost_policy", profile["accommodation_cost_policy"]
        )
    ).lower()
    if config["accommodation_cost_policy"] not in ACCOMMODATION_COST_POLICIES:
        raise PlannerError(
            "config.accommodation_cost_policy must be include_required or "
            "exclude_from_optimization"
        )
    if config["accommodation_cost_policy"] != profile["accommodation_cost_policy"]:
        raise PlannerError(
            "config.accommodation_cost_policy must match Profile "
            "accommodation_cost_policy"
        )
    config["base_currency"] = text(config.get("base_currency", profile["currency"]), "base_currency").upper()
    config["currency_decimal_places"] = integer(
        config.get("currency_decimal_places"),
        "currency_decimal_places",
        minimum=0,
    )
    if config["currency_decimal_places"] > 6:
        raise PlannerError("currency_decimal_places must not exceed 6")
    raw_snapshot = config.get("fx_snapshot")
    snapshot = (
        validate_fx_snapshot(raw_snapshot, config["base_currency"])
        if raw_snapshot is not None
        else None
    )
    raw_rates = config.get("fx_to_base")
    if raw_rates is None:
        raw_rates = (
            snapshot["rates"] if snapshot is not None else {config["base_currency"]: 1}
        )
    if not isinstance(raw_rates, dict) or not raw_rates:
        raise PlannerError("fx_to_base must be a non-empty object")
    rates = {
        str(key).upper(): number(value, f"fx_to_base.{key}", minimum=0.0000001)
        for key, value in raw_rates.items()
    }
    rates.setdefault(config["base_currency"], 1.0)
    if snapshot is not None:
        if set(rates) != set(snapshot["rates"]) or any(
            not math.isclose(rates[code], snapshot["rates"][code], rel_tol=0, abs_tol=1e-12)
            for code in rates
        ):
            raise PlannerError("fx_to_base must match fx_snapshot.rates when both are present")
        config["fx_snapshot"] = snapshot
    config["fx_to_base"] = rates
    if profile["budget"] is not None:
        budget_currency = profile["budget"]["currency"]
        if budget_currency not in rates:
            raise PlannerError(f"no FX rate for profile.budget currency {budget_currency}")
    live = config.get("live_provider_session", {})
    if not isinstance(live, dict):
        raise PlannerError("live_provider_session must be an object")
    require_fresh = live.get("require_fresh_fx_snapshot", False)
    if not isinstance(require_fresh, bool):
        raise PlannerError(
            "live_provider_session.require_fresh_fx_snapshot must be boolean"
        )
    live["require_fresh_fx_snapshot"] = require_fresh
    if live.get("fx_as_of") is not None:
        live["fx_as_of"] = iso(
            parse_datetime(live["fx_as_of"], "live_provider_session.fx_as_of")
        )
    config["live_provider_session"] = live
    audit = fx_audit(config)
    if require_fresh and snapshot is None:
        raise PlannerError(
            "strict live FX requires a source-attributed fx_snapshot"
        )
    if require_fresh and not audit["usable_for_new_evidence"]:
        if audit["freshness_status"] == "future_observation":
            raise PlannerError(
                "strict live FX snapshot observed_at is after the configured as-of"
            )
        raise PlannerError("strict live FX snapshot is expired")
    search = config.get("search", {})
    if not isinstance(search, dict):
        raise PlannerError("search must be an object")
    defaults = {
        "max_total_travel_minutes": 4320,
        "max_transfers": 12,
        "max_routes": None,
        "labels_per_node": None,
        "connector_probe_limit": 20,
        "reference_cost": (
            profile["budget"]["amount"] if profile["budget"] is not None else None
        ),
        "cost_relaxation_factor": 1.2,
        "max_connector_gap_km": 3000,
        "minimum_connection_minutes": 0,
    }
    for key, default in defaults.items():
        search.setdefault(key, default)
    for key in ("max_total_travel_minutes", "max_transfers", "connector_probe_limit", "minimum_connection_minutes"):
        search[key] = integer(search[key], f"search.{key}", minimum=0 if key == "minimum_connection_minutes" else 1)
    for key in ("max_routes", "labels_per_node"):
        if search[key] is not None:
            search[key] = integer(search[key], f"search.{key}", minimum=1)
    search["mode_transition_buffers_minutes"] = normalize_mode_transition_buffers(
        search.get("mode_transition_buffers_minutes"),
        "search.mode_transition_buffers_minutes",
    )
    if search["reference_cost"] is not None:
        search["reference_cost"] = number(
            search["reference_cost"], "search.reference_cost"
        )
    search["cost_relaxation_factor"] = number(search["cost_relaxation_factor"], "search.cost_relaxation_factor", minimum=0.0000001)
    search["max_connector_gap_km"] = number(search["max_connector_gap_km"], "search.max_connector_gap_km", minimum=0.0000001)
    config["search"] = search
    airport_transit = config.get("airport_transit", {})
    if not isinstance(airport_transit, dict):
        raise PlannerError("airport_transit must be an object")
    unknown_policy = str(
        airport_transit.get("unknown_policy", "allow_with_warning")
    ).lower()
    if unknown_policy not in AIRPORT_TRANSIT_UNKNOWN_POLICIES:
        raise PlannerError(
            "airport_transit.unknown_policy must be allow_with_warning or reject"
        )
    airport_transit["unknown_policy"] = unknown_policy
    airside_only = airport_transit.get("airside_only", False)
    if not isinstance(airside_only, bool):
        raise PlannerError("airport_transit.airside_only must be boolean")
    airport_transit["airside_only"] = airside_only
    config["airport_transit"] = airport_transit
    return config


def validate_nodes(raw: Any) -> dict[str, dict[str, Any]]:
    source = raw.get("nodes") if isinstance(raw, dict) else raw
    if not isinstance(source, list) or not source:
        raise PlannerError("nodes must be a non-empty array")
    nodes: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise PlannerError(f"nodes[{index}] must be an object")
        node = copy.deepcopy(item)
        node_id = text(node.get("node_id"), f"nodes[{index}].node_id").upper()
        if node_id in nodes:
            raise PlannerError(f"duplicate node {node_id}")
        node_type = str(node.get("node_type", "")).lower()
        if node_type not in NODE_TYPES:
            raise PlannerError(f"node {node_id} has unsupported node_type")
        coordinates = node.get("coordinates")
        normalized_coordinates = None
        if coordinates is not None:
            if not isinstance(coordinates, dict):
                raise PlannerError(f"node {node_id} coordinates must be an object")
            latitude = number(coordinates.get("latitude"), f"node {node_id}.latitude", minimum=-90)
            longitude = number(coordinates.get("longitude"), f"node {node_id}.longitude", minimum=-180)
            if latitude > 90 or longitude > 180:
                raise PlannerError(f"node {node_id} coordinates are invalid")
            normalized_coordinates = {"latitude": latitude, "longitude": longitude}
        node.update({"node_id": node_id, "node_type": node_type, "coordinates": normalized_coordinates})
        nodes[node_id] = node
    return nodes


def validate_source(raw: Any, edge_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError(f"edge {edge_id} source must be an object")
    source = copy.deepcopy(raw)
    source["provider_id"] = text(source.get("provider_id"), f"edge {edge_id} source.provider_id")
    source["url"] = text(source.get("url"), f"edge {edge_id} source.url")
    source["observed_at"] = iso(parse_datetime(source.get("observed_at"), f"edge {edge_id} source.observed_at"))
    source["evidence_type"] = text(source.get("evidence_type", "observed"), f"edge {edge_id} source.evidence_type")
    source["confidence"] = number(source.get("confidence", 1), f"edge {edge_id} source.confidence")
    if source["confidence"] > 1:
        raise PlannerError(f"edge {edge_id} source.confidence must not exceed 1")
    if source.get("expires_at"):
        source["expires_at"] = iso(parse_datetime(source["expires_at"], f"edge {edge_id} source.expires_at"))
    return source


def validate_airport_context(raw: Any, field: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PlannerError(f"{field} must be an object")
    return {
        "terminal_id": text(raw.get("terminal_id"), f"{field}.terminal_id"),
        "zone_id": text(raw.get("zone_id"), f"{field}.zone_id"),
    }


def validate_flight_identity(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError(f"{field} must be an object")
    identity = copy.deepcopy(raw)
    carriers = []
    for carrier_field in ("marketing_carrier", "operating_carrier"):
        if identity.get(carrier_field) is not None:
            identity[carrier_field] = text(
                identity[carrier_field], f"{field}.{carrier_field}"
            )
            carriers.append(identity[carrier_field])
    if not carriers:
        raise PlannerError(
            f"{field} must include marketing_carrier or operating_carrier"
        )
    numbers = identity.get("flight_numbers")
    reason = identity.get("unknown_reason")
    if (numbers is None) == (reason is None):
        raise PlannerError(
            f"{field} must include exactly one of flight_numbers or unknown_reason"
        )
    if numbers is not None:
        identity["flight_numbers"] = string_list(
            numbers, f"{field}.flight_numbers", upper=True
        )
    else:
        if not isinstance(reason, dict):
            raise PlannerError(f"{field}.unknown_reason must be an object")
        code = text(reason.get("code"), f"{field}.unknown_reason.code")
        if code not in {
            "not_disclosed_by_source",
            "not_visible_in_search_result",
            "provider_omitted",
        }:
            raise PlannerError(f"{field}.unknown_reason.code is unsupported")
        identity["unknown_reason"] = {"code": code}
        if reason.get("detail") is not None:
            identity["unknown_reason"]["detail"] = text(
                reason["detail"], f"{field}.unknown_reason.detail"
            )
    return identity


def validate_offer_set(
    raw: Any,
    edge_id: str,
    itinerary_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlannerError(f"edge {edge_id}.offer_set must be an object")
    offer_set = copy.deepcopy(raw)
    if text(offer_set.get("itinerary_id"), f"edge {edge_id}.offer_set.itinerary_id") != itinerary_id:
        raise PlannerError(f"edge {edge_id}.offer_set itinerary_id mismatch")
    offers = offer_set.get("offers")
    if not isinstance(offers, list) or not offers:
        raise PlannerError(f"edge {edge_id}.offer_set.offers must be non-empty")
    offer_ids: set[str] = set()
    snapshot_index: dict[str, tuple[str, dict[str, Any]]] = {}
    normalized_offers: list[dict[str, Any]] = []
    for offer_index, raw_offer in enumerate(offers):
        field = f"edge {edge_id}.offer_set.offers[{offer_index}]"
        if not isinstance(raw_offer, dict):
            raise PlannerError(f"{field} must be an object")
        offer = copy.deepcopy(raw_offer)
        offer_id = text(offer.get("offer_id"), f"{field}.offer_id")
        if offer_id in offer_ids:
            raise PlannerError(f"edge {edge_id} has duplicate offer_id {offer_id}")
        offer_ids.add(offer_id)
        offer["offer_id"] = offer_id
        offer["provider"] = text(offer.get("provider"), f"{field}.provider")
        if offer.get("provider_offer_id") is not None:
            offer["provider_offer_id"] = text(
                offer["provider_offer_id"], f"{field}.provider_offer_id"
            )
        snapshots = offer.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise PlannerError(f"{field}.snapshots must be non-empty")
        normalized_snapshots: list[dict[str, Any]] = []
        for snapshot_index_number, raw_snapshot in enumerate(snapshots):
            snapshot_field = f"{field}.snapshots[{snapshot_index_number}]"
            if not isinstance(raw_snapshot, dict):
                raise PlannerError(f"{snapshot_field} must be an object")
            snapshot = copy.deepcopy(raw_snapshot)
            snapshot_id = text(snapshot.get("snapshot_id"), f"{snapshot_field}.snapshot_id")
            if snapshot_id in snapshot_index:
                raise PlannerError(f"edge {edge_id} has duplicate snapshot_id {snapshot_id}")
            snapshot["snapshot_id"] = snapshot_id
            snapshot["url"] = text(snapshot.get("url"), f"{snapshot_field}.url")
            snapshot["observed_at"] = iso(
                parse_datetime(snapshot.get("observed_at"), f"{snapshot_field}.observed_at")
            )
            if snapshot.get("expires_at") is not None:
                expires_at = parse_datetime(snapshot["expires_at"], f"{snapshot_field}.expires_at")
                if utc(expires_at) <= utc(parse_datetime(snapshot["observed_at"], f"{snapshot_field}.observed_at")):
                    raise PlannerError(f"{snapshot_field}.expires_at must be after observed_at")
                snapshot["expires_at"] = iso(expires_at)
            snapshot["price"] = money(
                snapshot.get("price"), f"{snapshot_field}.price", config["base_currency"]
            )
            baggage = snapshot.get("baggage")
            if not isinstance(baggage, dict):
                raise PlannerError(f"{snapshot_field}.baggage must be an object")
            baggage = copy.deepcopy(baggage)
            baggage["mandatory_cost"] = money(
                baggage.get("mandatory_cost"),
                f"{snapshot_field}.baggage.mandatory_cost",
                config["base_currency"],
            )
            snapshot["baggage"] = baggage
            for money_field, money_value in (
                (f"{snapshot_field}.price", snapshot["price"]),
                (f"{snapshot_field}.baggage.mandatory_cost", baggage["mandatory_cost"]),
            ):
                if money_value["currency"] not in config["fx_to_base"]:
                    raise PlannerError(
                        f"no FX rate for {money_field} currency {money_value['currency']}"
                    )
            snapshot_index[snapshot_id] = (offer_id, snapshot)
            normalized_snapshots.append(snapshot)
        offer["snapshots"] = normalized_snapshots
        normalized_offers.append(offer)
    selected_offer_id = text(
        offer_set.get("selected_offer_id"), f"edge {edge_id}.offer_set.selected_offer_id"
    )
    selected_snapshot_id = text(
        offer_set.get("selected_snapshot_id"), f"edge {edge_id}.offer_set.selected_snapshot_id"
    )
    if selected_offer_id not in offer_ids:
        raise PlannerError(f"edge {edge_id}.offer_set selected_offer_id is unknown")
    selected = snapshot_index.get(selected_snapshot_id)
    if selected is None or selected[0] != selected_offer_id:
        raise PlannerError(f"edge {edge_id}.offer_set selected_snapshot_id is inconsistent")
    offer_set["offers"] = normalized_offers
    offer_set["selected_offer_id"] = selected_offer_id
    offer_set["selected_snapshot_id"] = selected_snapshot_id
    return offer_set


def validate_edges(raw: Any, nodes: dict[str, dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    source = raw.get("edges") if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        raise PlannerError("edges must be an array")
    edges: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise PlannerError(f"edges[{index}] must be an object")
        edge = copy.deepcopy(item)
        edge_id = text(edge.get("edge_id"), f"edges[{index}].edge_id")
        if edge_id in ids:
            raise PlannerError(f"duplicate edge {edge_id}")
        ids.add(edge_id)
        from_node = text(edge.get("from_node"), f"edge {edge_id}.from_node").upper()
        to_node = text(edge.get("to_node"), f"edge {edge_id}.to_node").upper()
        if from_node not in nodes or to_node not in nodes:
            raise PlannerError(f"edge {edge_id} references an unknown node")
        mode = str(edge.get("mode", "")).lower()
        if mode not in MODES:
            raise PlannerError(f"edge {edge_id} has unsupported mode")
        departure = parse_datetime(edge.get("departure"), f"edge {edge_id}.departure")
        arrival = parse_datetime(edge.get("arrival"), f"edge {edge_id}.arrival")
        if utc(arrival) <= utc(departure):
            raise PlannerError(f"edge {edge_id} must arrive after departure")
        category = str(edge.get("cost_category", "airfare" if mode == "flight" else "intermediate_ground_cost"))
        category = COST_CATEGORY_ALIASES.get(category, category)
        if category not in COST_CATEGORIES:
            raise PlannerError(f"edge {edge_id} has unsupported cost_category")
        if category == "required_accommodation_cost" and edge.get("cost") is None:
            raise PlannerError(
                f"edge {edge_id} required accommodation needs an explicit observed cost"
            )
        tags = string_list(edge.get("entitlement_tags", []), f"edge {edge_id}.entitlement_tags", required=False, upper=False)
        edge.update({
            "edge_id": edge_id,
            "from_node": from_node,
            "to_node": to_node,
            "mode": mode,
            "departure": iso(departure),
            "arrival": iso(arrival),
            "cost": money(edge.get("cost"), f"edge {edge_id}.cost", config["base_currency"]),
            "mandatory_baggage_cost": money(edge.get("mandatory_baggage_cost"), f"edge {edge_id}.mandatory_baggage_cost", config["base_currency"]),
            "cost_category": category,
            "entitlement_tags": tags,
            "source": validate_source(edge.get("source"), edge_id),
            "border_crossing_count": integer(
                edge.get("border_crossing_count", 0),
                f"edge {edge_id}.border_crossing_count",
            ),
        })
        for money_field in ("cost", "mandatory_baggage_cost"):
            currency = edge[money_field]["currency"]
            if currency not in config["fx_to_base"]:
                raise PlannerError(
                    f"no FX rate for edge {edge_id}.{money_field} currency {currency}"
                )
        if mode == "flight" and edge.get("flight_identity") is not None:
            edge["flight_identity"] = validate_flight_identity(
                edge["flight_identity"], f"edge {edge_id}.flight_identity"
            )
        if (
            mode == "flight"
            and edge["source"].get("evidence_status") == "verified_observed"
            and edge.get("flight_identity") is None
        ):
            raise PlannerError(
                f"edge {edge_id} verified flight requires flight_identity"
            )
        if edge.get("offer_set") is not None:
            itinerary_id = text(
                edge.get("itinerary_id"), f"edge {edge_id}.itinerary_id"
            )
            edge["itinerary_id"] = itinerary_id
            edge["offer_set"] = validate_offer_set(
                edge["offer_set"], edge_id, itinerary_id, config
            )
            selected_snapshot_id = edge["offer_set"]["selected_snapshot_id"]
            selected_snapshot = next(
                snapshot
                for offer in edge["offer_set"]["offers"]
                for snapshot in offer["snapshots"]
                if snapshot["snapshot_id"] == selected_snapshot_id
            )
            if edge["cost"] != selected_snapshot["price"]:
                raise PlannerError(
                    f"edge {edge_id}.cost must equal the selected offer snapshot base fare"
                )
            if edge["mandatory_baggage_cost"] != selected_snapshot["baggage"]["mandatory_cost"]:
                raise PlannerError(
                    f"edge {edge_id}.mandatory_baggage_cost must equal the selected snapshot"
                )
        if edge.get("independent_ticket_group") is not None:
            edge["independent_ticket_group"] = text(
                edge["independent_ticket_group"],
                f"edge {edge_id}.independent_ticket_group",
            )
        for context_field in ("arrival_context", "departure_context"):
            context = validate_airport_context(
                edge.get(context_field), f"edge {edge_id}.{context_field}"
            )
            if context is None:
                edge.pop(context_field, None)
            else:
                edge[context_field] = context
        edges.append(edge)
    return sorted(edges, key=lambda item: (utc(parse_datetime(item["departure"], "departure")), item["edge_id"]))


def to_base(value: dict[str, Any], config: dict[str, Any], field: str) -> float:
    currency = value["currency"]
    if currency not in config["fx_to_base"]:
        raise PlannerError(f"no FX rate for {field} currency {currency}")
    return round(value["amount"] * config["fx_to_base"][currency], 8)


def edge_effective_cost(edge: dict[str, Any], profile: dict[str, Any], config: dict[str, Any]) -> tuple[float, str | None]:
    raw = to_base(edge["cost"], config, f"edge {edge['edge_id']}.cost")
    if edge["cost_category"] == "required_accommodation_cost":
        if profile["accommodation_cost_policy"] == "exclude_from_optimization":
            return 0.0, None
        return raw, None
    tags = set(edge["entitlement_tags"])
    for entitlement in profile["transport_entitlements"]:
        if edge["mode"] in entitlement["covered_modes"] and tags.intersection(entitlement["coverage_tags"]):
            return round(entitlement["cost_override"], 8), entitlement["entitlement_id"]
    return raw, None


def haversine_km(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    if not left.get("coordinates") or not right.get("coordinates"):
        return None
    lat1, lon1 = math.radians(left["coordinates"]["latitude"]), math.radians(left["coordinates"]["longitude"])
    lat2, lon2 = math.radians(right["coordinates"]["latitude"]), math.radians(right["coordinates"]["longitude"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def connector_mode_limits(config: dict[str, Any]) -> dict[str, float]:
    """Return independent discovery radii; the legacy surface gap never limits flights."""
    search = config["search"]
    legacy_surface_gap = number(
        search.get("max_connector_gap_km", 3000),
        "search.max_connector_gap_km",
        minimum=0.0000001,
    )
    defaults = {
        "flight": 6000.0,
        "rail": min(1500.0, legacy_surface_gap),
        "bus": min(1000.0, legacy_surface_gap),
        "coach": min(1000.0, legacy_surface_gap),
        "ferry": min(600.0, legacy_surface_gap),
        "local_transit": min(250.0, legacy_surface_gap),
        "walk": min(40.0, legacy_surface_gap),
        "rideshare": min(500.0, legacy_surface_gap),
        "car": min(1000.0, legacy_surface_gap),
    }
    if "max_flight_connector_gap_km" in search:
        defaults["flight"] = number(
            search["max_flight_connector_gap_km"],
            "search.max_flight_connector_gap_km",
            minimum=0.0000001,
        )
    overrides = search.get("connector_mode_max_gap_km", {})
    if not isinstance(overrides, dict):
        raise PlannerError("search.connector_mode_max_gap_km must be an object")
    for mode, value in overrides.items():
        normalized_mode = str(mode).lower()
        if normalized_mode not in MODES:
            raise PlannerError(
                f"search.connector_mode_max_gap_km has unsupported mode {normalized_mode}"
            )
        defaults[normalized_mode] = number(
            value,
            f"search.connector_mode_max_gap_km.{normalized_mode}",
            minimum=0.0000001,
        )
    return defaults


def connector_speed_limits(config: dict[str, Any]) -> dict[str, float]:
    defaults = {
        "flight": 900.0,
        "rail": 350.0,
        "bus": 120.0,
        "coach": 120.0,
        "ferry": 80.0,
        "local_transit": 100.0,
        "walk": 8.0,
        "rideshare": 130.0,
        "car": 130.0,
    }
    overrides = config["search"].get("connector_mode_max_speed_kmh", {})
    if not isinstance(overrides, dict):
        raise PlannerError("search.connector_mode_max_speed_kmh must be an object")
    for mode, value in overrides.items():
        normalized_mode = str(mode).lower()
        if normalized_mode not in MODES:
            raise PlannerError(
                f"search.connector_mode_max_speed_kmh has unsupported mode {normalized_mode}"
            )
        defaults[normalized_mode] = number(
            value,
            f"search.connector_mode_max_speed_kmh.{normalized_mode}",
            minimum=0.0000001,
        )
    return defaults


def initial_discovery_requests(profile: dict[str, Any]) -> list[dict[str, Any]]:
    requests = [
        {
            "request_id": "discover-forward-air-anchors",
            "request_type": "origin_market_scan",
            "direction": "depart_after",
            "origins": pool_airport_nodes(profile, "origin"),
            "destinations": ["ANYWHERE"],
            "date_window": profile["date_window"],
            "suggested_modes": ["flight"],
            "provider_kinds": ["flight_schedule_or_offer"],
            "endpoint_policy": {"from": "allowed", "to": "discovered"},
            "traveler_count": profile["traveler_count"],
            "baggage": copy.deepcopy(profile["baggage"]),
        },
        {
            "request_id": "discover-backward-air-anchors",
            "request_type": "destination_market_scan",
            "direction": "arrive_by",
            "origins": ["ANYWHERE"],
            "destinations": pool_airport_nodes(profile, "destination"),
            "date_window": profile["date_window"],
            "suggested_modes": ["flight"],
            "provider_kinds": ["flight_schedule_or_offer"],
            "endpoint_policy": {"from": "discovered", "to": "allowed"},
            "traveler_count": profile["traveler_count"],
            "baggage": copy.deepcopy(profile["baggage"]),
        },
    ]
    for request, side in zip(requests, ("origin", "destination")):
        pool = profile.get(f"{side}_pool")
        if pool:
            request["region_pool"] = {
                "pool_id": pool["pool_id"],
                "policy": pool["policy"],
                "seed_nodes": pool["seed_nodes"],
                "max_access_time_minutes": pool["max_access_time_minutes"],
                "max_egress_time_minutes": pool["max_egress_time_minutes"],
                "max_cost": pool["max_cost"],
                "allowed_modes": pool["allowed_modes"],
                "expansion_by_provider": pool["expansion_by_provider"],
            }
    return requests


def conventional_reference_request(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": "conventional-reference",
        "request_type": "conventional_reference",
        "direction": "complete_trip",
        "origins": pool_airport_nodes(profile, "origin"),
        "destinations": pool_airport_nodes(profile, "destination"),
        "date_window": profile["date_window"],
        "suggested_modes": ["flight"],
        "provider_kinds": ["flight_schedule_or_offer"],
        "endpoint_policy": {"from": "allowed", "to": "allowed"},
        "traveler_count": profile["traveler_count"],
        "baggage": copy.deepcopy(profile["baggage"]),
    }


def anchor_edges(edges: Iterable[dict[str, Any]], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    origin_airports = set(pool_airport_nodes(profile, "origin"))
    destination_airports = set(pool_airport_nodes(profile, "destination"))
    forward = [edge for edge in edges if edge["mode"] == "flight" and edge["from_node"] in origin_airports]
    backward = [edge for edge in edges if edge["mode"] == "flight" and edge["to_node"] in destination_airports]
    return forward, backward


def connector_requests(edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    forward, backward = anchor_edges(edges, profile)
    requests: list[dict[str, Any]] = []
    reference_cost = config["search"]["reference_cost"]
    threshold = (
        math.inf
        if reference_cost is None
        else reference_cost * config["search"]["cost_relaxation_factor"]
    )
    for left in forward:
        left_cost, _ = edge_effective_cost(left, profile, config)
        left_cost += to_base(left["mandatory_baggage_cost"], config, f"edge {left['edge_id']}.mandatory_baggage_cost")
        for right in backward:
            if left["to_node"] == right["from_node"]:
                continue
            if utc(parse_datetime(left["arrival"], "left arrival")) > utc(parse_datetime(right["departure"], "right departure")):
                continue
            right_cost, _ = edge_effective_cost(right, profile, config)
            right_cost += to_base(right["mandatory_baggage_cost"], config, f"edge {right['edge_id']}.mandatory_baggage_cost")
            lower_bound = left_cost + right_cost
            if lower_bound > threshold:
                continue
            distance = haversine_km(nodes[left["to_node"]], nodes[right["from_node"]])
            if distance is not None and distance > config["search"]["max_connector_gap_km"]:
                continue
            connector_modes = [mode for mode in profile["allowed_modes"] if mode != "flight"]
            left_buffer, left_rules = conservative_transition_buffer_minutes(
                [left["mode"]], {"mode": connector_modes[0] if connector_modes else "unknown"}, config
            )
            right_buffer, right_rules = conservative_transition_buffer_minutes(
                connector_modes, right, config
            )
            earliest_departure = utc(parse_datetime(left["arrival"], "left arrival"))
            earliest_departure += timedelta(minutes=left_buffer)
            latest_arrival = utc(parse_datetime(right["departure"], "right departure"))
            latest_arrival -= timedelta(minutes=right_buffer)
            if latest_arrival <= earliest_departure:
                continue
            requests.append({
                "request_id": f"bridge-{left['edge_id']}-{right['edge_id']}",
                "request_type": "discover_connector",
                "from_node": left["to_node"],
                "to_node": right["from_node"],
                "earliest_departure": iso(earliest_departure),
                "latest_arrival": iso(latest_arrival),
                "allowed_modes": connector_modes,
                "mode_transition_buffer": {
                    "before_connector_minutes": left_buffer,
                    "before_suffix_minutes": right_buffer,
                    "before_connector_rules": left_rules,
                    "before_suffix_rules": right_rules,
                },
                "anchor_edge_ids": [left["edge_id"], right["edge_id"]],
                "optimistic_total_cost": round(lower_bound, 2),
                "straight_line_gap_km": round(distance, 1) if distance is not None else None,
                "provider_order": ["structured_transit", "road_access", "targeted_browser_evidence"],
            })
    requests.sort(key=lambda item: (item["optimistic_total_cost"], item["straight_line_gap_km"] if item["straight_line_gap_km"] is not None else math.inf, item["request_id"]))
    return requests[: config["search"]["connector_probe_limit"]]


def frontier_settings(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Map the stable v0.4 planner contract onto the v0.5 frontier core."""
    search = config["search"]
    requested = config.get("frontier_search", {})
    if not isinstance(requested, dict):
        raise PlannerError("frontier_search must be an object")
    budget_in_base = (
        math.inf
        if profile["budget"] is None
        else to_base(profile["budget"], config, "profile.budget")
    )
    allowed_modes = [mode for mode in profile["allowed_modes"] if mode in MODES]
    mode_limits = connector_mode_limits(config)
    speed_limits = connector_speed_limits(config)
    ai_led = str(
        config.get("live_provider_session", {}).get(
            "source_selection_mode", "ai_led"
        )
    ).lower() == "ai_led"
    settings: dict[str, Any] = {
        "max_hops": search["max_transfers"] + 1,
        "max_labels_per_node": search["labels_per_node"] or 1_000_000,
        "max_total_cost": budget_in_base,
        "max_total_minutes": search["max_total_travel_minutes"],
        "minimum_connection_minutes": search["minimum_connection_minutes"],
        "mode_transition_buffers_minutes": copy.deepcopy(
            search["mode_transition_buffers_minutes"]
        ),
        "query_budget": search["connector_probe_limit"],
        "query_cost_relaxation_factor": search["cost_relaxation_factor"],
        "allowed_connector_modes": allowed_modes,
        "connector_mode_max_gap_km": {
            mode: mode_limits[mode] for mode in allowed_modes
        },
        "connector_mode_max_speed_kmh": {
            mode: speed_limits[mode] for mode in allowed_modes
        },
    }
    settings.update(copy.deepcopy(requested))
    if settings.get("max_labels_per_node") is None:
        settings["max_labels_per_node"] = 1_000_000
    if ai_led:
        settings["geography_quota"] = 1_000_000
        settings["mode_quota"] = 1_000_000
    if "allowed_connector_modes" in settings:
        requested_modes = string_list(
            settings["allowed_connector_modes"],
            "frontier_search.allowed_connector_modes",
            required=True,
            upper=False,
        )
        unsupported = sorted(set(requested_modes) - set(profile["allowed_modes"]))
        if unsupported:
            raise PlannerError(
                "frontier_search.allowed_connector_modes is outside Profile allowed_modes: "
                + ", ".join(unsupported)
            )
        settings["allowed_connector_modes"] = requested_modes
    return settings


def frontier_edges(
    edges: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Normalize effective prices while preserving evidence and atomic offer context."""
    adapted: list[dict[str, Any]] = []
    for edge in edges:
        item = copy.deepcopy(edge)
        effective_cost, _ = edge_effective_cost(edge, profile, config)
        # The residual engine reasons in integer minor currency units.  FX
        # multiplication may produce more decimal places, so normalize only at
        # this boundary; the source money and FX snapshot remain preserved on
        # the original edge for audit.
        item["effective_cost"] = round(effective_cost, 2)
        accommodation_observed = (
            round(
                to_base(edge["cost"], config, f"edge {edge['edge_id']}.cost"),
                2,
            )
            if edge["cost_category"] == "required_accommodation_cost"
            else 0.0
        )
        item["accommodation_cost_policy"] = profile["accommodation_cost_policy"]
        item["required_accommodation_cost_observed"] = accommodation_observed
        item["required_accommodation_cost_included"] = (
            round(effective_cost, 2)
            if edge["cost_category"] == "required_accommodation_cost"
            else 0.0
        )
        item["excluded_required_accommodation_cost"] = round(
            accommodation_observed
            - item["required_accommodation_cost_included"],
            2,
        )
        item["mandatory_baggage_cost"] = round(
            to_base(
                edge["mandatory_baggage_cost"],
                config,
                f"edge {edge['edge_id']}.mandatory_baggage_cost",
            ),
            2,
        )
        adapted.append(item)
    return adapted


def run_frontier_search(
    config: dict[str, Any],
    profile: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    if not edges:
        limit = frontier_settings(config, profile)["query_budget"]
        return {
            "provider_requests": [],
            "prune_trace": [],
            "gap_statuses": [],
            "query_budget": {
                "limit": limit,
                "used": 0,
                "remaining": limit,
                "eligible_gap_count": 0,
                "raw_gap_pair_count": 0,
                "exhausted": False,
            },
            "snapshot_audit": {
                "as_of": profile["date_window"]["start"],
                "status_counts": {},
                "eligible_edge_ids": [],
                "excluded_edge_ids": [],
                "rule": "no observed edges were supplied",
            },
            "searched_scope": {
                "observed_nodes": len(nodes),
                "observed_edges": 0,
                "forward_labels": 0,
                "backward_labels": 0,
                "complete_routes": 0,
                "claim": "initial discovery required",
            },
            "prune_reason_counts": {},
            "routes": [],
            "pareto_route_ids": [],
        }
    try:
        return FRONTIER_SEARCH.frontier_search(
            nodes,
            frontier_edges(edges, profile, config),
            profile["origin_start_nodes"],
            profile["destination_end_nodes"],
            frontier_settings(config, profile),
            complete_route_validator=route_validator_for_edges(edges, profile, config),
        )
    except FRONTIER_SEARCH.FrontierSearchError as exc:
        raise PlannerError(f"frontier search failed: {exc}") from exc


def _initial_incumbent(
    routes: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    if routes:
        best = min(
            routes,
            key=lambda item: (
                item["total_trip_cost"],
                item["total_travel_time_minutes"],
                item["id"],
            ),
        )
        return {
            "total_cost": best["total_trip_cost"],
            "edge_ids": list(best["edge_ids"]),
            "source": "observed_complete_route",
            "route_id": best["id"],
        }
    if profile["budget"] is not None:
        ceiling = to_base(profile["budget"], config, "profile.budget")
        ceiling_source = "user_budget"
    else:
        ceiling = config["search"]["reference_cost"]
        ceiling_source = "reference_cost"
    if ceiling is None:
        return {
            "total_cost": None,
            "edge_ids": [],
            "source": "unbounded",
            "route_id": None,
        }
    return {
        "total_cost": ceiling,
        "edge_ids": [],
        "source": ceiling_source,
        "route_id": None,
    }


def run_residual_budget_search(
    config: dict[str, Any],
    profile: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    initial_incumbent: dict[str, Any],
) -> dict[str, Any]:
    if initial_incumbent["total_cost"] is None:
        return {
            "search_model": "residual_budget_backward_recursive",
            "currency": config["base_currency"],
            "currency_decimal_places": config["currency_decimal_places"],
            "incumbent": {
                "initial_total_cost": None,
                "final_total_cost": None,
                "initial_edge_ids": [],
                "final_edge_ids": [],
                "initial_route_id": None,
                "final_route_id": None,
                "improved": False,
                "source": "unbounded_without_complete_route",
            },
            "routes": [],
            "reverse_labels": [],
            "remaining_budget": {"by_node": {}},
            "research_requests": [],
            "research_candidates": [],
            "trace": [],
            "prune_reason_counts": {},
        }
    requested = copy.deepcopy(config.get("frontier_search", {}))
    ai_led = str(
        config.get("live_provider_session", {}).get(
            "source_selection_mode", "ai_led"
        )
    ).lower() == "ai_led"
    residual_config = {
        "search_intensity": requested.get("search_intensity", "standard"),
        "max_hops": requested.get("max_hops", config["search"]["max_transfers"] + 1),
        "max_labels_per_node": requested.get(
            "max_labels_per_node", config["search"]["labels_per_node"]
        ) or 1_000_000,
        "query_budget": requested.get(
            "query_budget", config["search"]["connector_probe_limit"]
        ),
        "geography_quota": requested.get("geography_quota", 6),
        "mode_quota": requested.get("mode_quota", 8),
        "minimum_connection_minutes": config["search"]["minimum_connection_minutes"],
        "mode_transition_buffers_minutes": copy.deepcopy(
            config["search"]["mode_transition_buffers_minutes"]
        ),
        "max_total_minutes": config["search"]["max_total_travel_minutes"],
        "allowed_research_modes": list(profile["allowed_modes"]),
        "date_window": profile["date_window"],
        "currency_decimal_places": config["currency_decimal_places"],
    }
    if ai_led:
        residual_config["geography_quota"] = 1_000_000
        residual_config["mode_quota"] = 1_000_000
    if requested.get("search_as_of"):
        residual_config["search_as_of"] = requested["search_as_of"]
    bundle = {
        "nodes": _as_node_list(nodes),
        "edges": frontier_edges(edges, profile, config),
        "origin_nodes": profile["origin_start_nodes"],
        "destination_nodes": profile["destination_end_nodes"],
        "incumbent": initial_incumbent,
        "currency": config["base_currency"],
        "date_window": profile["date_window"],
        "config": residual_config,
    }
    try:
        return RESIDUAL_BUDGET_SEARCH.residual_budget_search(
            bundle,
            complete_route_validator=route_validator_for_edges(edges, profile, config),
        )
    except RESIDUAL_BUDGET_SEARCH.ResidualBudgetSearchError as exc:
        raise PlannerError(f"residual-budget search failed: {exc}") from exc


def _as_node_list(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(nodes[node_id]) for node_id in sorted(nodes)]


def _provider_request_from_residual(
    request: dict[str, Any], incumbent: dict[str, Any]
) -> dict[str, Any]:
    request_type = str(request.get("scheduler_task_type", "budgeted_predecessor_scan"))
    modes = list(request.get("candidate_modes", ["flight"]))
    endpoint = request.get("endpoint_policy", {})
    exact_policy = {
        "from": "exact",
        "to": "exact",
    }
    if isinstance(endpoint, dict) and endpoint.get("allow_region_expansion"):
        exact_policy = {
            "from": "discovered" if endpoint.get("from") != "exact_node" else "exact",
            "to": "discovered" if endpoint.get("to") != "exact_node" else "exact",
        }
    result = copy.deepcopy(request)
    result.update(
        {
            "request_type": request_type,
            "endpoint_policy": exact_policy,
            "origins": [request["from_node"]],
            "destinations": [request["to_node"]],
            "suggested_modes": modes,
            "provider_kinds": [f"{mode}_schedule_or_offer" for mode in modes],
            "budget_context": {
                "incumbent_route_id": incumbent.get("route_id"),
                "incumbent_total_cost": incumbent["total_trip_cost"],
                "known_prefix_cost": 0,
                "known_suffix_cost": request.get("known_suffix_cost", 0),
                "remaining_budget": request.get(
                    "remaining_budget_before_unknown_edge",
                    incumbent["total_trip_cost"],
                ),
                "ceiling_source": incumbent.get("source", "observed_complete_route"),
            },
            "consumer_contexts": [
                {
                    "known_suffix_label_id": request.get("known_suffix_label_id"),
                    "known_suffix_edge_ids": request.get("known_suffix_edge_ids", []),
                }
            ],
        }
    )
    return result


def recursive_predecessor_requests(
    residual: dict[str, Any],
    profile: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    active_incumbent: dict[str, Any],
    config: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Turn confirmed suffix labels into bounded reverse market scans.

    These are discovery tasks, not route edges.  A browser Provider may return
    a newly observed predecessor node, after which the deterministic engine
    validates and prices that edge before another recursive round is emitted.
    """
    labels = {item["label_id"]: item for item in residual.get("reverse_labels", [])}
    origins = set(profile["origin_start_nodes"])
    candidates: list[dict[str, Any]] = []
    for node_id, context in residual.get("remaining_budget", {}).get("by_node", {}).items():
        label = labels.get(context.get("label_id"), {})
        if node_id in origins or not label or int(label.get("hops", 0)) <= 0:
            continue
        remaining = float(context.get("remaining_budget", 0))
        if remaining <= 0:
            continue
        modes = [
            mode
            for mode in profile["allowed_modes"]
            if mode in {"flight", "rail", "bus", "coach", "ferry", "local_transit"}
        ]
        suffix_modes = label.get("modes", [])
        suffix_mode = suffix_modes[0] if suffix_modes else None
        latest = label.get("departure") or profile["date_window"]["end"]
        transition_buffer = config["search"]["minimum_connection_minutes"]
        transition_rules: list[str] = []
        if label.get("departure") and suffix_mode:
            transition_buffer, transition_rules = conservative_transition_buffer_minutes(
                modes,
                {
                    "mode": suffix_mode,
                    "independent_ticket_group": (
                        label.get("ticket_groups", [None])[0]
                        if label.get("ticket_groups")
                        else None
                    ),
                },
                config,
            )
            latest = iso(
                utc(parse_datetime(latest, "reverse label departure"))
                - timedelta(minutes=transition_buffer)
            )
        token = hashlib.sha256(
            f"{node_id}|{latest}|{label['label_id']}".encode("utf-8")
        ).hexdigest()[:12]
        candidates.append(
            {
                "request_id": f"reverse-anchor-{token}",
                "request_type": "budgeted_predecessor_scan",
                "origins": ["ANYWHERE"],
                "destinations": [node_id],
                "from_node": None,
                "to_node": node_id,
                "earliest_departure": profile["date_window"]["start"],
                "latest_arrival": latest,
                "mode_transition_buffer": {
                    "minutes": transition_buffer,
                    "rules": transition_rules,
                    "suffix_mode": suffix_mode,
                    "candidate_predecessor_modes": modes,
                },
                "suggested_modes": modes,
                "provider_kinds": [f"{mode}_schedule_or_offer" for mode in modes],
                "endpoint_policy": {"from": "discovered", "to": "exact"},
                "budget_context": {
                    "incumbent_route_id": active_incumbent.get("route_id"),
                    "incumbent_total_cost": active_incumbent["total_trip_cost"],
                    "known_prefix_cost": 0,
                    "known_suffix_cost": context["known_suffix_cost"],
                    "remaining_budget": context["remaining_budget"],
                    "ceiling_source": active_incumbent.get(
                        "source", "observed_complete_route"
                    ),
                },
                "consumer_contexts": [
                    {
                        "known_suffix_label_id": label["label_id"],
                        "known_suffix_edge_ids": label["edge_ids"],
                    }
                ],
                "not_a_route": True,
            }
        )
    candidates.sort(
        key=lambda item: (
            -float(item["budget_context"]["known_suffix_cost"]),
            item["to_node"],
            item["request_id"],
        )
    )
    return candidates[:limit]


def _with_default_budget_context(
    request: dict[str, Any], active_incumbent: dict[str, Any]
) -> dict[str, Any]:
    item = copy.deepcopy(request)
    item.setdefault(
        "budget_context",
        {
            "incumbent_route_id": active_incumbent.get("route_id"),
            "incumbent_total_cost": active_incumbent["total_trip_cost"],
            "known_prefix_cost": 0,
            "known_suffix_cost": 0,
            "remaining_budget": active_incumbent["total_trip_cost"],
            "ceiling_source": active_incumbent.get("source", "user_budget"),
        },
    )
    return item


def route_identifier(edge_ids: list[str]) -> str:
    digest = hashlib.sha256("|".join(edge_ids).encode("utf-8")).hexdigest()[:12]
    return f"route-{digest}"


def load_airport_transit_catalog(base: Path, config: dict[str, Any]) -> dict[str, Any]:
    catalog_ref = config.get("airport_transit_file")
    if not catalog_ref:
        return config
    catalog_path = Path(str(catalog_ref))
    if not catalog_path.is_absolute():
        catalog_path = base / catalog_path
    try:
        config["_airport_transit_catalog"] = AIRPORT_TRANSIT.load_catalog(catalog_path)
    except (AIRPORT_TRANSIT.AirportTransitError, OSError, json.JSONDecodeError) as exc:
        raise PlannerError(f"invalid airport_transit_file {catalog_path}: {exc}") from exc
    return config


def airport_transfer_audit(
    previous_edge: dict[str, Any], next_edge: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    if previous_edge["mode"] != "flight" or next_edge["mode"] != "flight":
        return None
    if previous_edge["to_node"] != next_edge["from_node"]:
        return None
    airport_id = previous_edge["to_node"]
    settings = config["airport_transit"]
    unknown_policy = settings["unknown_policy"]
    base = {
        "previous_edge_id": previous_edge["edge_id"],
        "next_edge_id": next_edge["edge_id"],
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

    catalog = config.get("_airport_transit_catalog")
    if catalog is None:
        return unknown("AIRPORT_TRANSIT_CATALOG_MISSING")
    arrival_context = previous_edge.get("arrival_context")
    departure_context = next_edge.get("departure_context")
    if not arrival_context or not departure_context:
        return unknown("AIRPORT_TRANSIT_CONTEXT_MISSING")
    request = {
        "airport_id": airport_id,
        "arrival": {
            **arrival_context,
            "local_datetime": previous_edge["arrival"],
        },
        "departure": {
            **departure_context,
            "local_datetime": next_edge["departure"],
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


def route_airport_transit_audits(
    path: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for previous_edge, next_edge in zip(path, path[1:]):
        audit = airport_transfer_audit(previous_edge, next_edge, config)
        if audit is not None:
            audits.append(audit)
    return audits


def pool_limit_in_base(pool: dict[str, Any], config: dict[str, Any], field: str) -> float:
    value = pool["max_cost"]
    if value is None:
        return math.inf
    return to_base(value, config, field)


def route_pool_audit(
    path: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    flight_indexes = [index for index, edge in enumerate(path) if edge["mode"] == "flight"]
    audit: dict[str, Any] = {"accepted": True, "reason_codes": []}
    if not flight_indexes:
        return audit
    first_flight_index, last_flight_index = flight_indexes[0], flight_indexes[-1]
    for side, segment_indexes, airport_node in (
        ("origin", range(0, first_flight_index), path[first_flight_index]["from_node"]),
        ("destination", range(last_flight_index + 1, len(path)), path[last_flight_index]["to_node"]),
    ):
        pool = profile.get(f"{side}_pool")
        if not pool:
            continue
        edges = [path[index] for index in segment_indexes]
        accepted_airports = set(pool_airport_nodes(profile, side))
        expansion = pool.get("expansion_by_provider", {})
        expansion_allowed = (
            pool.get("policy") != "exact" and bool(expansion.get("enabled"))
        )
        expected_endpoints = set(profile[f"{side}_start_nodes"] if side == "origin" else profile[f"{side}_end_nodes"])
        access_endpoint = (
            path[0]["from_node"] if side == "origin" else path[-1]["to_node"]
        )
        evidenced_expansion = (
            bool(edges)
            and access_endpoint in expected_endpoints
            and all(edge["mode"] != "flight" for edge in edges)
        )
        if airport_node not in accepted_airports and not (
            expansion_allowed and evidenced_expansion
        ):
            audit["accepted"] = False
            audit["reason_codes"].append(f"{side.upper()}_AIRPORT_OUTSIDE_POOL")
        if edges:
            duration = (
                utc(parse_datetime(edges[-1]["arrival"], "edge arrival"))
                - utc(parse_datetime(edges[0]["departure"], "edge departure"))
            ).total_seconds() / 60
        else:
            duration = 0.0
        cost = 0.0
        for edge in edges:
            effective, _ = edge_effective_cost(edge, profile, config)
            cost += effective + to_base(
                edge["mandatory_baggage_cost"], config, f"edge {edge['edge_id']}.mandatory_baggage_cost"
            )
        disallowed = sorted({edge["mode"] for edge in edges} - set(pool["allowed_modes"]))
        time_limit = pool["max_access_time_minutes"] if side == "origin" else pool["max_egress_time_minutes"]
        if duration > time_limit:
            audit["accepted"] = False
            audit["reason_codes"].append(f"{side.upper()}_GROUND_TIME_EXCEEDS_POOL")
        if cost > pool_limit_in_base(pool, config, f"{side}_pool.max_cost"):
            audit["accepted"] = False
            audit["reason_codes"].append(f"{side.upper()}_GROUND_COST_EXCEEDS_POOL")
        if disallowed:
            audit["accepted"] = False
            audit["reason_codes"].append(f"{side.upper()}_GROUND_MODE_OUTSIDE_POOL")
        audit[side] = {
            "pool_id": pool["pool_id"],
            "airport_node": airport_node,
            "airport_admission": (
                "declared_pool_airport"
                if airport_node in accepted_airports
                else "evidenced_surface_expansion"
                if expansion_allowed and evidenced_expansion
                else "rejected"
            ),
            "ground_time_minutes": round(duration, 2),
            "ground_cost": round(cost, 2),
            "ground_modes": [edge["mode"] for edge in edges],
        }
    return audit


def complete_route_audit(
    path: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Apply the same terminal route contract to every deterministic solver."""
    reason_codes: list[str] = []
    if not path:
        return {
            "accepted": False,
            "reason_codes": ["EMPTY_ROUTE"],
            "region_pool_audit": {"accepted": False, "reason_codes": ["EMPTY_ROUTE"]},
        }
    if path[0]["from_node"] not in set(profile["origin_start_nodes"]):
        reason_codes.append("ORIGIN_START_NODE_OUTSIDE_PROFILE")
    if path[-1]["to_node"] not in set(profile["destination_end_nodes"]):
        reason_codes.append("DESTINATION_END_NODE_OUTSIDE_PROFILE")
    if any(
        previous["to_node"] != following["from_node"]
        for previous, following in zip(path, path[1:])
    ):
        reason_codes.append("DISCONNECTED_PATH")
    disallowed_modes = sorted(
        {edge["mode"] for edge in path} - set(profile["allowed_modes"])
    )
    if disallowed_modes:
        reason_codes.append("ROUTE_MODE_OUTSIDE_PROFILE")

    start = utc(parse_datetime(profile["date_window"]["start"], "date_window.start"))
    end = utc(parse_datetime(profile["date_window"]["end"], "date_window.end"))
    departures = [utc(parse_datetime(edge["departure"], "edge departure")) for edge in path]
    arrivals = [utc(parse_datetime(edge["arrival"], "edge arrival")) for edge in path]
    if departures[0] < start or arrivals[-1] > end:
        reason_codes.append("ROUTE_OUTSIDE_DATE_WINDOW")
    if any(arrival <= departure for departure, arrival in zip(departures, arrivals)):
        reason_codes.append("NON_POSITIVE_EDGE_DURATION")
    for index, (previous, following) in enumerate(zip(path, path[1:])):
        required, _ = mode_transition_buffer_minutes(previous, following, config)
        if departures[index + 1] < arrivals[index] + timedelta(minutes=required):
            reason_codes.append("CONNECTION_BUFFER_VIOLATION")
            break
    elapsed = (arrivals[-1] - departures[0]).total_seconds() / 60
    if elapsed > config["search"]["max_total_travel_minutes"]:
        reason_codes.append("MAX_TOTAL_TRAVEL_TIME_EXCEEDED")
    if len(path) > config["search"]["max_transfers"] + 1:
        reason_codes.append("MAX_TRANSFERS_EXCEEDED")
    overnight_count = sum(
        max(
            0,
            (
                parse_datetime(following["departure"], "next departure").date()
                - parse_datetime(previous["arrival"], "previous arrival").date()
            ).days,
        )
        for previous, following in zip(path, path[1:])
    )
    if not profile["overnight_allowed"] and overnight_count:
        reason_codes.append("OVERNIGHT_OUTSIDE_PROFILE")
    flight_groups = [
        edge.get("independent_ticket_group")
        for edge in path
        if edge["mode"] == "flight"
    ]
    independent_transfers = sum(
        1
        for previous, following in zip(flight_groups, flight_groups[1:])
        if previous and following and previous != following
    )
    if profile["self_transfer_policy"] == "never" and independent_transfers:
        reason_codes.append("SELF_TRANSFER_OUTSIDE_PROFILE")

    airport_audits = route_airport_transit_audits(path, config)
    if any(not audit["allowed"] for audit in airport_audits):
        reason_codes.append("AIRPORT_TRANSIT_REJECTED")
    pool_audit = route_pool_audit(path, profile, config)
    reason_codes.extend(pool_audit["reason_codes"])
    reason_codes = list(dict.fromkeys(reason_codes))
    return {
        "accepted": not reason_codes,
        "reason_codes": reason_codes,
        "region_pool_audit": pool_audit,
        "airport_transit_audits": airport_audits,
    }


def route_validator_for_edges(
    edges: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]
) -> Any:
    edge_map = {edge["edge_id"]: edge for edge in edges}

    def validate(edge_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        missing = [edge_id for edge_id in edge_ids if edge_id not in edge_map]
        if missing:
            return {
                "accepted": False,
                "reason_codes": ["UNKNOWN_ROUTE_EDGE"],
                "missing_edge_ids": missing,
            }
        return complete_route_audit(
            [edge_map[edge_id] for edge_id in edge_ids], profile, config
        )

    return validate


def atomic_offer_internal_transfer_count(edge: dict[str, Any]) -> int:
    """Count evidenced internal changes without decomposing an atomic price."""
    ticket_context = edge.get("ticket_context")
    if not isinstance(ticket_context, dict):
        return 0
    if ticket_context.get("pricing_scope") != "atomic_offer":
        return 0
    component_edge_ids = ticket_context.get("component_edge_ids", [])
    via_nodes = ticket_context.get("via_nodes", [])
    if ticket_context.get("use_pattern") == "planned_exit_at_connection":
        component_edge_ids = ticket_context.get(
            "used_component_edge_ids", component_edge_ids
        )
        via_nodes = ticket_context.get("used_via_nodes", [])
    component_transfers = (
        max(0, len(component_edge_ids) - 1)
        if isinstance(component_edge_ids, list)
        else 0
    )
    evidenced_via_nodes = len(via_nodes) if isinstance(via_nodes, list) else 0
    return max(component_transfers, evidenced_via_nodes)


def atomic_offer_segment_time_breakdown(
    edge: dict[str, Any],
) -> tuple[float, float] | None:
    """Return evidenced flight and internal-wait minutes for one atomic offer.

    An atomic price remains one edge.  Its aggregate scheduled interval is only
    split when every priced component has a matching, continuous flight segment
    whose timestamps cover the edge exactly.  Partial or inconsistent segment
    evidence deliberately falls back to the aggregate edge duration.
    """
    if edge.get("mode") != "flight":
        return None
    ticket_context = edge.get("ticket_context")
    if not isinstance(ticket_context, dict):
        return None
    if ticket_context.get("pricing_scope") != "atomic_offer":
        return None
    component_ids = ticket_context.get("component_edge_ids")
    segments = ticket_context.get("segments")
    if segments is None:
        # Preserve compatibility with pre-contract direct engine inputs that
        # carried component timing evidence beside ticket_context.
        segments = edge.get("segments")
    if (
        not isinstance(component_ids, list)
        or len(component_ids) < 2
        or not isinstance(segments, list)
        or len(segments) != len(component_ids)
    ):
        return None
    if ticket_context.get("use_pattern") == "planned_exit_at_connection":
        used_component_ids = ticket_context.get("used_component_edge_ids")
        if (
            not isinstance(used_component_ids, list)
            or not used_component_ids
            or component_ids[: len(used_component_ids)] != used_component_ids
        ):
            return None
        component_ids = used_component_ids
        segments = segments[: len(used_component_ids)]

    try:
        edge_departure = utc(parse_datetime(edge.get("departure"), "edge departure"))
        edge_arrival = utc(parse_datetime(edge.get("arrival"), "edge arrival"))
    except PlannerError:
        return None

    flight_minutes = 0.0
    internal_wait_minutes = 0.0
    previous_arrival: datetime | None = None
    previous_to_node: str | None = None
    first_departure: datetime | None = None
    final_arrival: datetime | None = None
    for index, (component_id, segment) in enumerate(zip(component_ids, segments)):
        if not isinstance(segment, dict):
            return None
        if segment.get("component_id") != component_id:
            return None
        if segment.get("mode") != "flight":
            return None
        from_node = str(segment.get("from_node") or "").strip().upper()
        to_node = str(segment.get("to_node") or "").strip().upper()
        if not from_node or not to_node:
            return None
        if index == 0 and from_node != edge.get("from_node"):
            return None
        if previous_to_node is not None and from_node != previous_to_node:
            return None
        try:
            departure = utc(
                parse_datetime(
                    segment.get("departure"),
                    f"atomic segment {component_id} departure",
                )
            )
            arrival = utc(
                parse_datetime(
                    segment.get("arrival"),
                    f"atomic segment {component_id} arrival",
                )
            )
        except PlannerError:
            return None
        if arrival <= departure:
            return None
        if previous_arrival is not None:
            if departure < previous_arrival:
                return None
            internal_wait_minutes += (
                departure - previous_arrival
            ).total_seconds() / 60
        if first_departure is None:
            first_departure = departure
        flight_minutes += (arrival - departure).total_seconds() / 60
        final_arrival = arrival
        previous_arrival = arrival
        previous_to_node = to_node

    if (
        first_departure != edge_departure
        or final_arrival != edge_arrival
        or previous_to_node != edge.get("to_node")
    ):
        return None
    aggregate_minutes = (edge_arrival - edge_departure).total_seconds() / 60
    if abs(aggregate_minutes - flight_minutes - internal_wait_minutes) > 1e-6:
        return None
    return flight_minutes, internal_wait_minutes


def build_route(path: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    breakdown = {category: 0.0 for category in COST_CATEGORIES}
    breakdown["mandatory_baggage_cost"] = 0.0
    applied_entitlements: list[dict[str, str]] = []
    raw_ground_cost = 0.0
    edge_duration = 0.0
    flight_duration = 0.0
    ground_duration = 0.0
    confidence_floor = 1.0
    normalized_edges: list[dict[str, Any]] = []
    required_accommodation_cost_observed = 0.0
    required_accommodation_cost_included = 0.0
    flight_indexes = [index for index, edge in enumerate(path) if edge["mode"] == "flight"]
    first_flight_index = flight_indexes[0] if flight_indexes else len(path)
    last_flight_index = flight_indexes[-1] if flight_indexes else -1
    for edge_index, edge in enumerate(path):
        departure = parse_datetime(edge["departure"], "edge departure")
        arrival = parse_datetime(edge["arrival"], "edge arrival")
        duration = (utc(arrival) - utc(departure)).total_seconds() / 60
        active_duration = duration
        if edge["mode"] == "flight":
            atomic_time_breakdown = atomic_offer_segment_time_breakdown(edge)
            if atomic_time_breakdown is not None:
                active_duration = atomic_time_breakdown[0]
            flight_duration += active_duration
        else:
            ground_duration += duration
            raw_ground_cost += to_base(edge["cost"], config, f"edge {edge['edge_id']}.cost")
        edge_duration += active_duration
        effective_cost, entitlement = edge_effective_cost(edge, profile, config)
        baggage_cost = to_base(edge["mandatory_baggage_cost"], config, f"edge {edge['edge_id']}.mandatory_baggage_cost")
        applied_cost_category = edge["cost_category"]
        if edge["mode"] != "flight" and applied_cost_category in {
            "origin_ground_cost",
            "intermediate_ground_cost",
            "destination_ground_cost",
        }:
            if edge_index < first_flight_index:
                applied_cost_category = "origin_ground_cost"
            elif edge_index > last_flight_index:
                applied_cost_category = "destination_ground_cost"
            else:
                applied_cost_category = "intermediate_ground_cost"
        breakdown[applied_cost_category] += effective_cost
        edge_accommodation_observed = 0.0
        edge_accommodation_included = 0.0
        if edge["cost_category"] == "required_accommodation_cost":
            edge_accommodation_observed = to_base(
                edge["cost"], config, f"edge {edge['edge_id']}.cost"
            )
            edge_accommodation_included = effective_cost
            required_accommodation_cost_observed += edge_accommodation_observed
            required_accommodation_cost_included += edge_accommodation_included
        breakdown["mandatory_baggage_cost"] += baggage_cost
        if entitlement:
            applied_entitlements.append({"edge_id": edge["edge_id"], "entitlement_id": entitlement})
        confidence_floor = min(confidence_floor, edge["source"]["confidence"])
        normalized_edges.append({
            **copy.deepcopy(edge),
            "effective_cost": round(effective_cost, 2),
            "applied_entitlement": entitlement,
            "applied_cost_category": applied_cost_category,
            "required_accommodation_cost_observed": round(
                edge_accommodation_observed, 2
            ),
            "required_accommodation_cost_included": round(
                edge_accommodation_included, 2
            ),
            "excluded_required_accommodation_cost": round(
                edge_accommodation_observed - edge_accommodation_included, 2
            ),
        })
    first_departure = parse_datetime(path[0]["departure"], "first departure")
    final_arrival = parse_datetime(path[-1]["arrival"], "final arrival")
    elapsed = (utc(final_arrival) - utc(first_departure)).total_seconds() / 60
    wait = max(0.0, elapsed - edge_duration)
    total = sum(breakdown.values())
    edge_ids = [edge["edge_id"] for edge in path]
    overnight_count = 0
    for previous_edge, next_edge in zip(path, path[1:]):
        previous_arrival = parse_datetime(previous_edge["arrival"], "previous arrival")
        next_departure = parse_datetime(next_edge["departure"], "next departure")
        overnight_count += max(0, (next_departure.date() - previous_arrival.date()).days)
    flight_edges = [edge for edge in path if edge["mode"] == "flight"]
    ticket_groups = [edge.get("independent_ticket_group") for edge in flight_edges]
    independent_ticket_transfer_count = sum(
        1
        for previous_group, next_group in zip(ticket_groups, ticket_groups[1:])
        if previous_group and next_group and previous_group != next_group
    )
    self_transfer_evidence_complete = all(ticket_groups) if len(ticket_groups) > 1 else True
    if not self_transfer_evidence_complete:
        self_transfer_risk = "unknown"
    elif independent_ticket_transfer_count == 0:
        self_transfer_risk = "low"
    elif independent_ticket_transfer_count == 1:
        self_transfer_risk = "medium"
    else:
        self_transfer_risk = "high"
    external_transfer_count = max(0, len(path) - 1)
    atomic_internal_transfer_count = sum(
        atomic_offer_internal_transfer_count(edge) for edge in path
    )
    planned_exit_tickets = []
    for edge in path:
        ticket_context = edge.get("ticket_context")
        if not isinstance(ticket_context, dict) or ticket_context.get(
            "use_pattern"
        ) != "planned_exit_at_connection":
            continue
        planned_exit_tickets.append(
            {
                "edge_id": edge["edge_id"],
                "atomic_offer_id": ticket_context.get("atomic_offer_id"),
                "ticketed_final_node": ticket_context.get("ticketed_final_node"),
                "planned_exit_node": ticket_context.get("planned_exit_node"),
                "used_component_edge_ids": copy.deepcopy(
                    ticket_context.get("used_component_edge_ids", [])
                ),
                "unused_component_edge_ids": copy.deepcopy(
                    ticket_context.get("unused_component_edge_ids", [])
                ),
            }
        )
    mode_transition_audits = []
    for previous_edge, next_edge in zip(path, path[1:]):
        required_minutes, transition_kind = mode_transition_buffer_minutes(
            previous_edge, next_edge, config
        )
        available_minutes = (
            utc(parse_datetime(next_edge["departure"], "next departure"))
            - utc(parse_datetime(previous_edge["arrival"], "previous arrival"))
        ).total_seconds() / 60
        mode_transition_audits.append(
            {
                "previous_edge_id": previous_edge["edge_id"],
                "next_edge_id": next_edge["edge_id"],
                "transition_kind": transition_kind,
                "required_minutes": required_minutes,
                "available_minutes": round(available_minutes, 2),
            }
        )
    return {
        "id": route_identifier(edge_ids),
        "edge_ids": edge_ids,
        "node_path": [path[0]["from_node"], *[edge["to_node"] for edge in path]],
        "edges": normalized_edges,
        "departure": iso(first_departure),
        "arrival": iso(final_arrival),
        "accommodation_cost_policy": profile["accommodation_cost_policy"],
        "required_accommodation_cost_observed": round(
            required_accommodation_cost_observed, 2
        ),
        "required_accommodation_cost_included": round(
            required_accommodation_cost_included, 2
        ),
        "excluded_required_accommodation_cost": round(
            required_accommodation_cost_observed
            - required_accommodation_cost_included,
            2,
        ),
        "total_trip_cost": round(total, 2),
        "raw_ground_cost_before_entitlements": round(raw_ground_cost, 2),
        "cost_breakdown": {key: round(value, 2) for key, value in breakdown.items()},
        "total_travel_time_minutes": round(elapsed, 2),
        "flight_time_minutes": round(flight_duration, 2),
        "ground_time_minutes": round(ground_duration, 2),
        "wait_time_minutes": round(wait, 2),
        "transfer_count": external_transfer_count + atomic_internal_transfer_count,
        "external_transfer_count": external_transfer_count,
        "atomic_offer_internal_transfer_count": atomic_internal_transfer_count,
        "planned_exit_ticket_count": len(planned_exit_tickets),
        "planned_exit_tickets": planned_exit_tickets,
        "border_crossing_count": sum(edge.get("border_crossing_count", 0) for edge in path),
        "overnight_count": overnight_count,
        "independent_ticket_transfer_count": independent_ticket_transfer_count,
        "self_transfer_risk": self_transfer_risk,
        "self_transfer_evidence_complete": self_transfer_evidence_complete,
        "evidence_confidence_floor": round(confidence_floor, 4),
        "evidence_uncertainty": round(1 - confidence_floor, 4),
        "applied_entitlements": applied_entitlements,
        "region_pool_audit": route_pool_audit(path, profile, config),
        "airport_transit_audits": route_airport_transit_audits(path, config),
        "mode_transition_audits": mode_transition_audits,
    }


def accommodation_cost_audit_for_edge_ids(
    edge_ids: list[str],
    edges: list[dict[str, Any]],
    profile: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Audit observed lodging without changing the solver's effective-cost result."""
    edge_index = {edge["edge_id"]: edge for edge in edges}
    observed = 0.0
    included = 0.0
    for edge_id in edge_ids:
        edge = edge_index.get(edge_id)
        if edge is None or edge["cost_category"] != "required_accommodation_cost":
            continue
        observed += to_base(edge["cost"], config, f"edge {edge_id}.cost")
        effective, _ = edge_effective_cost(edge, profile, config)
        included += effective
    return {
        "accommodation_cost_policy": profile["accommodation_cost_policy"],
        "required_accommodation_cost_observed": round(observed, 2),
        "required_accommodation_cost_included": round(included, 2),
        "excluded_required_accommodation_cost": round(observed - included, 2),
    }


def attach_accommodation_cost_audit(
    routes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    profile: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    for route in routes:
        route.update(
            accommodation_cost_audit_for_edge_ids(
                list(route.get("edge_ids", [])), edges, profile, config
            )
        )
    return routes


def route_equivalence_key(route: dict[str, Any]) -> str:
    """Identify the same priced physical journey across duplicate evidence edges."""
    movement = []
    for edge in route.get("edges", []):
        movement.append(
            {
                "from_node": edge.get("from_node"),
                "to_node": edge.get("to_node"),
                "mode": edge.get("mode"),
                "departure": iso(
                    utc(parse_datetime(edge.get("departure"), "route edge departure"))
                ),
                "arrival": iso(
                    utc(parse_datetime(edge.get("arrival"), "route edge arrival"))
                ),
            }
        )
    payload = {
        "movement": movement,
        "total_trip_cost": round(float(route.get("total_trip_cost", 0)), 8),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _route_information_score(route: dict[str, Any]) -> tuple[int, float, str]:
    score = 0
    for edge in route.get("edges", []):
        score += int(isinstance(edge.get("offer_set"), dict))
        score += int(isinstance(edge.get("ticket_context"), dict))
        score += int(bool(edge.get("itinerary_id")))
        score += int(edge.get("source", {}).get("evidence_status") == "verified_observed")
    return (
        score,
        float(route.get("evidence_confidence_floor", 0)),
        str(route.get("id", "")),
    )


def deduplicate_equivalent_routes(
    routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        groups[route_equivalence_key(route)].append(route)
    deduplicated: list[dict[str, Any]] = []
    for equivalent in groups.values():
        representative = max(equivalent, key=_route_information_score)
        result = copy.deepcopy(representative)
        if len(equivalent) > 1:
            result["equivalent_route_count"] = len(equivalent)
            result["equivalent_route_variants"] = [
                {
                    "route_id": route["id"],
                    "edge_ids": copy.deepcopy(route.get("edge_ids", [])),
                    "planned_exit_tickets": copy.deepcopy(
                        route.get("planned_exit_tickets", [])
                    ),
                    "source_provider_ids": [
                        edge.get("source", {}).get("provider_id")
                        for edge in route.get("edges", [])
                    ],
                }
                for route in sorted(equivalent, key=lambda item: item["id"])
            ]
        deduplicated.append(result)
    return sorted(
        deduplicated,
        key=lambda item: (
            item["total_trip_cost"],
            item["total_travel_time_minutes"],
            item["id"],
        ),
    )


def enumerate_routes(edges: list[dict[str, Any]], profile: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        if edge["mode"] in profile["allowed_modes"]:
            outgoing[edge["from_node"]].append(edge)
    for group in outgoing.values():
        group.sort(key=lambda item: (utc(parse_datetime(item["departure"], "departure")), item["edge_id"]))
    start = utc(parse_datetime(profile["date_window"]["start"], "date_window.start"))
    end = utc(parse_datetime(profile["date_window"]["end"], "date_window.end"))
    destinations = set(profile["destination_end_nodes"])
    max_minutes = config["search"]["max_total_travel_minutes"]
    max_edges = config["search"]["max_transfers"] + 1
    max_routes = config["search"]["max_routes"]
    sequence = 0
    heap: list[tuple[float, float, int, dict[str, Any]]] = []
    for node_id in profile["origin_start_nodes"]:
        label = {"node": node_id, "arrival": start, "first_departure": None, "path": [], "visited": (node_id,), "cost": 0.0}
        heapq.heappush(heap, (0.0, start.timestamp(), sequence, label))
        sequence += 1
    per_node: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    routes: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    unique_route_keys: set[str] = set()
    while heap and (max_routes is None or len(unique_route_keys) < max_routes):
        _, _, _, label = heapq.heappop(heap)
        if label["node"] in destinations and label["path"]:
            signature = tuple(edge["edge_id"] for edge in label["path"])
            if signature not in seen_paths:
                route_audit = complete_route_audit(label["path"], profile, config)
                if route_audit["accepted"]:
                    candidate = build_route(label["path"], profile, config)
                    if not profile["overnight_allowed"] and candidate["overnight_count"]:
                        continue
                    if (
                        profile["self_transfer_policy"] == "never"
                        and candidate["independent_ticket_transfer_count"]
                    ):
                        continue
                    seen_paths.add(signature)
                    routes.append(candidate)
                    unique_route_keys.add(route_equivalence_key(candidate))
            continue
        if len(label["path"]) >= max_edges:
            continue
        feasible_edges: list[dict[str, Any]] = []
        for edge in outgoing.get(label["node"], []):
            if edge["to_node"] in label["visited"]:
                continue
            departure = utc(parse_datetime(edge["departure"], "edge departure"))
            arrival = utc(parse_datetime(edge["arrival"], "edge arrival"))
            required_ready = label["arrival"]
            if label["path"]:
                required_minutes, _ = mode_transition_buffer_minutes(
                    label["path"][-1], edge, config
                )
                required_ready = required_ready.timestamp() + required_minutes * 60
                if departure.timestamp() < required_ready:
                    continue
            elif departure < start:
                continue
            if arrival > end:
                continue
            if label["path"]:
                transit_audit = airport_transfer_audit(label["path"][-1], edge, config)
                if transit_audit is not None and not transit_audit["allowed"]:
                    continue
            feasible_edges.append(edge)
        for edge in feasible_edges:
            if edge["mode"] == "flight" and not any(item["mode"] == "flight" for item in label["path"]):
                partial_pool_audit = route_pool_audit(
                    [*label["path"], edge], profile, config
                )
                if any(
                    reason.startswith("ORIGIN_")
                    for reason in partial_pool_audit["reason_codes"]
                ):
                    continue
            departure = utc(parse_datetime(edge["departure"], "edge departure"))
            arrival = utc(parse_datetime(edge["arrival"], "edge arrival"))
            first_departure = label["first_departure"] or departure
            elapsed = (arrival - first_departure).total_seconds() / 60
            if elapsed > max_minutes:
                continue
            effective, _ = edge_effective_cost(edge, profile, config)
            baggage = to_base(edge["mandatory_baggage_cost"], config, f"edge {edge['edge_id']}.mandatory_baggage_cost")
            next_cost = label["cost"] + effective + baggage
            budget_base = (
                math.inf
                if profile["budget"] is None
                else to_base(profile["budget"], config, "profile.budget")
            )
            if next_cost > budget_base:
                continue
            key = (next_cost, arrival.timestamp(), len(label["path"]) + 1)
            labels = per_node[edge["to_node"]]
            label_limit = config["search"]["labels_per_node"]
            if label_limit is not None and len(labels) >= label_limit and key >= max(labels):
                continue
            labels.append(key)
            labels.sort()
            if label_limit is not None:
                del labels[label_limit:]
            next_label = {
                "node": edge["to_node"],
                "arrival": arrival,
                "first_departure": first_departure,
                "path": [*label["path"], edge],
                "visited": (*label["visited"], edge["to_node"]),
                "cost": next_cost,
            }
            heapq.heappush(heap, (next_cost, arrival.timestamp(), sequence, next_label))
            sequence += 1
    return deduplicate_equivalent_routes(routes)


def normalize(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high == low:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def rank_routes(routes: list[dict[str, Any]]) -> dict[str, list[str]]:
    if not routes:
        return {key: [] for key in ("lowest_price", "shortest_time", "convenience", "balanced", "extreme_savings", "pareto")}
    costs = normalize([route["total_trip_cost"] for route in routes])
    times = normalize([route["total_travel_time_minutes"] for route in routes])
    transfers = normalize([float(route["transfer_count"]) for route in routes])
    uncertainty = normalize([route["evidence_uncertainty"] for route in routes])
    for index, route in enumerate(routes):
        route["ranking_scores"] = {
            "lowest_price": round(costs[index], 6),
            "shortest_time": round(times[index], 6),
            "convenience": round(0.7 * times[index] + 0.2 * transfers[index] + 0.1 * uncertainty[index], 6),
            "balanced": round(0.5 * costs[index] + 0.4 * times[index] + 0.1 * transfers[index], 6),
            "extreme_savings": round(0.95 * costs[index] + 0.05 * uncertainty[index], 6),
        }
    rankings: dict[str, list[str]] = {}
    for strategy in ("lowest_price", "shortest_time", "convenience", "balanced", "extreme_savings"):
        rankings[strategy] = [route["id"] for route in sorted(routes, key=lambda item: (item["ranking_scores"][strategy], item["total_trip_cost"], item["id"]))]
    pareto: list[dict[str, Any]] = []
    for candidate in routes:
        dominated = any(
            other["id"] != candidate["id"]
            and other["total_trip_cost"] <= candidate["total_trip_cost"]
            and other["total_travel_time_minutes"] <= candidate["total_travel_time_minutes"]
            and other["evidence_uncertainty"] <= candidate["evidence_uncertainty"]
            and (
                other["total_trip_cost"] < candidate["total_trip_cost"]
                or other["total_travel_time_minutes"] < candidate["total_travel_time_minutes"]
                or other["evidence_uncertainty"] < candidate["evidence_uncertainty"]
            )
            for other in routes
        )
        if not dominated:
            pareto.append(candidate)
    rankings["pareto"] = [route["id"] for route in sorted(pareto, key=lambda item: (item["total_trip_cost"], item["total_travel_time_minutes"], item["id"]))]
    return rankings


def solve(config: dict[str, Any], profile: dict[str, Any], nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    routes = enumerate_routes(edges, profile, config)
    rankings = rank_routes(routes)
    frontier = run_frontier_search(config, profile, nodes, edges)
    initial_incumbent = _initial_incumbent(routes, profile, config)
    residual = run_residual_budget_search(
        config, profile, nodes, edges, initial_incumbent
    )
    attach_accommodation_cost_audit(
        frontier.get("routes", []), edges, profile, config
    )
    frontier.update(
        {
            "accommodation_cost_policy": profile["accommodation_cost_policy"],
            "cost_policy_scope": (
                "required_accommodation_cost_only; other mandatory costs remain included"
            ),
        }
    )
    attach_accommodation_cost_audit(
        residual.get("routes", []), edges, profile, config
    )
    residual.update(
        {
            "accommodation_cost_policy": profile["accommodation_cost_policy"],
            "cost_policy_scope": (
                "required_accommodation_cost_only; other mandatory costs remain included"
            ),
        }
    )
    final = residual["incumbent"]
    active_incumbent = {
        "route_id": final.get("final_route_id") or initial_incumbent.get("route_id"),
        "total_trip_cost": final["final_total_cost"],
        "currency": config["base_currency"],
        "edge_ids": list(final.get("final_edge_ids", [])),
        "source": "residual_budget_search"
        if final.get("improved")
        else initial_incumbent["source"],
    }
    incumbent_accommodation_audit = accommodation_cost_audit_for_edge_ids(
        active_incumbent["edge_ids"], edges, profile, config
    )
    active_incumbent.update(incumbent_accommodation_audit)
    residual["incumbent"].update(incumbent_accommodation_audit)
    residual_selected_ids = {
        str(request["request_id"])
        for request in residual.get("research_requests", [])
    }
    residual_requests = []
    for request in residual.get("research_candidates", []):
        item = _provider_request_from_residual(request, active_incumbent)
        item["selection_advice"] = {
            "script_recommended": str(request["request_id"]) in residual_selected_ids,
            "reason": (
                "selected_by_residual_scheduler"
                if str(request["request_id"]) in residual_selected_ids
                else "outside_script_budget_or_diversity_recommendation"
            ),
            "binding": False,
        }
        residual_requests.append(item)
    recursive_requests = recursive_predecessor_requests(
        residual,
        profile,
        nodes,
        active_incumbent,
        config,
        min(
            config["search"]["connector_probe_limit"],
            max(2, config["search"]["connector_probe_limit"] // 4),
        ),
    )
    temporal_base_requests = [
        conventional_reference_request(profile),
        *initial_discovery_requests(profile),
    ]
    try:
        temporal_corridor = TEMPORAL_CORRIDOR_SCHEDULER.schedule_temporal_corridors(
            profile,
            {"nodes": list(nodes.values())},
            {"edges": edges},
            temporal_base_requests,
            config.get("temporal_corridor_search"),
        )
    except TEMPORAL_CORRIDOR_SCHEDULER.SchedulerError as exc:
        raise PlannerError(f"temporal corridor scheduling failed: {exc}") from exc
    provider_requests: list[dict[str, Any]] = []
    seen_requests: set[str] = set()
    frontier_selected_by_id = {
        str(request["request_id"]): request
        for request in frontier.get("provider_requests", [])
    }
    frontier_selected_ids = set(frontier_selected_by_id)
    frontier_candidates = []
    for candidate_sequence, request in enumerate(
        frontier.get("unresolved_gaps", []), 1
    ):
        item = copy.deepcopy(request)
        selected_version = frontier_selected_by_id.get(str(request["request_id"]))
        item["query_sequence"] = (
            selected_version.get("query_sequence", candidate_sequence)
            if selected_version
            else candidate_sequence
        )
        item["selection_advice"] = {
            "script_recommended": str(request["request_id"]) in frontier_selected_ids,
            "reason": (
                "selected_by_frontier_scheduler"
                if str(request["request_id"]) in frontier_selected_ids
                else "outside_script_budget_or_diversity_recommendation"
            ),
            "binding": False,
        }
        frontier_candidates.append(item)
    for request in [
        *temporal_corridor.get("candidates", temporal_corridor["requests"]),
        *recursive_requests,
        *residual_requests,
        *frontier_candidates,
    ]:
        request = _with_default_budget_context(request, active_incumbent)
        request.setdefault("traveler_count", profile["traveler_count"])
        request.setdefault("baggage", copy.deepcopy(profile["baggage"]))
        request.setdefault("overnight_allowed", profile["overnight_allowed"])
        request.setdefault("self_transfer_policy", profile["self_transfer_policy"])
        request_id = str(request["request_id"])
        if request_id not in seen_requests:
            seen_requests.add(request_id)
            provider_requests.append(request)
    forward, backward = anchor_edges(edges, profile)
    used_edges = {edge_id for route in routes for edge_id in route["edge_ids"]}
    return {
        "engine_version": ENGINE_VERSION,
        "trip_mode": TRIP_MODE,
        **incumbent_accommodation_audit,
        "base_currency": config["base_currency"],
        "fx_audit": fx_audit(config),
        "profile_id": profile["profile_id"],
        "initial_discovery_requests": initial_discovery_requests(profile),
        "connector_discovery_requests": connector_requests(edges, nodes, profile, config),
        "active_incumbent": active_incumbent,
        "residual_contexts": {
            "accommodation_cost_policy": profile["accommodation_cost_policy"],
            "reverse_labels": residual["reverse_labels"],
            "remaining_budget": residual["remaining_budget"],
        },
        "residual_trace": residual["trace"],
        "residual_search": residual,
        "provider_requests": provider_requests,
        "temporal_corridor_coverage": temporal_corridor["coverage"],
        "temporal_corridors": temporal_corridor["corridors"],
        "temporal_corridor_budget": temporal_corridor["budget"],
        "temporal_corridor_pruned": temporal_corridor["pruned"],
        "frontier_trace": frontier["prune_trace"],
        "frontier_state": copy.deepcopy(frontier.get("frontiers", {"forward": [], "backward": []})),
        "frontier_coverage": {
            "gap_statuses": frontier["gap_statuses"],
            "query_budget": frontier["query_budget"],
            "snapshot_audit": frontier["snapshot_audit"],
            "searched_scope": frontier["searched_scope"],
            "prune_reason_counts": frontier["prune_reason_counts"],
        },
        "frontier_routes": frontier["routes"],
        "frontier_pareto_route_ids": frontier["pareto_route_ids"],
        "anchor_summary": {
            "forward_edge_ids": [edge["edge_id"] for edge in forward],
            "backward_edge_ids": [edge["edge_id"] for edge in backward],
            "dead_end_forward_edge_ids": [edge["edge_id"] for edge in forward if edge["edge_id"] not in used_edges],
        },
        "routes": routes,
        "rankings": rankings,
        "itinerary_offer_sets": [
            {
                "edge_id": edge["edge_id"],
                "itinerary_id": edge["itinerary_id"],
                "from_node": edge["from_node"],
                "to_node": edge["to_node"],
                "departure": edge["departure"],
                "arrival": edge["arrival"],
                **(
                    {"flight_identity": copy.deepcopy(edge["flight_identity"])}
                    if edge.get("flight_identity") is not None
                    else {}
                ),
                "offer_set": copy.deepcopy(edge["offer_set"]),
            }
            for edge in edges
            if edge.get("itinerary_id") and isinstance(edge.get("offer_set"), dict)
        ],
        "searched_scope": {
            "nodes": len(nodes),
            "observed_edges": len(edges),
            "completed_routes": len(routes),
            "connector_probe_limit": config["search"]["connector_probe_limit"],
            "origin_pool_id": profile.get("origin_pool", {}).get("pool_id"),
            "destination_pool_id": profile.get("destination_pool", {}).get("pool_id"),
            "claim_scope": "best candidates within observed edges, provider coverage, constraints, and query budget",
        },
    }


def resolve_file(base: Path, explicit: str | None, config: dict[str, Any], key: str) -> Path:
    raw = explicit or config.get(key)
    if not raw:
        raise PlannerError(f"{key} is required")
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else base / candidate


def load_bundle(config_path: str | Path, nodes_path: str | None = None, edges_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    path = Path(config_path)
    raw_config = read_json(path)
    if not isinstance(raw_config, dict):
        raise PlannerError("config must be an object")
    profile_path = resolve_file(path.parent, None, raw_config, "profile_file")
    profile = validate_profile(read_json(profile_path))
    profile = materialize_pool_endpoints(
        load_region_pool_scenario(path.parent, raw_config, profile)
    )
    config = load_airport_transit_catalog(path.parent, validate_config(raw_config, profile))
    nodes = validate_nodes(read_json(resolve_file(path.parent, nodes_path, raw_config, "nodes_file")))
    referenced = set(profile["origin_start_nodes"] + profile["destination_end_nodes"] + profile["origin_airport_nodes"] + profile["destination_airport_nodes"])
    missing = sorted(referenced - set(nodes))
    if missing:
        raise PlannerError("profile references missing nodes: " + ", ".join(missing))
    edges = validate_edges(read_json(resolve_file(path.parent, edges_path, raw_config, "edges_file")), nodes, config)
    return config, profile, nodes, edges


def load_config_and_profile(config_path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(config_path)
    raw_config = read_json(path)
    if not isinstance(raw_config, dict):
        raise PlannerError("config must be an object")
    profile = validate_profile(read_json(resolve_file(path.parent, None, raw_config, "profile_file")))
    profile = materialize_pool_endpoints(
        load_region_pool_scenario(path.parent, raw_config, profile)
    )
    return validate_config(raw_config, profile), profile


def report(result: dict[str, Any]) -> str:
    lines = [
        f"Combination engine {result.get('engine_version', 'unknown')}",
        f"Completed routes: {len(result.get('routes', []))}",
    ]
    routes = {route["id"]: route for route in result.get("routes", [])}
    for strategy in ("lowest_price", "shortest_time", "convenience", "balanced", "extreme_savings"):
        ids = result.get("rankings", {}).get(strategy, [])
        if ids:
            route = routes[ids[0]]
            lines.append(f"{strategy}: {' -> '.join(route['node_path'])} | {route['total_trip_cost']:.2f} | {route['total_travel_time_minutes']:.0f} min")
            for edge in route.get("edges", []):
                if edge.get("mode") != "flight":
                    continue
                identity = edge.get("flight_identity", {})
                carrier = identity.get("marketing_carrier") or identity.get("operating_carrier")
                numbers = identity.get("flight_numbers")
                number_display = ",".join(numbers) if numbers else (
                    "unknown:" + identity.get("unknown_reason", {}).get("code", "legacy")
                )
                offer_set = edge.get("offer_set", {})
                lines.append(
                    f"  flight: {carrier or 'carrier-unverified'} {number_display} | "
                    f"selected seller {offer_set.get('selected_provider', edge['source']['provider_id'])}"
                )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and rank fixed-destination air-ground combinations")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--config", required=True)
    requests = sub.add_parser("requests")
    requests.add_argument("--config", required=True)
    requests.add_argument("--phase", choices=["anchors", "connectors", "frontier"], default="anchors")
    solve_parser = sub.add_parser("solve")
    solve_parser.add_argument("--config", required=True)
    solve_parser.add_argument("--output", required=True)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--result", required=True)
    profile_parser = sub.add_parser("profile-validate")
    profile_parser.add_argument("--profile", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "profile-validate":
            profile = validate_profile(read_json(args.profile))
            profile = materialize_pool_endpoints(load_region_pool_scenario(Path(args.profile).parent, {}, profile))
            print(json.dumps({"valid": True, "profile": profile}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "report":
            print(report(read_json(args.result)))
            return 0
        if args.command == "requests" and args.phase == "anchors":
            _, profile = load_config_and_profile(args.config)
            print(json.dumps(initial_discovery_requests(profile), ensure_ascii=False, indent=2))
            return 0
        config, profile, nodes, edges = load_bundle(args.config)
        if args.command == "validate":
            print(json.dumps({"valid": True, "engine_version": ENGINE_VERSION, "profile_id": profile["profile_id"], "nodes": len(nodes), "edges": len(edges)}, ensure_ascii=False, indent=2))
        elif args.command == "requests":
            if args.phase == "frontier":
                frontier = run_frontier_search(config, profile, nodes, edges)
                print(json.dumps({
                    "provider_requests": frontier["provider_requests"],
                    "trace": frontier["prune_trace"],
                    "coverage": {
                        "gap_statuses": frontier["gap_statuses"],
                        "query_budget": frontier["query_budget"],
                        "snapshot_audit": frontier["snapshot_audit"],
                        "searched_scope": frontier["searched_scope"],
                    },
                }, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(connector_requests(edges, nodes, profile, config), ensure_ascii=False, indent=2))
        elif args.command == "solve":
            result = solve(config, profile, nodes, edges)
            write_json(args.output, result)
            print(json.dumps({"valid": True, "routes": len(result["routes"]), "output": display_path(args.output)}, ensure_ascii=False, indent=2))
        return 0
    except PlannerError as exc:
        print(f"combination_engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
