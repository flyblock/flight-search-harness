"""Deterministic complete-journey solver for atomic round-trip offer portfolios."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


VERSION = "0.4.0"
ALLOWED_MODES = {
    "flight",
    "rail",
    "bus",
    "coach",
    "ferry",
    "local_transit",
    "walk",
    "rideshare",
    "car",
}
SURFACE_MODES = ALLOWED_MODES - {"flight"}
OFFER_SCOPES = {"joint_round_trip", "one_way", "atomic_itinerary", "surface"}
PORTFOLIO_KINDS = (
    "joint_round_trip",
    "split_one_ways",
    "nested_round_trips",
    "mixed_atomic_offers",
)
DEFAULT_BUFFERS = {
    "surface_to_flight": 120,
    "self_transfer_to_flight": 120,
    "flight_to_flight": 60,
    "flight_to_surface": 0,
    "surface_to_surface": 0,
}
STAY_NIGHT_COUNT_SEMANTICS = "destination_endpoint_local_calendar_date_difference"


class RoundTripError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoundTripError(f"{field} must be a non-empty string")
    return value.strip()


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field)
    if any(character.isspace() for character in url):
        raise RoundTripError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RoundTripError(f"{field} must be an absolute HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise RoundTripError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RoundTripError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def _dt(value: Any, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RoundTripError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoundTripError(f"{field} must include a UTC offset")
    return parsed


def _amount(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoundTripError(f"{field} must be numeric")
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        raise RoundTripError(f"{field} must be a finite non-negative number")
    return number


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RoundTripError(f"{field} must be an integer >= {minimum}")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        raise RoundTripError(f"{field} must be {qualifier}")
    result = [_text(item, f"{field}[]") for item in value]
    if len(result) != len(set(result)):
        raise RoundTripError(f"{field} must not contain duplicates")
    return result


def _window(raw: Any, field: str) -> tuple[datetime, datetime]:
    if not isinstance(raw, dict):
        raise RoundTripError(f"{field} must be an object")
    start = _dt(raw.get("start"), f"{field}.start")
    end = _dt(raw.get("end"), f"{field}.end")
    if end < start:
        raise RoundTripError(f"{field}.end must not precede start")
    return start, end


def _exact_departure_dates(raw: Any, field: str) -> set[date]:
    values = _string_list(raw, field)
    result: set[date] = set()
    for index, value in enumerate(values):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise RoundTripError(f"{field}[{index}] must be an ISO date") from exc
        if parsed in result:
            raise RoundTripError(f"{field} must not contain duplicate dates")
        result.add(parsed)
    return result


def _date_constraints(raw: Any) -> tuple[str, str, set[date] | None, set[date] | None]:
    if raw is None:
        return "fully_flexible", "fully_flexible", None, None
    if not isinstance(raw, dict):
        raise RoundTripError("intent.date_flexibility must be an object")
    shared_mode = raw.get("mode", "bounded_window")
    if shared_mode not in {"exact_dates", "bounded_window", "fully_flexible", "independent"}:
        raise RoundTripError("intent.date_flexibility.mode is unsupported")
    outbound_mode = raw.get("outbound_mode", shared_mode)
    return_mode = raw.get("return_mode", shared_mode)
    if outbound_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise RoundTripError("intent.date_flexibility.outbound_mode is unsupported")
    if return_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise RoundTripError("intent.date_flexibility.return_mode is unsupported")
    outbound = (
        _exact_departure_dates(
            raw.get("outbound_exact_departure_dates"),
            "intent.date_flexibility.outbound_exact_departure_dates",
        )
        if outbound_mode == "exact_dates"
        else None
    )
    returning = (
        _exact_departure_dates(
            raw.get("return_exact_departure_dates"),
            "intent.date_flexibility.return_exact_departure_dates",
        )
        if return_mode == "exact_dates"
        else None
    )
    return outbound_mode, return_mode, outbound, returning


def _node_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise RoundTripError("nodes must be a non-empty array")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        if isinstance(item, str):
            node = {"node_id": _text(item, f"nodes[{index}]"), "node_type": "airport"}
        elif isinstance(item, dict):
            node = dict(item)
            node["node_id"] = _text(item.get("node_id"), f"nodes[{index}].node_id")
            node["node_type"] = _text(item.get("node_type"), f"nodes[{index}].node_type")
        else:
            raise RoundTripError(f"nodes[{index}] must be a string or object")
        if node["node_id"] in result:
            raise RoundTripError(f"duplicate node_id {node['node_id']}")
        result[node["node_id"]] = node
    return result


def _money(raw: Any, field: str) -> tuple[float, str]:
    if not isinstance(raw, dict):
        raise RoundTripError(f"{field} must be an object")
    return _amount(raw.get("amount"), f"{field}.amount"), _text(raw.get("currency"), f"{field}.currency").upper()


def _fx_context(raw: Any, base_currency: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RoundTripError("fx_snapshot must be an object")
    if _text(raw.get("base_currency"), "fx_snapshot.base_currency").upper() != base_currency:
        raise RoundTripError("fx_snapshot.base_currency must equal base_currency")
    rates_raw = raw.get("rates")
    if not isinstance(rates_raw, dict) or not rates_raw:
        raise RoundTripError("fx_snapshot.rates must be a non-empty object")
    rates = {str(code).upper(): _amount(rate, f"fx_snapshot.rates.{code}") for code, rate in rates_raw.items()}
    if rates.get(base_currency, 1.0) != 1.0:
        raise RoundTripError("fx_snapshot base-currency rate must be 1")
    rates[base_currency] = 1.0
    url = _http_url(raw.get("url", raw.get("source_url")), "fx_snapshot.url")
    return {**raw, "rates": rates, "url": url}


def _convert(amount: float, currency: str, context: dict[str, Any], field: str) -> float:
    if currency == context["currency"]:
        return round(amount, 2)
    fx = context["fx_snapshot"]
    if fx is None or currency not in fx["rates"]:
        raise RoundTripError(f"{field} currency {currency} requires an explicit FX rate")
    return round(amount * fx["rates"][currency], 2)


def _buffers(raw: Any) -> dict[str, int]:
    result = dict(DEFAULT_BUFFERS)
    if raw is None:
        return result
    if not isinstance(raw, dict):
        raise RoundTripError("intent.mode_transition_buffers_minutes must be an object")
    unknown = sorted(set(raw) - set(result))
    if unknown:
        raise RoundTripError(f"unknown mode transition buffers {unknown}")
    for key, value in raw.items():
        result[key] = _integer(value, f"intent.mode_transition_buffers_minutes.{key}")
    return result


def _entitlements(raw: Any, currency: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RoundTripError("transport_entitlements must be an array")
    result = []
    for index, item in enumerate(raw):
        field = f"transport_entitlements[{index}]"
        if not isinstance(item, dict):
            raise RoundTripError(f"{field} must be an object")
        override, override_currency = _money(item.get("cost_override"), f"{field}.cost_override")
        if override_currency != currency:
            raise RoundTripError(f"{field}.cost_override must use base_currency")
        result.append(
            {
                "entitlement_id": _text(item.get("entitlement_id"), f"{field}.entitlement_id"),
                "covered_modes": set(_string_list(item.get("covered_modes"), f"{field}.covered_modes")),
                "coverage_tags": set(_string_list(item.get("coverage_tags"), f"{field}.coverage_tags")),
                "cost_override": override,
            }
        )
    return result


def _validate_bundle(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RoundTripError("bundle must be an object")
    if raw.get("schema_version") != VERSION:
        raise RoundTripError(f"schema_version must be {VERSION}")
    currency = _text(raw.get("base_currency"), "base_currency").upper()
    if len(currency) != 3:
        raise RoundTripError("base_currency must be a three-letter code")
    nodes = _node_catalog(raw.get("nodes"))
    intent = raw.get("intent")
    if not isinstance(intent, dict):
        raise RoundTripError("intent must be an object")
    origin_field = (
        "origin_endpoint_nodes"
        if "origin_endpoint_nodes" in intent
        else "origin_anchor_nodes" if "origin_anchor_nodes" in intent else "origin_nodes"
    )
    destination_field = (
        "destination_endpoint_nodes"
        if "destination_endpoint_nodes" in intent
        else "destination_anchor_nodes" if "destination_anchor_nodes" in intent else "destination_nodes"
    )
    origins = set(_string_list(intent.get(origin_field), f"intent.{origin_field}"))
    destinations = set(_string_list(intent.get(destination_field), f"intent.{destination_field}"))
    if not origins <= set(nodes) or not destinations <= set(nodes):
        raise RoundTripError("intent anchors must be declared nodes")
    if origins & destinations:
        raise RoundTripError("origin and destination anchors must be disjoint")
    traveler_count = _integer(intent.get("traveler_count"), "intent.traveler_count", minimum=1)
    baggage_policy = intent.get("baggage_policy")
    if not isinstance(baggage_policy, dict):
        raise RoundTripError("intent.baggage_policy must be an object")
    required_baggage_items = baggage_policy.get("required_items")
    if not isinstance(required_baggage_items, list) or not all(
        isinstance(item, str) and item.strip() for item in required_baggage_items
    ):
        raise RoundTripError("intent.baggage_policy.required_items must be an array of non-empty strings")
    required_baggage_items = [item.strip() for item in required_baggage_items]
    if len(required_baggage_items) != len(set(required_baggage_items)):
        raise RoundTripError("intent.baggage_policy.required_items contains duplicates")
    paid_baggage_allowed = baggage_policy.get("paid_baggage_allowed", False)
    if not isinstance(paid_baggage_allowed, bool):
        raise RoundTripError(
            "intent.baggage_policy.paid_baggage_allowed must be boolean"
        )
    accommodation_policy = intent.get("accommodation_cost_policy", "include_required")
    if accommodation_policy not in {"include_required", "exclude_from_optimization"}:
        raise RoundTripError("intent.accommodation_cost_policy is unsupported")
    outbound_window = _window(intent.get("outbound_departure_window"), "intent.outbound_departure_window")
    return_window = _window(intent.get("return_departure_window"), "intent.return_departure_window")
    outbound_date_flexibility_mode, return_date_flexibility_mode, outbound_exact_departure_dates, return_exact_departure_dates = _date_constraints(intent.get("date_flexibility"))
    allowed_modes = set(intent.get("allowed_modes", ALLOWED_MODES))
    if not allowed_modes or not allowed_modes <= ALLOWED_MODES:
        raise RoundTripError("intent.allowed_modes contains an unsupported mode")
    max_portfolio_offers = _integer(intent.get("max_portfolio_offers", 8), "intent.max_portfolio_offers", minimum=1)
    max_solver_offers = _integer(intent.get("max_solver_offers", 24), "intent.max_solver_offers", minimum=1)
    if max_portfolio_offers > max_solver_offers:
        raise RoundTripError("max_portfolio_offers must not exceed max_solver_offers")
    budget = None
    if intent.get("budget") is not None:
        budget_amount, budget_currency = _money(intent.get("budget"), "intent.budget")
        if budget_currency != currency:
            raise RoundTripError("intent.budget must use base_currency")
        budget = budget_amount
    self_transfer_policy = intent.get("self_transfer_policy")
    if self_transfer_policy not in {"allow", "avoid", "never"}:
        raise RoundTripError("intent.self_transfer_policy is unsupported")
    overnight_allowed = intent.get("overnight_allowed")
    if not isinstance(overnight_allowed, bool):
        raise RoundTripError("intent.overnight_allowed must be boolean")
    maximum_total_duration_minutes = intent.get("maximum_total_duration_minutes")
    if maximum_total_duration_minutes is not None:
        maximum_total_duration_minutes = _integer(maximum_total_duration_minutes, "intent.maximum_total_duration_minutes", minimum=1)
    minimum_stay_nights = intent.get("minimum_stay_nights")
    if minimum_stay_nights is not None:
        minimum_stay_nights = _integer(minimum_stay_nights, "intent.minimum_stay_nights")
    maximum_stay_nights = intent.get("maximum_stay_nights")
    if maximum_stay_nights is not None:
        maximum_stay_nights = _integer(maximum_stay_nights, "intent.maximum_stay_nights")
    if minimum_stay_nights is not None and maximum_stay_nights is not None and minimum_stay_nights > maximum_stay_nights:
        raise RoundTripError("intent.minimum_stay_nights must not exceed intent.maximum_stay_nights")
    airport_transit_unknown_policy = intent.get("airport_transit_unknown_policy", "allow_with_warning")
    if airport_transit_unknown_policy not in {"allow_with_warning", "reject"}:
        raise RoundTripError("intent.airport_transit_unknown_policy is unsupported")
    context: dict[str, Any] = {
        "currency": currency,
        "nodes": nodes,
        "origins": origins,
        "destinations": destinations,
        "endpoint_resolution": {
            "origin_field": origin_field,
            "destination_field": destination_field,
            "origin_nodes": sorted(origins),
            "destination_nodes": sorted(destinations),
        },
        "outbound_window": outbound_window,
        "return_window": return_window,
        "outbound_date_flexibility_mode": outbound_date_flexibility_mode,
        "return_date_flexibility_mode": return_date_flexibility_mode,
        "outbound_exact_departure_dates": outbound_exact_departure_dates,
        "return_exact_departure_dates": return_exact_departure_dates,
        "allowed_modes": allowed_modes,
        "buffers": _buffers(intent.get("mode_transition_buffers_minutes")),
        "max_portfolio_offers": max_portfolio_offers,
        "max_solver_offers": max_solver_offers,
        "fx_snapshot": _fx_context(raw.get("fx_snapshot"), currency),
        "entitlements": _entitlements(raw.get("transport_entitlements"), currency),
        "evaluation_at": _dt(raw.get("evaluation_at"), "evaluation_at") if raw.get("evaluation_at") is not None else None,
        "expired_offer_ids": [],
        "accommodation_cost_policy": accommodation_policy,
        "budget": budget,
        "self_transfer_policy": self_transfer_policy,
        "overnight_allowed": overnight_allowed,
        "maximum_total_duration_minutes": maximum_total_duration_minutes,
        "minimum_stay_nights": minimum_stay_nights,
        "maximum_stay_nights": maximum_stay_nights,
        "airport_transit_unknown_policy": airport_transit_unknown_policy,
        "traveler_count": traveler_count,
        "baggage_policy": {
            "required_items": required_baggage_items,
            "paid_baggage_allowed": paid_baggage_allowed,
        },
    }

    movements_raw = raw.get("movements")
    if not isinstance(movements_raw, list):
        raise RoundTripError("movements must be an array")
    movements: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(movements_raw):
        field = f"movements[{index}]"
        if not isinstance(item, dict):
            raise RoundTripError(f"{field} must be an object")
        movement_id = _text(item.get("movement_id"), f"{field}.movement_id")
        if movement_id in movements:
            raise RoundTripError(f"duplicate movement_id {movement_id}")
        from_node = _text(item.get("from_node"), f"{field}.from_node")
        to_node = _text(item.get("to_node"), f"{field}.to_node")
        if from_node not in nodes or to_node not in nodes:
            raise RoundTripError(f"{field} references an undeclared node")
        departure = _dt(item.get("departure"), f"{field}.departure")
        arrival = _dt(item.get("arrival"), f"{field}.arrival")
        if arrival <= departure:
            raise RoundTripError(f"{field}.arrival must follow departure")
        mode = _text(item.get("mode"), f"{field}.mode")
        if mode not in allowed_modes:
            raise RoundTripError(f"{field}.mode is not allowed")
        movements[movement_id] = {
            **item,
            "movement_id": movement_id,
            "from_node": from_node,
            "to_node": to_node,
            "mode": mode,
            "operator": _text(item.get("operator"), f"{field}.operator"),
            "service_id": _text(item.get("service_id"), f"{field}.service_id"),
            "_departure": departure,
            "_arrival": arrival,
        }
        if mode == "flight":
            identity = item.get("flight_identity")
            if not isinstance(identity, dict):
                raise RoundTripError(f"{field}.flight_identity is required for flight")
            carrier = identity.get("marketing_carrier") or identity.get("operating_carrier")
            if not isinstance(carrier, str) or not carrier.strip():
                raise RoundTripError(f"{field}.flight_identity requires a carrier")
            numbers = identity.get("flight_numbers")
            unknown_reason = identity.get("unknown_reason")
            if not (isinstance(numbers, list) and numbers and all(isinstance(number, str) and number.strip() for number in numbers)):
                if not (isinstance(unknown_reason, dict) and isinstance(unknown_reason.get("code"), str) and unknown_reason["code"].strip()):
                    raise RoundTripError(f"{field}.flight_identity requires flight_numbers or structured unknown_reason")
        transfer = item.get("transfer_from_previous")
        if transfer is not None:
            if not isinstance(transfer, dict) or transfer.get("status") not in {"verified", "unknown", "infeasible"}:
                raise RoundTripError(f"{field}.transfer_from_previous is invalid")
            connection_protection = transfer.get("connection_protection")
            if connection_protection is not None and connection_protection not in {"carrier_protected", "self_transfer", "unknown"}:
                raise RoundTripError(f"{field}.transfer_from_previous.connection_protection is invalid")
            if connection_protection == "carrier_protected" and transfer.get("status") != "verified":
                raise RoundTripError(f"{field} carrier-protected connection must be verified")
            if transfer.get("minimum_connection_minutes") is not None:
                _integer(transfer["minimum_connection_minutes"], f"{field}.transfer_from_previous.minimum_connection_minutes")
    context["movements"] = movements

    offers_raw = raw.get("offers")
    if not isinstance(offers_raw, list):
        raise RoundTripError("offers must be an array")
    if len(offers_raw) > max_solver_offers:
        raise RoundTripError(f"offer pool exceeds deterministic max_solver_offers={max_solver_offers}; narrow it with AI-led hypotheses")
    offers: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(offers_raw):
        field = f"offers[{index}]"
        if not isinstance(item, dict):
            raise RoundTripError(f"{field} must be an object")
        offer_id = _text(item.get("offer_id"), f"{field}.offer_id")
        if offer_id in offers:
            raise RoundTripError(f"duplicate offer_id {offer_id}")
        if item.get("pricing_scope") != "atomic_offer" or item.get("atomic") is not True:
            raise RoundTripError(f"{field} must be an atomic_offer")
        scope = item.get("journey_scope")
        if scope not in OFFER_SCOPES:
            raise RoundTripError(f"{field}.journey_scope is unsupported")
        direction = item.get("direction")
        if scope == "one_way" and direction not in {"outbound", "return"}:
            raise RoundTripError(f"{field}.direction is required for one_way")
        if scope != "one_way" and direction is not None:
            raise RoundTripError(f"{field}.direction is allowed only for one_way")
        movement_ids = _string_list(item.get("movement_ids"), f"{field}.movement_ids")
        unknown = sorted(set(movement_ids) - set(movements))
        if unknown:
            raise RoundTripError(f"{field} references unknown movements {unknown}")
        used_movement_ids = list(movement_ids)
        unused_movement_ids: list[str] = []
        ticket_context = item.get("ticket_context")
        if ticket_context is not None:
            if not isinstance(ticket_context, dict):
                raise RoundTripError(f"{field}.ticket_context must be an object")
            use_pattern = ticket_context.get("use_pattern", "full_ticket")
            if use_pattern == "planned_exit_at_connection":
                if scope == "surface":
                    raise RoundTripError(f"{field} planned exit is supported only for ticketed travel")
                used_movement_ids = _string_list(ticket_context.get("used_movement_ids"), f"{field}.ticket_context.used_movement_ids")
                unused_movement_ids = _string_list(ticket_context.get("unused_movement_ids"), f"{field}.ticket_context.unused_movement_ids")
                if set(used_movement_ids) & set(unused_movement_ids) or set(used_movement_ids) | set(unused_movement_ids) != set(movement_ids):
                    raise RoundTripError(f"{field} planned-exit used/unused movements must partition the complete ticket")
                ordered_ticket = sorted((movements[movement_id] for movement_id in movement_ids), key=lambda movement: (movement["_departure"], movement["_arrival"], movement["movement_id"]))
                if any(following["from_node"] != previous["to_node"] or following["_departure"] < previous["_arrival"] for previous, following in zip(ordered_ticket, ordered_ticket[1:])):
                    raise RoundTripError(f"{field} planned-exit ticket movements must form one chronological ticketed path")
                ordered_used = [movement for movement in ordered_ticket if movement["movement_id"] in set(used_movement_ids)]
                if not ordered_used or [movement["movement_id"] for movement in ordered_ticket[: len(ordered_used)]] != [movement["movement_id"] for movement in ordered_used]:
                    raise RoundTripError(f"{field} planned-exit used movements must be a prefix of the ticketed path")
                planned_exit_node = _text(ticket_context.get("planned_exit_node"), f"{field}.ticket_context.planned_exit_node")
                ticketed_final_node = _text(ticket_context.get("ticketed_final_node"), f"{field}.ticket_context.ticketed_final_node")
                if ordered_used[-1]["to_node"] != planned_exit_node or ordered_ticket[-1]["to_node"] != ticketed_final_node:
                    raise RoundTripError(f"{field} planned-exit and ticketed-final nodes do not match movement geometry")
            elif use_pattern != "full_ticket":
                raise RoundTripError(f"{field}.ticket_context.use_pattern is unsupported")
        movement_modes = {movements[movement_id]["mode"] for movement_id in movement_ids}
        if scope == "surface" and not movement_modes <= SURFACE_MODES:
            raise RoundTripError(f"{field}.surface offer contains a flight")
        included_baggage = item.get("included_baggage", [])
        if not isinstance(included_baggage, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in included_baggage
        ):
            raise RoundTripError(f"{field}.included_baggage must be an array of non-empty strings")
        price, price_currency = _money(item.get("price"), f"{field}.price")
        baggage, baggage_currency = _money(item.get("mandatory_baggage_cost"), f"{field}.mandatory_baggage_cost")
        missing_baggage_items = sorted(
            set(required_baggage_items) - set(included_baggage)
        )
        if (
            "flight" in movement_modes
            and missing_baggage_items
            and (not paid_baggage_allowed or baggage <= 0)
        ):
            continue
        transport_and_baggage_cost = _convert(price, price_currency, context, f"{field}.price") + _convert(baggage, baggage_currency, context, f"{field}.mandatory_baggage_cost")
        accommodation_cost = 0.0
        if item.get("required_accommodation_cost") is not None:
            accommodation_amount, accommodation_currency = _money(item.get("required_accommodation_cost"), f"{field}.required_accommodation_cost")
            accommodation_cost = _convert(accommodation_amount, accommodation_currency, context, f"{field}.required_accommodation_cost")
        observed_cost = round(transport_and_baggage_cost + accommodation_cost, 2)
        accommodation_included = accommodation_cost if context["accommodation_cost_policy"] == "include_required" else 0.0
        effective_cost = round(transport_and_baggage_cost + accommodation_included, 2)
        applied_entitlement = None
        tags = set(item.get("entitlement_tags", []))
        for entitlement in context["entitlements"]:
            if scope == "surface" and movement_modes <= entitlement["covered_modes"] and tags & entitlement["coverage_tags"]:
                effective_cost = round(entitlement["cost_override"] + accommodation_included, 2)
                applied_entitlement = entitlement["entitlement_id"]
                break
        url = _http_url(item.get("url"), f"{field}.url")
        if item.get("evidence_status") != "verified_observed":
            raise RoundTripError(f"{field}.evidence_status must be verified_observed")
        observed_at = _dt(item.get("observed_at"), f"{field}.observed_at")
        first_departure = min(movements[movement_id]["_departure"] for movement_id in movement_ids)
        if observed_at > first_departure:
            raise RoundTripError(f"{field}.observed_at must not follow first departure")
        expires_at = None
        if item.get("expires_at") is not None:
            expires_at = _dt(item.get("expires_at"), f"{field}.expires_at")
            if expires_at <= observed_at:
                raise RoundTripError(f"{field}.expires_at must follow observed_at")
            if context["evaluation_at"] is not None and expires_at <= context["evaluation_at"]:
                context["expired_offer_ids"].append(offer_id)
                continue
        offers[offer_id] = {
            **item,
            "offer_id": offer_id,
            "journey_scope": scope,
            "direction": direction,
            "movement_ids": movement_ids,
            "_used_movement_ids": used_movement_ids,
            "_unused_movement_ids": unused_movement_ids,
            "provider": _text(item.get("provider"), f"{field}.provider"),
            "seller": _text(item.get("seller"), f"{field}.seller"),
            "provider_offer_id": _text(item.get("provider_offer_id"), f"{field}.provider_offer_id"),
            "source_id": _text(item.get("source_id"), f"{field}.source_id"),
            "_effective_cost": effective_cost,
            "_observed_cost": round(observed_cost, 2),
            "_required_accommodation_cost": round(accommodation_cost, 2),
            "_included_accommodation_cost": round(accommodation_included, 2),
            "_applied_entitlement": applied_entitlement,
            "_paid_baggage_items": missing_baggage_items,
        }
        if scope == "surface":
            constraints = item.get("pool_constraints")
            if not isinstance(constraints, dict):
                raise RoundTripError(f"{field}.pool_constraints is required for surface")
            max_time = _integer(constraints.get("max_time_minutes"), f"{field}.pool_constraints.max_time_minutes")
            max_cost = None
            if constraints.get("max_cost") is not None:
                max_cost, max_cost_currency = _money(constraints.get("max_cost"), f"{field}.pool_constraints.max_cost")
                if max_cost_currency != currency:
                    raise RoundTripError(f"{field}.pool_constraints.max_cost must use base_currency")
            allowed_surface_modes = set(_string_list(constraints.get("allowed_modes"), f"{field}.pool_constraints.allowed_modes"))
            if not movement_modes <= allowed_surface_modes:
                raise RoundTripError(f"{field} uses a surface mode outside its regional pool")
            first = min(movements[movement_id]["_departure"] for movement_id in movement_ids)
            last = max(movements[movement_id]["_arrival"] for movement_id in movement_ids)
            if int((last - first).total_seconds() // 60) > max_time:
                raise RoundTripError(f"{field} exceeds regional pool time limit")
            if max_cost is not None and effective_cost > max_cost:
                raise RoundTripError(f"{field} exceeds regional pool effective-cost limit")
    context["offers"] = offers
    for offer in offers.values():
        if offer["journey_scope"] != "one_way":
            continue
        starts = set(_string_list(offer.get("coverage_start_nodes"), f"offers.{offer['offer_id']}.coverage_start_nodes"))
        goals = set(_string_list(offer.get("coverage_end_nodes"), f"offers.{offer['offer_id']}.coverage_end_nodes"))
        if not starts <= set(nodes) or not goals <= set(nodes):
            raise RoundTripError(f"one-way offer {offer['offer_id']} coverage endpoints must be declared nodes")
        window = outbound_window if offer["direction"] == "outbound" else return_window
        movement_offer = {movement_id: offer["offer_id"] for movement_id in offer["_used_movement_ids"]}
        complete = any(
            set(path) == set(offer["_used_movement_ids"])
            for path in _paths(set(offer["_used_movement_ids"]), movements, movement_offer, starts, goals, window, context)
        )
        if not complete:
            raise RoundTripError(f"one-way offer {offer['offer_id']} does not independently complete its declared direction")
    return context


def _buffer_minutes(previous: dict[str, Any], following: dict[str, Any], previous_offer: str, following_offer: str, context: dict[str, Any]) -> int:
    previous_mode = previous["mode"]
    following_mode = following["mode"]
    if previous_mode == "flight" and following_mode == "flight":
        key = "self_transfer_to_flight" if previous_offer != following_offer else "flight_to_flight"
    elif previous_mode == "flight":
        key = "flight_to_surface"
    elif following_mode == "flight":
        key = "surface_to_flight"
    else:
        key = "surface_to_surface"
    return context["buffers"][key]


def _paths(
    movement_ids: set[str],
    movements: dict[str, dict[str, Any]],
    movement_offer: dict[str, str],
    starts: set[str],
    goals: set[str],
    first_window: tuple[datetime, datetime],
    context: dict[str, Any],
    not_before: datetime | None = None,
) -> Iterable[list[str]]:
    ordered = sorted((movements[mid] for mid in movement_ids), key=lambda item: (item["_departure"], item["_arrival"], item["movement_id"]))

    def walk(node: str, current_time: datetime | None, remaining: set[str], path: list[str]) -> Iterable[list[str]]:
        if path and node in goals:
            yield list(path)
            return
        for movement in ordered:
            movement_id = movement["movement_id"]
            if movement_id not in remaining or movement["from_node"] != node:
                continue
            departure = movement["_departure"]
            if current_time is None:
                if not (first_window[0] <= departure <= first_window[1]) or (not_before is not None and departure < not_before):
                    continue
            else:
                previous = movements[path[-1]]
                buffer = _buffer_minutes(previous, movement, movement_offer[path[-1]], movement_offer[movement_id], context)
                if not context["overnight_allowed"] and departure.date() > current_time.date():
                    continue
                if previous["mode"] == "flight" and movement["mode"] == "flight":
                    same_atomic_offer = movement_offer[path[-1]] == movement_offer[movement_id]
                    if not same_atomic_offer and context["self_transfer_policy"] == "never":
                        continue
                    transfer = movement.get("transfer_from_previous")
                    transfer_status = transfer.get("status") if isinstance(transfer, dict) else "unknown"
                    if transfer_status == "infeasible" or (transfer_status == "unknown" and context["airport_transit_unknown_policy"] == "reject"):
                        continue
                    carrier_protected = (
                        same_atomic_offer
                        and transfer_status == "verified"
                        and transfer.get("connection_protection") == "carrier_protected"
                    )
                    if carrier_protected:
                        buffer = int(transfer.get("minimum_connection_minutes", 0))
                    elif transfer_status == "verified" and transfer.get("minimum_connection_minutes") is not None:
                        buffer = max(buffer, int(transfer["minimum_connection_minutes"]))
                if departure < current_time + timedelta(minutes=buffer):
                    continue
            yield from walk(movement["to_node"], movement["_arrival"], remaining - {movement_id}, [*path, movement_id])

    for start in sorted(starts):
        yield from walk(start, None, set(movement_ids), [])


def _complete_itinerary(selected: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any] | None:
    movement_offer: dict[str, str] = {}
    for offer in selected:
        for movement_id in offer["_used_movement_ids"]:
            if movement_id in movement_offer:
                return None
            movement_offer[movement_id] = offer["offer_id"]
    movement_ids = set(movement_offer)
    movements = context["movements"]
    for outbound in _paths(movement_ids, movements, movement_offer, context["origins"], context["destinations"], context["outbound_window"], context):
        first_departure = movements[outbound[0]]["_departure"]
        if context["outbound_date_flexibility_mode"] == "exact_dates" and first_departure.date() not in context["outbound_exact_departure_dates"]:
            continue
        outbound_set = set(outbound)
        remaining = movement_ids - outbound_set
        outbound_arrival = movements[outbound[-1]]["_arrival"]
        for return_path in _paths(remaining, movements, movement_offer, context["destinations"], context["origins"], context["return_window"], context, not_before=outbound_arrival):
            if outbound_set | set(return_path) != movement_ids:
                continue
            return_departure = movements[return_path[0]]["_departure"]
            if context["return_date_flexibility_mode"] == "exact_dates" and return_departure.date() not in context["return_exact_departure_dates"]:
                continue
            stay_nights = (return_departure.date() - outbound_arrival.date()).days
            if stay_nights < 0:
                continue
            if context["minimum_stay_nights"] is not None and stay_nights < context["minimum_stay_nights"]:
                continue
            if context["maximum_stay_nights"] is not None and stay_nights > context["maximum_stay_nights"]:
                continue
            stay_duration_minutes = int((return_departure - outbound_arrival).total_seconds() // 60)
            final_arrival = movements[return_path[-1]]["_arrival"]
            total_elapsed_minutes = int((final_arrival - first_departure).total_seconds() // 60)
            if context["maximum_total_duration_minutes"] is not None and total_elapsed_minutes > context["maximum_total_duration_minutes"]:
                continue
            self_transfer_count = 0
            unknown_transit_connections = []
            overnight_connection_count = 0
            for directional_path in (outbound, return_path):
                for previous_id, following_id in zip(directional_path, directional_path[1:]):
                    previous = movements[previous_id]
                    following = movements[following_id]
                    if following["_departure"].date() > previous["_arrival"].date():
                        overnight_connection_count += 1
                    if previous["mode"] == "flight" and following["mode"] == "flight":
                        if movement_offer[previous_id] != movement_offer[following_id]:
                            self_transfer_count += 1
                        transfer = following.get("transfer_from_previous")
                        if not isinstance(transfer, dict) or transfer.get("status") == "unknown":
                            unknown_transit_connections.append({"at_node": previous["to_node"], "previous_movement_id": previous_id, "following_movement_id": following_id})
            return {
                "outbound_movement_ids": outbound,
                "return_movement_ids": return_path,
                "departure": first_departure.isoformat(),
                "outbound_arrival": outbound_arrival.isoformat(),
                "return_departure": return_departure.isoformat(),
                "arrival": final_arrival.isoformat(),
                "total_elapsed_minutes": total_elapsed_minutes,
                "destination_stay": {
                    "arrival_at_destination": outbound_arrival.isoformat(),
                    "return_departure_from_destination": return_departure.isoformat(),
                    "arrival_local_date": outbound_arrival.date().isoformat(),
                    "return_departure_local_date": return_departure.date().isoformat(),
                    "actual_duration_minutes": stay_duration_minutes,
                    "nights": stay_nights,
                    "night_count_semantics": STAY_NIGHT_COUNT_SEMANTICS,
                    "minimum_stay_nights": context["minimum_stay_nights"],
                    "maximum_stay_nights": context["maximum_stay_nights"],
                    "constraints_satisfied": True,
                },
                "self_transfer_count": self_transfer_count,
                "self_transfer_policy": context["self_transfer_policy"],
                "overnight_connection_count": overnight_connection_count,
                "airport_transit_unknown_connections": unknown_transit_connections,
            }
    return None


def _portfolio_kind(selected: list[dict[str, Any]]) -> str:
    ticket_offers = [offer for offer in selected if offer["journey_scope"] != "surface"]
    scopes = [offer["journey_scope"] for offer in ticket_offers]
    if len(ticket_offers) == 1 and scopes == ["joint_round_trip"]:
        return "joint_round_trip"
    if len(ticket_offers) == 2 and set(scopes) == {"one_way"} and {offer["direction"] for offer in ticket_offers} == {"outbound", "return"}:
        return "split_one_ways"
    if len(ticket_offers) >= 2 and all(scope in {"joint_round_trip", "atomic_itinerary"} for scope in scopes):
        return "nested_round_trips"
    return "mixed_atomic_offers"


def _portfolio(selected: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any] | None:
    itinerary = _complete_itinerary(selected, context)
    if itinerary is None:
        return None
    kind = _portfolio_kind(selected)
    offer_ids = sorted(offer["offer_id"] for offer in selected)
    token = hashlib.sha256("|".join([kind, *offer_ids]).encode("utf-8")).hexdigest()[:16]
    total = round(sum(offer["_effective_cost"] for offer in selected), 2)
    if context["budget"] is not None and total > context["budget"]:
        return None
    observed_total = round(sum(offer["_observed_cost"] for offer in selected), 2)
    accommodation_observed = round(sum(offer["_required_accommodation_cost"] for offer in selected), 2)
    accommodation_included = round(sum(offer["_included_accommodation_cost"] for offer in selected), 2)
    task_ids = sorted({offer.get("research_task_id") for offer in selected if offer.get("research_task_id")})
    entitlements = sorted({offer["_applied_entitlement"] for offer in selected if offer["_applied_entitlement"]})
    planned_exit_offers = [
        {
            "offer_id": offer["offer_id"],
            "used_movement_ids": list(offer["_used_movement_ids"]),
            "unused_movement_ids": list(offer["_unused_movement_ids"]),
            "ticket_context": copy_context,
        }
        for offer in selected
        if offer["_unused_movement_ids"]
        for copy_context in [offer.get("ticket_context")]
    ]
    return {
        "portfolio_id": f"portfolio-{token}",
        "portfolio_kind": kind,
        "offer_ids": offer_ids,
        "itinerary_ids": sorted({offer.get("itinerary_id") for offer in selected if offer.get("itinerary_id")}),
        "offer_snapshot_ids": sorted({offer.get("offer_snapshot_id") for offer in selected if offer.get("offer_snapshot_id")}),
        "evidence_task_ids": task_ids,
        "total_mandatory_cost": {"amount": total, "currency": context["currency"]},
        "observed_mandatory_cost_before_entitlements": {"amount": observed_total, "currency": context["currency"]},
        "required_accommodation_cost_observed": {"amount": accommodation_observed, "currency": context["currency"]},
        "required_accommodation_cost_included": {"amount": accommodation_included, "currency": context["currency"]},
        "required_accommodation_cost_excluded": {"amount": round(accommodation_observed - accommodation_included, 2), "currency": context["currency"]},
        "paid_baggage_items_by_offer": {
            offer["offer_id"]: list(offer["_paid_baggage_items"])
            for offer in selected
            if offer["_paid_baggage_items"]
        },
        "applied_entitlements": entitlements,
        "planned_exit_offers": planned_exit_offers,
        "evidence_variants": [{"offer_ids": offer_ids, "offer_snapshot_ids": sorted({offer.get("offer_snapshot_id") for offer in selected if offer.get("offer_snapshot_id")})}],
        **itinerary,
    }


def solve_bundle(raw: Any) -> dict[str, Any]:
    context = _validate_bundle(raw)
    offers = sorted(context["offers"].values(), key=lambda item: (item["_effective_cost"], item["offer_id"]))
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    max_size = min(context["max_portfolio_offers"], len(offers))
    for size in range(1, max_size + 1):
        for selected_tuple in itertools.combinations(offers, size):
            selected = list(selected_tuple)
            candidate = _portfolio(selected, context)
            if candidate is None:
                continue
            key = (
                tuple(candidate["outbound_movement_ids"]),
                tuple(candidate["return_movement_ids"]),
                candidate["total_mandatory_cost"]["amount"],
                candidate["portfolio_kind"],
            )
            if key in seen:
                existing = next(item for item in candidates if (
                    tuple(item["outbound_movement_ids"]),
                    tuple(item["return_movement_ids"]),
                    item["total_mandatory_cost"]["amount"],
                    item["portfolio_kind"],
                ) == key)
                variant = {"offer_ids": sorted(offer["offer_id"] for offer in selected), "offer_snapshot_ids": sorted({offer.get("offer_snapshot_id") for offer in selected if offer.get("offer_snapshot_id")})}
                if variant not in existing["evidence_variants"]:
                    existing["evidence_variants"].append(variant)
                continue
            seen.add(key)
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item["total_mandatory_cost"]["amount"], item["total_elapsed_minutes"], item["portfolio_id"]))
    return {
        "solver_version": VERSION,
        "search_scope": "verified_atomic_offers_in_supplied_bundle",
        "enumeration_complete_within_supplied_offer_pool": True,
        "base_currency": context["currency"],
        "endpoint_resolution": context["endpoint_resolution"],
        "stay_constraints": {
            "minimum_stay_nights": context["minimum_stay_nights"],
            "maximum_stay_nights": context["maximum_stay_nights"],
            "night_count_semantics": STAY_NIGHT_COUNT_SEMANTICS,
        },
        "offers_considered": len(offers),
        "expired_offer_ids": sorted(context["expired_offer_ids"]),
        "portfolios": candidates,
        "best_portfolio": candidates[0] if candidates else None,
        "portfolio_kind_counts": {kind: sum(item["portfolio_kind"] == kind for item in candidates) for kind in PORTFOLIO_KINDS},
    }


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = solve_bundle(read_json(args.input))
        write_json(args.output, result)
        print(json.dumps({"valid": True, "portfolios": len(result["portfolios"]), "output": args.output}))
        return 0
    except (OSError, json.JSONDecodeError, RoundTripError) as exc:
        print(f"round_trip_portfolio_solver: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
