"""Normalize the small search request without inventing scope or source facts.

This module validates declared values. It does not interpret prose, select airports,
compile research tasks, or decide whether a source observation meets a requirement.
"""

from copy import deepcopy
from datetime import date, datetime
import math
import re


COMMON_FIELDS = {
    "base_currency", "adult_count", "ranking_basis", "objective", "budget",
    "price_target", "fx_rates", "baggage_policy", "overnight_allowed",
    "self_transfer_policy", "allowed_modes", "presence_intervals",
    "additional_requirements", "search_limits", "output", "search_effort",
    "maximum_total_duration_minutes", "airport_transit_unknown_policy",
}
FIXED_FIELDS = {"origin", "destination", "origin_airports", "destination_airports"}
SHAPE_FIELDS = {
    "round_trip": FIXED_FIELDS | {
        "outbound_departure_window", "return_departure_window", "return_arrival_window",
        "travel_windows", "outbound_exact_departure_dates", "return_exact_departure_dates",
        "minimum_stay_nights", "maximum_stay_nights", "maximum_stay_minutes",
        "maximum_outbound_duration_minutes", "maximum_return_duration_minutes",
        "maximum_transfer_count",
    },
    "one_way": FIXED_FIELDS | {"departure_window", "arrival_window", "exact_departure_dates", "maximum_transfer_count"},
    "random_loop": {
        "departure_airports", "return_airports", "first_departure_window", "return_arrival_window",
        "min_legs", "max_legs", "connection_buffer_minutes", "max_connection_wait_minutes",
        "airport_transit", "first_leg_earliest_local_time", "quality_gate",
    },
}
MODES = {"flight", "rail", "bus", "coach", "ferry", "local_transit", "walk", "rideshare", "car"}


def _object(value, path, allowed, required=()):
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"{path}: unsupported field {', '.join(sorted(unknown))}")
    missing = set(required) - set(value)
    if missing:
        raise ValueError(f"{path}: missing field {', '.join(sorted(missing))}")


def _string(value, path, pattern=None):
    if not isinstance(value, str) or not value.strip() or (pattern and not re.fullmatch(pattern, value)):
        raise ValueError(f"{path} must be a nonempty string" + (f" matching {pattern}" if pattern else ""))


def _number(value, path, minimum=0, integer=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    if (integer and not isinstance(value, int)) or value < minimum or (positive and value <= 0):
        raise ValueError(f"{path} must be {'an integer' if integer else 'a number'} "
                         f"{'greater than zero' if positive else f'at least {minimum}'}")


def _boolean(value, path):
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be boolean")


def _enum(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{path} must be one of {', '.join(sorted(choices))}")


def _array(value, path, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{path} must be {'an' if allow_empty else 'a nonempty'} array")


def _strings(value, path, pattern=None, choices=None):
    _array(value, path)
    for item in value:
        _string(item, path, pattern)
        if choices is not None:
            _enum(item, path, choices)
    if len(set(value)) != len(value):
        raise ValueError(f"{path} contains duplicate values")


def _instant(value, path, date_allowed=False):
    _string(value, path)
    try:
        if date_allowed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except ValueError as exc:
        raise ValueError(f"{path} requires {'a date or ' if date_allowed else ''}an offset timestamp") from exc


def _window(value, path, offset_only=False):
    _object(value, path, {"start", "end"}, {"start", "end"})
    start = _instant(value["start"], path + ".start", not offset_only)
    end = _instant(value["end"], path + ".end", not offset_only)
    if type(start) is not type(end) or end < start:
        raise ValueError(f"{path} must use matching date representations with end >= start")


def _money(value, path, currency):
    if value is None:
        return
    _object(value, path, {"amount", "currency"}, {"amount", "currency"})
    _number(value["amount"], path + ".amount")
    if value["currency"] != currency:
        raise ValueError(f"{path}.currency must equal base_currency {currency}; do not silently convert a user limit")


def _bounds(value, path, lower, upper):
    if lower in value and upper in value and value[lower] > value[upper]:
        raise ValueError(f"{path}.{upper} must be >= {lower}")


def _endpoint(value, path, currency):
    fields = {"region_id", "label", "country_codes", "search_geography", "policy", "endpoint_policy",
              "anchor_nodes", "seed_gateway_nodes", "max_access_time_minutes", "max_egress_time_minutes",
              "max_ground_cost", "allowed_ground_modes"}
    _object(value, path, fields, {"region_id", "label", "country_codes", "policy", "endpoint_policy", "anchor_nodes", "seed_gateway_nodes"})
    _string(value["region_id"], path + ".region_id", r"[a-z0-9][a-z0-9._-]*")
    _string(value["label"], path + ".label")
    _strings(value["country_codes"], path + ".country_codes", r"[A-Z]{2}")
    _enum(value["policy"], path + ".policy", {"exact", "city_region", "extended_region"})
    _enum(value["endpoint_policy"], path + ".endpoint_policy", {"anchor_required", "any_admitted_gateway"})
    for pool in ("anchor_nodes", "seed_gateway_nodes"):
        _array(value[pool], path + "." + pool, pool == "seed_gateway_nodes")
        for node in value[pool]:
            node_fields = {"node_id", "node_type", "location", "country_or_region", "country_code", "timezone", "utc_offset"}
            _object(node, path + "." + pool, node_fields, node_fields - {"utc_offset", "country_or_region"})
            for key in node_fields - {"utc_offset"}:
                if key in node:
                    _string(node[key], path + "." + pool + "." + key)
            _enum(node["node_type"], path + ".node_type", {"airport", "railway_station", "bus_terminal", "ferry_terminal", "city_destination", "address"})
            _string(node["country_code"], path + ".country_code", r"[A-Z]{2}")
            # Retain the declared zone without needing the host's timezone database
            # merely to save a request. The comparison layer resolves zones when
            # it has actual instants; an unresolved zone must remain unverified.
            if "utc_offset" in node:
                _instant("2000-01-01T00:00:00" + node["utc_offset"], path + ".utc_offset")
    for key in ("max_access_time_minutes", "max_egress_time_minutes"):
        if value.get(key) is not None:
            _number(value[key], path + "." + key, integer=True)
    if "max_ground_cost" in value:
        _money(value["max_ground_cost"], path + ".max_ground_cost", currency)
    if "allowed_ground_modes" in value:
        _strings(value["allowed_ground_modes"], path + ".allowed_ground_modes", choices=MODES - {"flight"})
    if "search_geography" in value:
        geography = value["search_geography"]
        _object(geography, path + ".search_geography", {"kind", "country_code"}, {"kind", "country_code"})
        _enum(geography["kind"], path + ".search_geography.kind", {"country"})
        _string(geography["country_code"], path + ".search_geography.country_code", r"[A-Z]{2}")


def normalize_request(raw: dict) -> dict:
    """Return a detached version 7 request or reject unsupported declarations.

    Version 6 values retain their meaning. Deprecated research-credit settings must
    be replaced explicitly; normalization never chooses a new limit for the user.
    """
    _object(raw, "request", {"request_version", "journey_shape", "user_request", "intent"}, {"request_version", "journey_shape", "intent"})
    _enum(raw["request_version"], "request_version", {"6.0", "7.0"})
    _enum(raw["journey_shape"], "journey_shape", set(SHAPE_FIELDS))
    request = deepcopy(raw)
    shape, intent = request["journey_shape"], request["intent"]
    _object(intent, "intent", COMMON_FIELDS | SHAPE_FIELDS[shape], {"base_currency"})
    if "search_effort" in intent:
        raise ValueError("intent.search_effort is retired: remove it and declare search_limits explicitly if desired; research credits are not carried forward")
    if "user_request" in request:
        _string(request["user_request"], "user_request")
    _string(intent["base_currency"], "intent.base_currency", r"[A-Z]{3}")
    currency = intent["base_currency"]
    intent.setdefault("adult_count", 1)
    intent.setdefault("ranking_basis", "mandatory_air_ticket_total")
    intent.setdefault("objective", "distance_per_currency" if shape == "random_loop" else "price")
    _number(intent["adult_count"], "intent.adult_count", integer=True, positive=True)
    _enum(intent["ranking_basis"], "intent.ranking_basis", {"mandatory_air_ticket_total", "complete_journey_cost"})
    _enum(intent["objective"], "intent.objective", {"price", "stay_duration", "travel_duration", "distance_per_currency"})
    if shape != "round_trip" and intent["objective"] == "stay_duration":
        raise ValueError("intent.objective stay_duration requires round_trip")
    if shape == "random_loop":
        required = {"departure_airports", "return_airports", "first_departure_window"}
    else:
        required = set()
        for side in ("origin", "destination"):
            if side not in intent and side + "_airports" not in intent:
                raise ValueError(f"intent requires {side} or {side}_airports; cities are not converted to airports")
            if side in intent and side + "_airports" in intent:
                raise ValueError(f"intent must use either {side} or {side}_airports, not both")
        if shape == "one_way":
            required.add("departure_window")
        elif "travel_windows" not in intent:
            required.add("outbound_departure_window")
    if required - set(intent):
        raise ValueError(f"intent missing required fields: {', '.join(sorted(required - set(intent)))}")
    for key, value in intent.items():
        path = "intent." + key
        if key.endswith("_window"):
            _window(value, path)
        elif key.endswith("_airports"):
            _strings(value, path, r"[A-Z]{3}")
        elif key in {"origin", "destination"}:
            _endpoint(value, path, currency)
        elif key in {"budget", "price_target"}:
            _money(value, path, currency)
        elif key.endswith("_departure_dates"):
            _strings(value, path, r"\d{4}-\d{2}-\d{2}")
            for item in value:
                _instant(item, path, True)
        elif key in {"minimum_stay_nights", "maximum_stay_nights", "maximum_stay_minutes", "maximum_transfer_count", "connection_buffer_minutes"}:
            _number(value, path, integer=True)
        elif key in {"min_legs", "max_legs", "max_connection_wait_minutes", "maximum_total_duration_minutes", "maximum_outbound_duration_minutes", "maximum_return_duration_minutes"}:
            _number(value, path, integer=True, positive=True)
    _bounds(intent, "intent", "minimum_stay_nights", "maximum_stay_nights")
    _bounds(intent, "intent", "min_legs", "max_legs")
    _bounds(intent, "intent", "connection_buffer_minutes", "max_connection_wait_minutes")
    if ("connection_buffer_minutes" in intent and "max_connection_wait_minutes" in intent
            and intent["connection_buffer_minutes"] == intent["max_connection_wait_minutes"]):
        raise ValueError("intent.max_connection_wait_minutes must be greater than connection_buffer_minutes")
    if "travel_windows" in intent:
        if "outbound_departure_window" in intent or "return_departure_window" in intent:
            raise ValueError("intent.travel_windows cannot be combined with top-level departure windows")
        _array(intent["travel_windows"], "intent.travel_windows")
        for window in intent["travel_windows"]:
            _object(window, "intent.travel_windows[]", {"label", "outbound_departure_window", "return_departure_window", "minimum_stay_nights", "maximum_stay_nights"}, {"outbound_departure_window", "return_departure_window"})
            for key, value in window.items():
                if key.endswith("_window"):
                    _window(value, "intent.travel_windows[]." + key)
                elif key.endswith("_nights"):
                    _number(value, "intent.travel_windows[]." + key, integer=True)
                else:
                    _string(value, "intent.travel_windows[]." + key)
            _bounds(window, "intent.travel_windows[]", "minimum_stay_nights", "maximum_stay_nights")
    for dates_key, window_key in (("exact_departure_dates", "departure_window"),
                                  ("outbound_exact_departure_dates", "outbound_departure_window"),
                                  ("return_exact_departure_dates", "return_departure_window")):
        if dates_key not in intent:
            continue
        windows = ([intent[window_key]] if window_key in intent else
                   [window[window_key] for window in intent.get("travel_windows", []) if window_key in window])
        if not windows:
            raise ValueError(f"intent.{dates_key} requires its corresponding departure window")
        for departure_date in intent[dates_key]:
            if not any(window["start"][:10] <= departure_date <= window["end"][:10] for window in windows):
                raise ValueError(f"intent.{dates_key}: {departure_date} is outside its departure windows")
    _validate_options(intent)
    request["request_version"] = "7.0"
    return request


def _validate_options(intent):
    for key in ("fx_rates", "presence_intervals", "additional_requirements"):
        if key in intent:
            _array(intent[key], "intent." + key, True)
    for rate in intent.get("fx_rates", []):
        fields = {"from_currency", "to_currency", "rate", "observed_at", "source_url", "source_name"}
        _object(rate, "intent.fx_rates[]", fields, {"from_currency", "to_currency", "rate", "observed_at"})
        for key in ("from_currency", "to_currency"):
            _string(rate[key], "intent.fx_rates[]." + key, r"[A-Z]{3}")
        _number(rate["rate"], "intent.fx_rates[].rate", positive=True)
        _instant(rate["observed_at"], "intent.fx_rates[].observed_at")
        # A source name can attribute the numeric rate when its link is absent,
        # expired or truncated. Link syntax must not reject the whole Request.
        for field in ("source_url", "source_name"):
            if field in rate and not isinstance(rate[field], str):
                rate[field] = None
    for interval in intent.get("presence_intervals", []):
        _object(interval, "intent.presence_intervals[]", {"start", "end", "location_node_id"}, {"start", "end", "location_node_id"})
        _window({key: interval[key] for key in ("start", "end")}, "intent.presence_intervals[]", True)
        _string(interval["location_node_id"], "intent.presence_intervals[].location_node_id")
    identifiers = set()
    for requirement in intent.get("additional_requirements", []):
        _object(requirement, "intent.additional_requirements[]", {"requirement_id", "text"}, {"requirement_id", "text"})
        for key in ("requirement_id", "text"):
            _string(requirement[key], "intent.additional_requirements[]." + key)
        if requirement["requirement_id"] in identifiers:
            raise ValueError("intent.additional_requirements contains a duplicate requirement_id")
        identifiers.add(requirement["requirement_id"])
    if "baggage_policy" in intent:
        baggage = intent["baggage_policy"]
        fields = {"cabin_baggage", "checked_bag_count", "paid_baggage_allowed"}
        _object(baggage, "intent.baggage_policy", fields, fields)
        _enum(baggage["cabin_baggage"], "intent.baggage_policy.cabin_baggage", {"none_required", "personal_item", "cabin_bag", "personal_item_and_cabin_bag"})
        _number(baggage["checked_bag_count"], "intent.baggage_policy.checked_bag_count", integer=True)
        _boolean(baggage["paid_baggage_allowed"], "intent.baggage_policy.paid_baggage_allowed")
    if "overnight_allowed" in intent:
        _boolean(intent["overnight_allowed"], "intent.overnight_allowed")
    if "self_transfer_policy" in intent:
        _enum(intent["self_transfer_policy"], "intent.self_transfer_policy", {"allow", "never"})
    if "allowed_modes" in intent:
        _strings(intent["allowed_modes"], "intent.allowed_modes", choices=MODES)
    if "search_limits" in intent:
        limits = intent["search_limits"]
        _object(limits, "intent.search_limits", {"max_queries", "max_elapsed_minutes"})
        for key, value in limits.items():
            _number(value, "intent.search_limits." + key, integer=key == "max_queries", positive=True)
    if "output" in intent:
        output = intent["output"]
        _object(output, "intent.output", {"view", "limit"}, {"view"})
        _enum(output["view"], "intent.output.view", {"ranked", "matrix"})
        if "limit" in output:
            _number(output["limit"], "intent.output.limit", integer=True, positive=True)
    if "airport_transit_unknown_policy" in intent:
        _enum(intent["airport_transit_unknown_policy"], "intent.airport_transit_unknown_policy", {"allow_with_warning", "reject"})
    if "airport_transit" in intent:
        transit = intent["airport_transit"]
        _object(transit, "intent.airport_transit", {"unknown_policy", "airside_only"})
        if "unknown_policy" in transit:
            _enum(transit["unknown_policy"], "intent.airport_transit.unknown_policy", {"allow_with_warning", "reject"})
        if "airside_only" in transit:
            _boolean(transit["airside_only"], "intent.airport_transit.airside_only")
    if "first_leg_earliest_local_time" in intent:
        times = intent["first_leg_earliest_local_time"]
        if not isinstance(times, dict):
            raise ValueError("intent.first_leg_earliest_local_time must be an object")
        for airport, value in times.items():
            _string(airport, "intent.first_leg_earliest_local_time airport", r"[A-Z]{3}")
            _string(value, "intent.first_leg_earliest_local_time time", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
    if "quality_gate" in intent:
        gate = intent["quality_gate"]
        fields = {"minimum_complete_loop_efficiency_km_per_base_currency_unit", "maximum_leg_price_in_base_currency"}
        _object(gate, "intent.quality_gate", fields)
        for key, value in gate.items():
            if value is None and key == "maximum_leg_price_in_base_currency":
                continue
            _number(value, "intent.quality_gate." + key, positive=True)
