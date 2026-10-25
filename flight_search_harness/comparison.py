"""Pure comparison of saved discoveries against the current travel request.

Search strategy and observation admission belong to the caller. This module never
creates queries, changes saved facts, or treats a missing amount as zero.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def candidate_id(offer_refs: list[dict[str, str]]) -> str:
    refs = sorted(
        [{"observation_id": str(r["observation_id"]), "offer_id": str(r["offer_id"])} for r in offer_refs],
        key=lambda r: (r["observation_id"], r["offer_id"]),
    )
    encoded = json.dumps(refs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "candidate_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _variant_key(observation: dict, offer: dict, *, include_seller: bool = True) -> str:
    by_id = {m.get("movement_id"): m for m in observation.get("movements", [])}
    movements = []
    for identifier in offer.get("movement_ids", []):
        movement = by_id.get(identifier, {})
        movements.append({k: movement.get(k) for k in ("from_node", "to_node", "departure", "arrival", "mode", "operator", "service_id", "flight_identity")})
    movements.sort(key=lambda m: (m.get("departure") or "", json.dumps(m, sort_keys=True)))
    fare = {key: offer.get(key) for key in (
        "cabin", "fare_brand", "fare_basis", "fare_type", "price_type", "membership_required",
        "ticketing_structure", "price_scope", "traveler_count", "included_baggage", "checked_bag_count",
        "price_includes_mandatory_baggage", "restrictions", "ticket_context", "refundability", "change_policy")}
    identity = {"movements": movements, "fare": fare}
    if include_seller:
        identity["seller"] = offer.get("seller", observation.get("source", {}).get("seller"))
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _capture_time(observation: dict) -> datetime | None:
    """Only trustworthy observation times can order source snapshots."""
    if observation.get("capture_time_status") in {"unknown", "unreliable"}:
        return None
    return _time(observation.get("captured_at"))


def comparison_key(entry: dict) -> tuple:
    """Return the same objective/price ordering used by the public ranking.

    The final candidate ID is only a stable tie-breaker, not an improvement.
    """
    money = entry.get("ranking_cost") or {}
    price = _number(money.get("unrounded_amount", money.get("amount")))
    objective = entry.get("objective", "price")
    metric = price
    if objective == "stay_duration":
        metric = _number(entry.get("stay_minutes"))
    elif objective == "travel_duration":
        metric = _number(entry.get("travel_duration_minutes"))
    elif objective == "distance_per_currency":
        value = _number(entry.get("distance_per_currency"))
        metric = -value if value is not None else None
    return (metric is None, metric if metric is not None else Decimal(0), price is None,
            price if price is not None else Decimal(0), entry.get("candidate_id", ""))


def discovered_gateways(state: dict) -> list[dict]:
    """Project source-backed airport discoveries and their separate AI judgments.

    This reads ordinary observations. It creates no tasks, rewrites no Request,
    and treats country metadata as descriptive rather than a search boundary.
    """
    request = state["requests"][-1]["request"]
    intent = request["intent"]
    queries = {q["query_id"]: q for q in state.get("queries", [])}
    latest = {}
    for observation in state.get("observations", []):
        raw = observation.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        discovery = raw.get("gateway_discovery")
        provider = observation.get("source", {}).get("provider")
        if (not isinstance(discovery, dict) or not isinstance(provider, str) or not provider.strip()
                or raw.get("evidence_status") == "ai_proposed"
                or observation.get("admission", {}).get("status") not in {"signal", "admitted"}):
            continue
        query = queries.get(observation.get("query_id"), {})
        if query.get("status") in {"failed", "unsupported"}:
            continue
        side = discovery.get("side")
        if not isinstance(side, str) or side not in {"origin", "destination"} or not isinstance(discovery.get("airports"), list):
            continue
        region = intent.get(side, {})
        seeds = {_node(n) for n in region.get("seed_gateway_nodes", [])}
        flexible = (
            request.get("journey_shape") != "random_loop"
            and not intent.get(side + "_airports")
            and region.get("policy") != "exact"
            and any(n.get("node_type") != "airport" for n in region.get("anchor_nodes", []) if isinstance(n, dict))
        )
        for item in discovery["airports"]:
            if not isinstance(item, dict) or not isinstance(item.get("node"), dict):
                continue
            node = item["node"]
            identifier = node.get("node_id")
            decision, reason = item.get("decision"), item.get("reason")
            access = item.get("access", {"status": "unknown"})
            if (not isinstance(identifier, str) or not identifier.strip() or node.get("node_type") != "airport"
                    or not isinstance(decision, str) or decision not in {"investigate", "defer", "exclude"}
                    or not isinstance(reason, str) or not reason.strip()
                    or not isinstance(access, dict)
                    or not isinstance(access.get("status"), str) or access["status"] not in {"unknown", "plausible", "verified", "infeasible"}):
                continue
            latest[(side, identifier)] = {
                "side": side, "node": copy.deepcopy(node), "decision": decision, "reason": reason,
                "access": copy.deepcopy(access), "judgment_owner": "ai",
                "observation_id": observation["observation_id"], "query_id": observation.get("query_id"),
                "source": copy.deepcopy(observation.get("source")),
                "outside_seed_pool": identifier not in seeds,
                "eligible_for_expansion": flexible and decision != "exclude" and access["status"] != "infeasible",
            }
    return list(latest.values())


def _node(value: Any) -> str:
    return str(value.get("node_id", "")) if isinstance(value, dict) else str(value or "")


def _money(amount: Decimal | None, currency: str) -> dict[str, Any] | None:
    if amount is None:
        return None
    digits = 0 if currency in {"JPY", "KRW", "VND", "CLP", "ISK", "XAF", "XOF", "XPF"} else 3 if currency in {"BHD", "JOD", "KWD", "OMR", "TND"} else 2
    displayed = amount.quantize(Decimal(1).scaleb(-digits))
    return {"amount": format(displayed, "f"), "currency": currency, "unrounded_amount": str(amount)}


def _convert(money: Any, currency: str, intent: dict[str, Any]) -> Decimal | None:
    if not isinstance(money, dict):
        return None
    amount = _number(money.get("amount"))
    source = money.get("currency")
    if amount is None or amount < 0 or not source:
        return None
    if source == currency:
        return amount
    # Only explicit, attributed conversions are usable. No live lookup or guessed FX.
    rates = [r for r in intent.get("fx_rates", []) if _time(r.get("observed_at")) and any(
        isinstance(r.get(field), str) and r[field].strip() for field in ("source_name", "source_url"))]
    for rate in sorted(rates, key=lambda r: _time(r["observed_at"]).timestamp(), reverse=True):
        value = _number(rate.get("rate"))
        if value is None or value <= 0:
            continue
        if (rate.get("from_currency"), rate.get("to_currency")) == (source, currency):
            return amount * value
        if (rate.get("to_currency"), rate.get("from_currency")) == (source, currency):
            return amount / value
    return None


def _window(value: str | None, window: Any) -> bool | None:
    if not window:
        return True
    if not value or not isinstance(window, dict):
        return None
    start, end = window.get("start"), window.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    if len(start) == 10 and len(end) == 10:
        return start <= value[:10] <= end
    actual, lower, upper = _time(value), _time(start), _time(end)
    return lower <= actual <= upper if actual and lower and upper else None


def _status(value: bool | None) -> str:
    return "unknown" if value is None else "satisfied" if value else "violated"


def _local_value(value: str | None, region: dict, node: dict | None = None) -> str | None:
    anchors = region.get("anchor_nodes", [])
    zone = anchors[0].get("timezone") if anchors else None
    if not zone:
        return value
    instant = _time(value)
    try:
        return instant.astimezone(ZoneInfo(zone)).isoformat() if instant else None
    except (ZoneInfoNotFoundError, ValueError):
        # Windows may lack an IANA database. An explicitly observed local offset
        # at a node in the same declared zone already supplies the needed date.
        if instant and node and node.get("timezone") == zone:
            return value
        return None


def _airports(intent: dict[str, Any], side: str) -> set[str]:
    explicit = intent.get(side + "_airports")
    if explicit:
        return {_node(n) for n in explicit}
    region = intent.get(side, {})
    return {_node(n) for n in region.get("seed_gateway_nodes", [])}


def _endpoint_matches(node: str, intent: dict[str, Any], side: str, nodes: dict[str, dict]) -> bool | None:
    pool = _airports(intent, side)
    region = intent.get(side, {})
    if node in pool:
        return True
    if intent.get(side + "_airports") or region.get("policy") == "exact":
        return False if pool else None
    # A seed list is not the complete accepted region. Country alone cannot
    # establish that a newly discovered airport serves a particular city.
    return None


def _distance(movement: dict, nodes: dict[str, dict]) -> tuple[Decimal | None, str]:
    distance = _number(movement.get("distance_km"))
    if distance is not None and distance > 0:
        return distance, "observed_distance_km"
    endpoints = []
    for side in ("from_node", "to_node"):
        node = nodes.get(_node(movement.get(side)), {})
        coordinates = node.get("coordinates", node)
        lat = _number(coordinates.get("latitude", coordinates.get("lat")))
        lon = _number(coordinates.get("longitude", coordinates.get("lon")))
        if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return None, "unknown"
        endpoints.append((math.radians(float(lat)), math.radians(float(lon))))
    (a, b), (c, d) = endpoints
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return Decimal(str(6371.0088 * 2 * math.asin(math.sqrt(min(1, h))))), "derived_great_circle_from_observed_coordinates"


def _evaluate(refs: list[dict], identifier: str, records: dict, request: dict,
              revision: Any, assessments: list[dict], evaluation_at: str) -> dict:
    intent = request["intent"]
    shape = request.get("journey_shape", "round_trip")
    currency = intent.get("base_currency", "")
    checks: dict[str, dict] = {}
    warnings: list[str] = []

    def check(key: str, value: bool | None, reason: str) -> None:
        new = {"requirement_id": key, "status": _status(value), "reason": reason}
        old = checks.get(key)
        order = {"satisfied": 0, "unknown": 1, "violated": 2}
        if old is None or order[new["status"]] > order[old["status"]]:
            checks[key] = new

    selected = []
    seen_refs = set()
    for ref in refs:
        key = (ref.get("observation_id"), ref.get("offer_id"))
        if key in seen_refs:
            check("unique_tickets", False, "The same offer was included more than once.")
            continue
        seen_refs.add(key)
        if key not in records:
            check("admitted_offers", None, "An offer reference does not identify an admitted offer.")
            continue
        selected.append(records[key])

    nodes: dict[str, dict] = {}
    for side in ("origin", "destination"):
        region = intent.get(side, {})
        for n in region.get("anchor_nodes", []) + region.get("seed_gateway_nodes", []):
            if isinstance(n, dict):
                nodes[_node(n)] = n
    for observation, _ in selected:
        raw = observation.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        for n in observation.get("nodes", raw.get("nodes", [])):
            if isinstance(n, dict):
                nodes[_node(n)] = n

    movements = []
    owners = {}
    movement_signatures = set()
    prices = []
    air_total, full_total = Decimal(0), Decimal(0)
    surface_prices = {}
    air_known = full_known = True
    air_offer_count = 0
    timestamps = []
    quote_states = []
    verification = {"ticket_offers": [], "connections": [], "endpoints": []}
    disclosures = []
    complete_costs = True
    for observation, offer in selected:
        by_id = {m.get("movement_id"): m for m in observation.get("movements", [])}
        covered = []
        for movement_id in offer.get("movement_ids", []):
            movement = by_id.get(movement_id)
            if movement is None:
                check("movement_evidence", None, "A purchased movement is missing its observation.")
                continue
            signature = (_node(movement.get("from_node")), _node(movement.get("to_node")),
                         movement.get("departure"), movement.get("arrival"), movement.get("mode"),
                         movement.get("service_id"))
            if signature in movement_signatures:
                check("unique_tickets", False, "Tickets cover a duplicate movement; they cannot be added together.")
                continue
            movement_signatures.add(signature)
            saved = copy.deepcopy(movement)
            saved["observation_id"] = observation["observation_id"]
            movements.append(saved)
            covered.append(saved)
            owners[id(saved)] = (observation["observation_id"], offer["offer_id"])
        is_air = any(m.get("mode") == "flight" for m in covered)
        if is_air:
            air_offer_count += 1
        check("traveler_count", offer.get("traveler_count") == intent.get("adult_count", 1)
              if offer.get("traveler_count") is not None else None, "The observed price must cover the requested traveler count.")
        check("price_scope", offer.get("price_scope") == "party_total" if offer.get("price_scope") else None,
              "A payable party total is required; per-person prices are not silently multiplied.")
        amount = _convert(offer.get("price"), currency, intent)
        if not is_air:
            surface_prices[(observation["observation_id"], offer["offer_id"])] = amount
        if amount is None:
            full_known = False
            if is_air:
                air_known = False
            check("price_conversion", None, "Price or an attributed exchange rate is missing.")
        else:
            full_total += amount
            if is_air:
                air_total += amount
        baggage = intent.get("baggage_policy", {})
        needs_baggage = baggage.get("cabin_baggage", "none_required") != "none_required" or baggage.get("checked_bag_count", 0) > 0
        if is_air and needs_baggage and offer.get("price_includes_mandatory_baggage") is not True:
            extra = _convert(offer.get("mandatory_baggage_cost"), currency, intent)
            if baggage.get("paid_baggage_allowed") is False:
                check("baggage", False if offer.get("price_includes_mandatory_baggage") is False else None,
                      "Required baggage must already be included.")
            elif extra is None:
                check("baggage", None, "Required baggage inclusion or its payable cost is unknown.")
                air_known = full_known = False
            else:
                air_total += extra
                full_total += extra
        if offer.get("requires_accommodation") is True:
            accommodation = _convert(offer.get("required_accommodation_cost"), currency, intent)
            if accommodation is None:
                complete_costs = False
                if intent.get("ranking_basis") == "complete_journey_cost":
                    check("required_accommodation", None, "Required accommodation has no observed payable amount.")
                else:
                    warnings.append("Required accommodation is not included in the air-ticket comparison.")
            elif offer.get("price_includes_required_accommodation") is not True:
                full_total += accommodation
        if offer.get("mandatory_costs_complete") is False:
            complete_costs = False
            if intent.get("ranking_basis") == "complete_journey_cost":
                check("mandatory_costs_complete", None, "The source identifies unresolved mandatory costs.")
        capture_time = _capture_time(observation)
        capture_status = observation.get("capture_time_status", "known" if capture_time else "unknown")
        if capture_time is None and capture_status == "known":
            capture_status = "unreliable"
        captured = observation.get("captured_at") if capture_time else None
        if capture_time:
            timestamps.append(captured)
        metadata_issues = list(dict.fromkeys([
            *[str(issue) for issue in observation.get("metadata_issues", [])],
            *[str(issue) for issue in offer.get("metadata_issues", [])],
        ]))
        disclosures.extend(f"Offer {offer['offer_id']}: {issue}" for issue in metadata_issues)
        if capture_status != "known":
            disclosures.append(f"Offer {offer['offer_id']}: capture time is {capture_status}; its price can be compared but is not established as a fresh observation.")
        # Internal freshness TTLs are not provider promises.
        expiry = offer.get("provider_price_valid_until", offer.get("expires_at", observation.get("source_valid_until")))
        expired = bool(_time(expiry) and _time(evaluation_at) and _time(expiry) < _time(evaluation_at))
        if expired:
            warnings.append("A provider-declared validity period has ended; this is a historical comparison and requires rechecking.")
        quote_states.append({"observation_id": observation["observation_id"], "offer_id": offer["offer_id"],
                             "last_observed": captured, "provider_valid_until": expiry,
                             "capture_time_status": capture_status, "metadata_issues": metadata_issues,
                             "requires_recheck": expired})
        stage = offer.get("verification_stage", "unknown")
        if stage not in ("listing", "selected_itinerary", "fare_review", "checkout"):
            stage = "unknown"
        if is_air:
            verification["ticket_offers"].append({
                "observation_id": observation["observation_id"], "offer_id": offer["offer_id"],
                "stage": stage, "status": "reviewed" if stage in ("fare_review", "checkout") else "observed" if stage != "unknown" else "unknown",
            })
            if stage == "unknown":
                disclosures.append(f"Offer {offer['offer_id']}: verification stage was not recorded; booking readiness is not established by admission.")
            elif stage not in ("fare_review", "checkout"):
                disclosures.append(f"Offer {offer['offer_id']}: observed at {stage}; a final fare review or checkout has not been recorded.")
        raw_observation = observation.get("raw")
        raw_observation = raw_observation if isinstance(raw_observation, dict) else {}
        prices.append({"observation_id": observation["observation_id"], "offer_id": offer["offer_id"],
                       "seller": offer.get("seller", observation.get("source", {}).get("seller")),
                       "price": copy.deepcopy(offer.get("price")), "price_scope": offer.get("price_scope"),
                       "booking_url": offer.get("booking_url", observation.get("source", {}).get("page_url")),
                       "mandatory_baggage_cost": copy.deepcopy(offer.get("mandatory_baggage_cost")),
                       "ticketing_structure": copy.deepcopy(offer.get("ticketing_structure")),
                       "checked_bag_count": copy.deepcopy(offer.get("checked_bag_count")),
                       "included_baggage": copy.deepcopy(offer.get("included_baggage")),
                       "verification_stage": stage,
                       "metadata_issues": metadata_issues,
                       "capture_refs": copy.deepcopy(raw_observation.get("capture_refs", [])),
                       "limitations": copy.deepcopy(offer.get("restrictions", offer.get("ticket_context", {})))})

    movements.sort(key=lambda m: (_time(m.get("departure")).timestamp() if _time(m.get("departure")) else float("inf")))
    flights = [m for m in movements if m.get("mode") == "flight"]
    check("flight_itinerary", bool(flights), "A flight journey requires observed flight movements.")
    for m in movements:
        departure, arrival = _time(m.get("departure")), _time(m.get("arrival"))
        check("movement_times", arrival > departure if departure and arrival else None,
              "Every movement needs offset-aware departure and arrival in chronological order.")
        if m.get("mode") == "flight":
            identity = m.get("flight_identity", {})
            check("flight_identity", bool(m.get("service_id") or identity.get("flight_numbers")) or None,
                  "Flight numbers are needed for a usable itinerary.")
            check("airline_identity", bool(m.get("operator") or identity.get("marketing_carrier")) or None,
                  "The airline must be identified.")
    first = flights[0] if flights else {}
    last = flights[-1] if flights else {}
    departure_value = first.get("departure")
    return_value = None
    destination_arrival = None
    return_movement = None
    if flights and shape == "random_loop":
        check("origin_airport", _node(first.get("from_node")) in intent.get("departure_airports", []), "The loop must start at an accepted departure airport.")
        check("return_airport", _node(last.get("to_node")) in intent.get("return_airports", []), "The loop must end at an accepted return airport.")
        check("leg_count", intent.get("min_legs", 1) <= len(flights) <= intent.get("max_legs", len(flights)), "The observed number of flight legs must satisfy the request.")
    elif flights:
        check("origin_airport", _endpoint_matches(_node(first.get("from_node")), intent, "origin", nodes), "The first airport must satisfy the requested origin scope.")
        if shape == "one_way":
            check("destination_airport", _endpoint_matches(_node(last.get("to_node")), intent, "destination", nodes), "The final airport must satisfy the destination scope.")
            destination_arrival = last
        else:
            check("return_airport", _endpoint_matches(_node(last.get("to_node")), intent, "origin", nodes), "The return must reach an accepted origin airport.")
            for index, m in enumerate(flights[:-1]):
                if _endpoint_matches(_node(m.get("to_node")), intent, "destination", nodes) is True:
                    destination_arrival = m
                    return_movement = flights[index + 1]
                    break
            check("round_trip", bool(destination_arrival and return_movement) or None,
                  "Both outbound arrival and return departure at the requested destination must be identified.")
            if return_movement:
                return_value = return_movement.get("departure")
                check("return_departure_airport", _endpoint_matches(_node(return_movement.get("from_node")), intent, "destination", nodes), "Return departure must serve the requested destination.")

    used_gateways = {
        "origin": {_node(first.get("from_node")), _node(last.get("to_node"))} if shape == "round_trip" else {_node(first.get("from_node"))},
        "destination": {_node((destination_arrival or {}).get("to_node")), _node((return_movement or {}).get("from_node"))},
    }
    gateway_discoveries = [copy.deepcopy(item) for item in request.get("_gateway_discoveries", [])
                          if item["node"]["node_id"] in used_gateways[item["side"]]]
    for discovery in gateway_discoveries:
        disclosures.append(f"Gateway {discovery['node']['node_id']} was included through source-backed discovery; its access assessment ({discovery['access']['status']}) is an AI judgment, not a priced or dated endpoint connection.")

    stay_nights = stay_minutes = None
    stay_arrival, stay_departure = destination_arrival, return_movement
    destination = intent.get("destination", {})
    if destination_arrival and return_movement and destination.get("endpoint_policy") == "anchor_required":
        anchors = {_node(n) for n in destination.get("anchor_nodes", [])}
        lower, upper = _time(destination_arrival.get("arrival")), _time(return_value)
        at_destination = [m for m in movements if lower and upper and _time(m.get("arrival")) and lower <= _time(m["arrival"]) <= upper]
        arrivals = [m for m in at_destination if _node(m.get("to_node")) in anchors]
        departures = [m for m in movements if lower and upper and _time(m.get("departure")) and lower <= _time(m["departure"]) <= upper and _node(m.get("from_node")) in anchors]
        stay_arrival = arrivals[0] if arrivals else None
        stay_departure = departures[-1] if departures else None
    if stay_arrival and stay_departure:
        arrival = _time(_local_value(stay_arrival.get("arrival"), destination, nodes.get(_node(stay_arrival.get("to_node")))))
        departure = _time(_local_value(stay_departure.get("departure"), destination, nodes.get(_node(stay_departure.get("from_node")))))
        if arrival and departure:
            stay_minutes = Decimal(str((departure - arrival).total_seconds())) / 60
            stay_nights = (departure.date() - arrival.date()).days
            check("stay_chronology", stay_minutes >= 0, "Return departure must follow arrival at the destination.")
    for field, actual, lower in (("minimum_stay_nights", stay_nights, True), ("maximum_stay_nights", stay_nights, False), ("maximum_stay_minutes", stay_minutes, False)):
        if intent.get(field) is not None:
            check(field, (actual >= intent[field] if lower else actual <= intent[field]) if actual is not None else None, "The observed destination stay must satisfy the requested limit.")
    window = intent.get("outbound_departure_window", intent.get("departure_window", intent.get("first_departure_window")))
    local_departure = _local_value(departure_value, intent.get("origin", {}), nodes.get(_node(first.get("from_node"))))
    local_return = _local_value(return_value, intent.get("destination", {}), nodes.get(_node((return_movement or {}).get("from_node"))))
    if window:
        check("departure_window", _window(local_departure, window), "Departure must fall inside the requested window in its endpoint timezone.")
    if intent.get("return_departure_window"):
        check("return_departure_window", _window(local_return, intent["return_departure_window"]), "Return departure must fall inside its requested window in its endpoint timezone.")
    for field in ("arrival_window", "return_arrival_window"):
        if intent.get(field):
            local_arrival = _local_value(last.get("arrival"), intent.get("destination" if shape == "one_way" else "origin", {}), nodes.get(_node(last.get("to_node"))))
            check(field, _window(local_arrival, intent[field]), "Actual final flight arrival must fall inside the requested window.")
    earliest = intent.get("first_leg_earliest_local_time", {}).get(_node(first.get("from_node")))
    if earliest:
        check("first_leg_earliest_local_time", departure_value[11:16] >= earliest if departure_value else None,
              "The first flight must leave after the requested local time.")
    if intent.get("travel_windows"):
        matches = []
        for w in intent["travel_windows"]:
            a = _window(local_departure, w.get("outbound_departure_window", w.get("departure_window")))
            b = _window(local_return, w.get("return_departure_window"))
            stay_ok = True
            if w.get("minimum_stay_nights") is not None:
                stay_ok = stay_nights >= w["minimum_stay_nights"] if stay_nights is not None else None
            if w.get("maximum_stay_nights") is not None and stay_ok is not False:
                stay_ok = stay_nights <= w["maximum_stay_nights"] if stay_nights is not None else None
            matches.append(False if False in (a, b, stay_ok) else None if None in (a, b, stay_ok) else True)
        check("travel_windows", True if True in matches else None if None in matches else False,
              "Outbound and return must belong to the same requested paired window.")
    for field, value in (("exact_departure_dates", local_departure), ("outbound_exact_departure_dates", local_departure), ("return_exact_departure_dates", local_return)):
        if intent.get(field):
            check(field, value[:10] in intent[field] if value else None, "Departure must match one of the explicitly requested dates.")

    for previous, following in zip(movements, movements[1:]):
        arrival, departure = _time(previous.get("arrival")), _time(following.get("departure"))
        gap = Decimal(str((departure - arrival).total_seconds())) / 60 if departure and arrival else None
        is_stay = (previous is destination_arrival and following is return_movement) or (previous is stay_arrival and following is stay_departure)
        check("chronology", gap >= 0 if gap is not None else None, "Selected purchases must not overlap in time.")
        same_location = _node(previous.get("to_node")) == _node(following.get("from_node"))
        if not same_location:
            check("internal_transfer", None, "A change of nodes requires observed connecting transport; it is not assumed free or instantaneous.")
        separate = owners[id(previous)] != owners[id(following)]
        if not is_stay and previous.get("mode") == following.get("mode") == "flight":
            transfer = following.get("transfer_from_previous", {})
            transfer = transfer if isinstance(transfer, dict) else {}
            protection = transfer.get("connection_protection")
            if intent.get("self_transfer_policy", "allow") == "never":
                protected = False if separate or protection == "self_transfer" else True if protection == "carrier_protected" and transfer.get("status") == "verified" else None
                check("self_transfer", protected, "No-self-transfer requires observed carrier protection; a joint seller price alone does not prove it.")
            transit_policy = intent.get("airport_transit", {}).get("unknown_policy", intent.get("airport_transit_unknown_policy", "allow_with_warning"))
            status = transfer.get("status", "unknown")
            if status == "infeasible":
                check("airport_transit", False, "The observed airport connection is infeasible.")
            elif status != "verified":
                if transit_policy == "reject":
                    check("airport_transit", None, "Transit is unverified and this request excludes unverified airport transfers.")
                else:
                    warnings.append("Airport transfer feasibility is unverified; this request permits it with a warning.")
            if intent.get("airport_transit", {}).get("airside_only") is True:
                airside = transfer.get("airside_connection")
                if transfer.get("requires_landside_exit") is True:
                    airside = False
                check("airside_only", airside if isinstance(airside, bool) else None, "Airside-only transit requires observed airside connectivity.")
            if transfer.get("minimum_connection_minutes") is not None:
                check("observed_minimum_connection", gap >= transfer["minimum_connection_minutes"] if gap is not None else None, "Connection time must meet the observed minimum.")
        if not is_stay:
            if intent.get("connection_buffer_minutes") is not None:
                check("connection_buffer_minutes", gap >= intent["connection_buffer_minutes"] if gap is not None else None, "Connection time must meet the requested buffer.")
            if intent.get("max_connection_wait_minutes") is not None:
                check("max_connection_wait_minutes", gap <= intent["max_connection_wait_minutes"] if gap is not None else None, "Connection wait must meet the requested maximum.")
            if intent.get("overnight_allowed") is False:
                check("overnight_allowed", arrival.date() == departure.date() if arrival and departure else None, "Overnight connections are not permitted by this request.")

    duration = None
    if movements and _time(movements[0].get("departure")) and _time(movements[-1].get("arrival")):
        duration = Decimal(str((_time(movements[-1]["arrival"]) - _time(movements[0]["departure"])).total_seconds())) / 60
    if intent.get("maximum_total_duration_minutes") is not None:
        check("maximum_total_duration_minutes", duration <= intent["maximum_total_duration_minutes"] if duration is not None else None,
              "Total journey duration must satisfy the requested maximum.")
    travel_duration = duration - stay_minutes if duration is not None and stay_minutes is not None else duration if shape != "round_trip" else None
    for previous, following in zip(flights, flights[1:]):
        if previous is destination_arrival and following is return_movement:
            continue
        transfer = following.get("transfer_from_previous", {})
        transfer = transfer if isinstance(transfer, dict) else {}
        previous_owner, following_owner = owners[id(previous)], owners[id(following)]
        purchase_refs = list(dict.fromkeys((previous_owner, following_owner)))
        connection_path = movements[movements.index(previous):movements.index(following) + 1]
        continuous = all(_node(a.get("to_node")) == _node(b.get("from_node"))
                         and _time(a.get("arrival")) <= _time(b.get("departure"))
                         for a, b in zip(connection_path, connection_path[1:]))
        protection = transfer.get("connection_protection", "unknown")
        if protection not in ("carrier_protected", "self_transfer"):
            protection = "unknown"
        feasibility = transfer.get("status", "unknown")
        if feasibility not in ("verified", "infeasible"):
            feasibility = "unknown"
        connection = {
            "previous_movement_id": previous["movement_id"], "following_movement_id": following["movement_id"],
            "from_node": previous.get("to_node"), "to_node": following.get("from_node"),
            "ticket_relationship": "same_purchase" if previous_owner == following_owner else "separate_purchases",
            "ticketing_structures": [copy.deepcopy(records[ref][1].get("ticketing_structure")) for ref in purchase_refs],
            "offer_refs": [{"observation_id": oid, "offer_id": fid} for oid, fid in purchase_refs],
            "protection": protection, "feasibility_status": feasibility,
            "movement_chain_status": "observed_connection" if continuous else "unverified",
            "ground_movement_ids": [m["movement_id"] for m in connection_path[1:-1] if m.get("mode") != "flight"],
        }
        verification["connections"].append(connection)
        # Movement IDs repeat across separately recorded purchases; name the
        # connection by flight number and place so each one stays distinct.
        services = [next(iter((m.get("flight_identity") or {}).get("flight_numbers") or []), None) or m["movement_id"]
                    for m in (previous, following)]
        places = list(dict.fromkeys(_node(n) for n in (previous.get("to_node"), following.get("from_node"))))
        connection_label = f"Connection {services[0]} to {services[1]} at {'->'.join(places)}"
        if feasibility != "verified":
            disclosures.append(f"{connection_label}: feasibility is {feasibility}.")
        if protection == "unknown":
            disclosures.append(f"{connection_label}: transfer protection is unknown; one purchase or a single ticket is not proof of protection.")
        elif protection == "self_transfer":
            disclosures.append(f"{connection_label}: the source identifies a self-transfer.")
        if not continuous:
            disclosures.append(f"{connection_label}: a continuous observed movement chain is missing.")
        transit_policy = intent.get("airport_transit", {}).get("unknown_policy", intent.get("airport_transit_unknown_policy", "allow_with_warning"))
        if transfer.get("status") == "infeasible":
            check("airport_transit", False, "The observed connection between flights is infeasible.")
        elif transfer.get("status") != "verified":
            if transit_policy == "reject":
                check("airport_transit", None, "Transit between consecutive flights is unverified, including intervening ground transport.")
            else:
                warnings.append("Airport transfer feasibility is unverified; this request permits it with a warning.")
        if intent.get("self_transfer_policy") == "never":
            transfer = following.get("transfer_from_previous", {})
            transfer = transfer if isinstance(transfer, dict) else {}
            separate = owners[id(previous)] != owners[id(following)]
            protection = transfer.get("connection_protection")
            protected = False if separate or protection == "self_transfer" else True if protection == "carrier_protected" and transfer.get("status") == "verified" else None
            check("self_transfer", protected, "Connection protection is checked between consecutive flights even when ground transport lies between them.")
        if intent.get("airport_transit", {}).get("airside_only") is True and _node(previous.get("to_node")) != _node(following.get("from_node")):
            check("airside_only", False, "An airport change cannot satisfy airside-only transit.")
    if intent.get("maximum_transfer_count") is not None:
        transfers = max(0, len(flights) - (2 if shape == "round_trip" and return_movement else 1))
        check("maximum_transfer_count", transfers <= intent["maximum_transfer_count"], "Flight transfer count excludes the destination stay and must satisfy the requested limit.")
    if intent.get("allowed_modes"):
        check("allowed_modes", all(m.get("mode") in intent["allowed_modes"] for m in movements), "Every movement must use an allowed mode.")
    for field, begin, finish in (("maximum_outbound_duration_minutes", movements[0] if movements else {}, stay_arrival or {}),
                                 ("maximum_return_duration_minutes", stay_departure or {}, movements[-1] if movements else {})):
        if intent.get(field) is not None:
            a, b = _time(begin.get("departure")), _time(finish.get("arrival"))
            elapsed = Decimal(str((b - a).total_seconds())) / 60 if a and b else None
            origin = intent.get("origin", {})
            if origin.get("endpoint_policy") == "anchor_required":
                anchors = {_node(n) for n in origin.get("anchor_nodes", [])}
                endpoint = _node(begin.get("from_node")) if field == "maximum_outbound_duration_minutes" else _node(finish.get("to_node"))
                if endpoint not in anchors:
                    elapsed = None
            check(field, elapsed <= intent[field] if elapsed is not None else None, "The observed journey duration must satisfy the requested limit.")

    # Endpoint limits apply only when declared. Absent access observations remain
    # unknown; choosing an airfare objective does not waive an explicit limit.
    endpoint_paths = []
    for side in ("origin", "destination"):
        region = intent.get(side, {})
        anchors = {_node(n) for n in region.get("anchor_nodes", [])}
        segments = []
        if flights and side == "origin":
            start_index, end_index = movements.index(first), movements.index(last)
            segments.append(("access", movements[:start_index], _node(first.get("from_node")) in anchors))
            if shape == "round_trip":
                segments.append(("egress", movements[end_index + 1:], _node(last.get("to_node")) in anchors))
        elif destination_arrival:
            arrival_index = movements.index(destination_arrival)
            if shape == "one_way":
                segments.append(("egress", movements[arrival_index + 1:], _node(destination_arrival.get("to_node")) in anchors))
            elif return_movement:
                return_index = movements.index(return_movement)
                arrival_anchor_index = next((i for i in range(arrival_index + 1, return_index) if _node(movements[i].get("to_node")) in anchors), arrival_index)
                departure_anchor_index = next((i for i in range(return_index - 1, arrival_index, -1) if _node(movements[i].get("from_node")) in anchors), return_index)
                segments.append(("egress", movements[arrival_index + 1:arrival_anchor_index + 1], _node(destination_arrival.get("to_node")) in anchors))
                segments.append(("access", movements[departure_anchor_index:return_index], _node(return_movement.get("from_node")) in anchors))
        for direction, segment, coincident in segments:
            known = coincident or bool(segment)
            if segment:
                at_anchor = _node(segment[0].get("from_node")) if direction == "access" else _node(segment[-1].get("to_node"))
                known = at_anchor in anchors
            gateway = (_node(first.get("from_node")) if direction == "access" else _node(last.get("to_node"))) if side == "origin" else (
                _node((return_movement or {}).get("from_node")) if direction == "access" else _node(destination_arrival.get("to_node")))
            outside = any(d["side"] == side and d["node"]["node_id"] == gateway and d["outside_seed_pool"] for d in gateway_discoveries)
            required = region.get("endpoint_policy") == "anchor_required" and (intent.get("ranking_basis") == "complete_journey_cost" or outside)
            endpoint_paths.append((side, direction, gateway, segment))
            continuous = coincident
            if segment:
                at_gateway = _node(segment[-1].get("to_node")) if direction == "access" else _node(segment[0].get("from_node"))
                continuous = known and at_gateway == gateway and all(
                    _node(a.get("to_node")) == _node(b.get("from_node"))
                    and _time(a.get("arrival")) <= _time(b.get("departure"))
                    for a, b in zip(segment, segment[1:]))
            endpoint_status = "not_required" if not required else "observed_connection" if continuous else "unverified"
            verification["endpoints"].append({
                "role": side + "_" + direction, "anchor_node_ids": sorted(anchors),
                "gateway_node_id": gateway, "status": endpoint_status,
                "movement_ids": [m["movement_id"] for m in segment],
            })
            if endpoint_status == "unverified":
                disclosures.append(f"{side}_{direction}: connection between the requested Anchor and Gateway is unverified; airfare ranking does not establish it.")
            duration_limit = region.get("max_" + direction + "_time_minutes")
            if duration_limit is not None:
                a = _time(segment[0].get("departure")) if segment else None
                b = _time(segment[-1].get("arrival")) if segment else None
                elapsed = Decimal(str((b - a).total_seconds())) / 60 if a and b and known else Decimal(0) if coincident and not segment else None
                check(side + "_" + direction + "_time", elapsed <= duration_limit if elapsed is not None else None, "The requested endpoint travel-time limit needs observed transport between its anchor and airport.")
            allowed = region.get("allowed_ground_modes")
            if allowed:
                check(side + "_" + direction + "_modes", all(m.get("mode") in allowed for m in segment) if known else None, "Endpoint transport must use the requested ground modes.")
            ceiling = _convert(region.get("max_ground_cost"), currency, intent)
            if region.get("max_ground_cost") is not None:
                if not known or ceiling is None:
                    check(side + "_" + direction + "_cost", None, "The endpoint cost ceiling cannot be checked without its observed payable transport price.")
                elif segment:
                    segment_owners = {owners[id(m)] for m in segment}
                    for observation, offer in selected:
                        if (observation["observation_id"], offer["offer_id"]) in segment_owners:
                            amount = _convert(offer.get("price"), currency, intent)
                            check(side + "_" + direction + "_cost", amount <= ceiling if amount is not None else None, "Each endpoint transport purchase must meet the requested hard price ceiling.")

    # Presence is based on observed movement away from and back to the exact node.
    for index, interval in enumerate(intent.get("presence_intervals", [])):
        start, end = _time(interval.get("start")), _time(interval.get("end"))
        location = interval.get("location_node_id")
        result = None
        if start and end:
            away = None
            for m in movements:
                depart, arrive = _time(m.get("departure")), _time(m.get("arrival"))
                if _node(m.get("from_node")) == location and depart:
                    away = depart
                if away and _node(m.get("to_node")) == location and arrive:
                    result = not (away < end and arrive > start)
                    if result is False:
                        break
                    away = None
            if away and away < end:
                result = False if any(_time(m.get("departure")) and start <= _time(m["departure"]) < end for m in movements) else None
        check(f"presence_interval_{index}", result, "Presence requires actual departure from and return to the specified location; an airport arrival does not prove city arrival.")

    ranking_basis = intent.get("ranking_basis", "mandatory_air_ticket_total")
    if ranking_basis == "complete_journey_cost":
        for side in ("origin", "destination"):
            region = intent.get(side, {})
            if region.get("endpoint_policy") == "anchor_required":
                anchors = {_node(n) for n in region.get("anchor_nodes", [])}
                if side == "origin":
                    connected = bool(movements and _node(movements[0].get("from_node")) in anchors and (shape != "round_trip" or _node(movements[-1].get("to_node")) in anchors))
                else:
                    connected = any(_node(m.get("to_node")) in anchors for m in movements)
                    if shape == "round_trip":
                        connected = connected and any(_node(m.get("from_node")) in anchors for m in movements)
                check(side + "_completion", True if connected else None, "Complete journey cost requires observed transport to the requested anchor.")
    internal_surface = {owners[id(m)] for m in movements[movements.index(flights[0]):movements.index(flights[-1]) + 1]
                        if owners[id(m)] in surface_prices} if flights else set()
    endpoint_ids = {id(m) for _, _, _, path in endpoint_paths for m in path}
    internal_surface -= {ref for ref in internal_surface if all(id(m) in endpoint_ids for m in movements if owners[id(m)] == ref)}
    surface_known = all(surface_prices[ref] is not None for ref in internal_surface)
    core_total = air_total + sum((surface_prices[ref] for ref in internal_surface if surface_prices[ref] is not None), Decimal(0))
    outside_access, access_extra, access_known = [], Decimal(0), True
    if ranking_basis != "complete_journey_cost":
        outside = {(d["side"], d["node"]["node_id"]): d for d in request.get("_gateway_discoveries", [])
                   if d["outside_seed_pool"] and intent.get(d["side"], {}).get("endpoint_policy") == "anchor_required"}
        accepted = {side: _airports(intent, side) - {g for s, g in outside if s == side}
                    | {_node(n) for n in intent.get(side, {}).get("anchor_nodes", [])} for side in ("origin", "destination")}
        paths = [(s, direction, g, path) for s, direction, g, path in endpoint_paths if (s, g) in outside]
        # Positioning flights already belong to air_total. Identify separately
        # priced paths on either side without adding them again or splitting a joint fare.
        positioning = []
        for side in ("origin", "destination"):
            for flight in flights:
                index = movements.index(flight)
                for direction, gateway in (("access", _node(flight.get("from_node"))), ("egress", _node(flight.get("to_node")))):
                    if (side, gateway) not in outside:
                        continue
                    if side == "origin":
                        boundary = destination_arrival if direction == "access" else return_movement
                        if not boundary or (index > movements.index(boundary) if direction == "access" else index < movements.index(boundary)):
                            continue
                    elif not destination_arrival or index < movements.index(destination_arrival):
                        continue
                    if direction == "access":
                        start = next((i for i in range(index - 1, -1, -1) if _node(movements[i].get("from_node")) in accepted[side]), index)
                        path = movements[start:index]
                    else:
                        end = next((i for i in range(index + 1, len(movements)) if _node(movements[i].get("to_node")) in accepted[side]), index)
                        path = movements[index + 1:end + 1]
                    if path:
                        positioning.append((side, direction, gateway, path))
        def access_connected(side, direction, gateway, path):
            if not path:
                return False
            home = _node(path[0].get("from_node")) if direction == "access" else _node(path[-1].get("to_node"))
            airport = _node(path[-1].get("to_node")) if direction == "access" else _node(path[0].get("from_node"))
            return home in accepted[side] and airport == gateway and all(
                _node(a.get("to_node")) == _node(b.get("from_node")) for a, b in zip(path, path[1:]))

        path_ids = {id(m) for s, direction, g, path in endpoint_paths + positioning if access_connected(s, direction, g, path) for m in path}
        endpoint_only = {ref for ref in owners.values() if all(id(m) in path_ids for m in movements if owners[id(m)] == ref)}
        positioned_roles = set()
        for item in sorted(positioning, key=lambda p: len(p[3]), reverse=True):
            if item[:2] not in positioned_roles and access_connected(*item) and {owners[id(m)] for m in item[3]} <= endpoint_only:
                paths = [p for p in paths if p[:2] != item[:2]] + [item]
                positioned_roles.add(item[:2])
        core_refs = internal_surface | {owners[id(m)] for m in flights}
        observed = {}
        for side, direction, gateway, path in paths:
            path_refs = {owners[id(m)] for m in path}
            if access_connected(side, direction, gateway, path) and (path_refs <= endpoint_only or (side, direction, gateway, path) in endpoint_paths):
                for ref in sorted(path_refs):
                    if ref not in observed:
                        observation, offer = records[ref]
                        bundled = ref not in endpoint_only
                        amount = _convert(offer.get("price"), currency, intent) if not bundled else None
                        component = {"side": side, "gateway_node_ids": [], "directions": [], "cost": _money(amount, currency),
                                     "basis": "observed", "offer_refs": [{"observation_id": ref[0], "offer_id": ref[1]}], "included_in_core_total": ref in core_refs}
                        if bundled:
                            component["allocation"] = "included_in_joint_price"
                        outside_access.append(component)
                        observed[ref] = component
                        if amount is None and not bundled:
                            access_known = False
                            check(side + "_" + direction + "_outside_gateway_cost", None, f"Access cost unknown for outside gateway {gateway} (price or FX missing).")
                        elif ref not in core_refs and amount is not None:
                            access_extra += amount
                    if direction not in observed[ref]["directions"]:
                        observed[ref]["directions"].append(direction)
                    if gateway not in observed[ref]["gateway_node_ids"]:
                        observed[ref]["gateway_node_ids"].append(gateway)
            elif (side, direction, gateway, path) in endpoint_paths:
                discovery = outside[(side, gateway)]
                access = discovery["access"]
                basis = access.get("basis") if access.get("basis") in ("observed", "estimated") else "unknown"
                amount = _convert(access.get("cost"), currency, intent) if basis in ("observed", "estimated") else None
                outside_access.append({"side": side, "gateway_node_ids": [gateway], "directions": [direction], "cost": _money(amount, currency),
                                       "basis": basis, "observation_id": discovery["observation_id"], "included_in_core_total": False})
                if amount is None:
                    access_known = False
                    check(side + "_" + direction + "_outside_gateway_cost", None, f"Access cost unknown for outside gateway {gateway} (amount, FX or basis missing).")
                else:
                    access_extra += amount
                    if basis == "estimated":
                        disclosures.append(f"{side}_{direction}: outside gateway {gateway} access cost is AI-estimated, not an observed payable price.")
    ranking = full_total if ranking_basis == "complete_journey_cost" and full_known else core_total + access_extra if ranking_basis != "complete_journey_cost" and air_known and air_offer_count and surface_known and access_known else None
    if ranking is None:
        check("ranking_cost", None, "The requested comparison cost is not known.")
    if intent.get("budget"):
        ceiling = _convert(intent["budget"], currency, intent)
        check("budget", ranking <= ceiling if ranking is not None and ceiling is not None else None, "The hard budget applies to the requested comparison cost.")
    target = _convert(intent.get("price_target"), currency, intent)

    distance_total = Decimal(0)
    distance_known = True
    distance_basis = []
    for movement in flights:
        value, basis = _distance(movement, nodes)
        distance_basis.append({"movement_id": movement.get("movement_id"), "basis": basis})
        if value is None:
            distance_known = False
        else:
            distance_total += value
    efficiency = distance_total / ranking if distance_known and ranking is not None and ranking > 0 else None
    objective = intent.get("objective", "distance_per_currency" if shape == "random_loop" else "price")
    if objective == "stay_duration":
        check("objective_data", stay_minutes is not None or None, "Stay duration must be known for this ranking objective.")
    if objective == "travel_duration":
        check("objective_data", travel_duration is not None or None, "Travel duration must be known for this ranking objective.")
    if objective == "distance_per_currency":
        check("objective_data", efficiency is not None or None, "Distance and price must be known for distance-per-currency ranking.")
    gate = intent.get("quality_gate", {})
    minimum = _number(gate.get("minimum_complete_loop_efficiency_km_per_base_currency_unit"))
    if minimum is not None:
        check("minimum_loop_efficiency", efficiency >= minimum if efficiency is not None else None, "Loop efficiency must meet the user-specified minimum.")
    maximum_leg = _number(gate.get("maximum_leg_price_in_base_currency"))
    if maximum_leg is not None:
        for observation, offer in selected:
            by_id = {m.get("movement_id"): m for m in observation.get("movements", [])}
            legs = [by_id[m] for m in offer.get("movement_ids", []) if m in by_id and by_id[m].get("mode") == "flight"]
            if len(legs) == 1:
                price = _convert(offer.get("price"), currency, intent)
                baggage = intent.get("baggage_policy", {})
                needs_baggage = baggage.get("cabin_baggage", "none_required") != "none_required" or baggage.get("checked_bag_count", 0) > 0
                if needs_baggage and offer.get("price_includes_mandatory_baggage") is not True:
                    extra = _convert(offer.get("mandatory_baggage_cost"), currency, intent)
                    price = price + extra if price is not None and extra is not None else None
                check("maximum_leg_price", price <= maximum_leg if price is not None else None, "Each separately priced leg must meet the requested price ceiling.")
            elif legs:
                check("maximum_leg_price", None, "A joint-ticket price cannot be invented for individual legs.")
    for requirement in intent.get("additional_requirements", []):
        key = requirement["requirement_id"]
        evidence = [a for a in assessments if a.get("candidate_id") == identifier and a.get("requirement_id") == key]
        assessment = evidence[-1] if evidence else None
        checks["additional:" + key] = {"requirement_id": key, "status": assessment["status"] if assessment else "unknown",
                                      "reason": assessment.get("reason", "") if assessment else requirement.get("text", "No assessment saved."),
                                      "evidence_refs": copy.deepcopy(assessment.get("evidence_refs", [])) if assessment else []}

    scope_checks = ("round_trip",) if shape == "round_trip" else ("origin_airport", "destination_airport") if shape == "one_way" else ("origin_airport", "return_airport")
    journey_complete = all(checks.get(key, {}).get("status") == "satisfied" for key in scope_checks)
    if not journey_complete:
        # Keep a priced purchase available to combine, without applying a whole
        # journey's outbound window/stay checks to a return leg or other fragment.
        checks = {k: v for k, v in checks.items() if k in {"traveler_count", "price_scope", "price_conversion", "baggage", "admitted_offers", "flight_identity", "airline_identity", "movement_times"}}
        check("journey_scope", None, "Partial component; combine purchases to cover the requested journey before comparison.")
        verification["endpoints"] = []
        disclosures = [d for d in disclosures if not d.startswith(("origin_access:", "origin_egress:", "destination_access:", "destination_egress:", "Gateway "))]
        warnings = [w for w in warnings if not w.startswith("Ranking covers mandatory air tickets")]

    all_reviewed = bool(verification["ticket_offers"]) and all(t["status"] == "reviewed" for t in verification["ticket_offers"])
    all_connected = all(c["feasibility_status"] == "verified" and c["protection"] != "unknown"
                        and c["movement_chain_status"] == "observed_connection" for c in verification["connections"])
    endpoints_observed = all(e["status"] != "unverified" for e in verification["endpoints"])
    checks_satisfied = all(item["status"] == "satisfied" for item in checks.values())
    needs_price_recheck = any(quote["requires_recheck"] for quote in quote_states)
    capture_times_known = all(quote["capture_time_status"] == "known" for quote in quote_states)
    verification["overall_status"] = "verified" if all_reviewed and all_connected and endpoints_observed and checks_satisfied and capture_times_known and not needs_price_recheck else "partial" if selected else "unknown"
    verification["scope"] = "Recorded fare stage, connection statements and endpoint movements; not a guarantee of inventory, entry eligibility or future operation."
    required_disclosures = list(dict.fromkeys([
        *disclosures, *warnings,
        *(f"{item['requirement_id']}: {item['status']}. {item['reason']}" for item in checks.values() if item["status"] != "satisfied"),
    ]))
    return {"candidate_id": identifier, "offer_refs": copy.deepcopy(refs), "request_revision": revision,
            "eligibility_scope": "ticket_and_declared_constraints" if journey_complete else "component_facts", "verification": verification,
            "discovery_refs": list(dict.fromkeys(d["observation_id"] for d in gateway_discoveries)),
            "gateway_discoveries": gateway_discoveries,
            "required_disclosures": required_disclosures,
            "requirements": list(checks.values()), "warnings": list(dict.fromkeys(warnings)),
            "ranking_cost": _money(ranking, currency) if journey_complete else None, "mandatory_air_ticket_total": _money(air_total if air_known and air_offer_count else None, currency),
            "gateway_complete_cost": _money(core_total if air_known and air_offer_count and surface_known else None, currency),
            "outside_gateway_access": outside_access if journey_complete else [],
            "journey_complete": journey_complete,
            "component_cost": _money(full_total, currency) if not journey_complete and full_known else None,
            "ranking_basis": ranking_basis, "objective": objective, "prices": prices,
            "cost_coverage": "known_components" if not journey_complete or ranking_basis == "complete_journey_cost" else "tickets_internal_surface_and_outside_gateway_access",
            "disclosed_mandatory_costs_known": complete_costs and full_known,
            "known_cost_components": _money(full_total if full_known else None, currency),
            "price_target_met": ranking <= target if journey_complete and ranking is not None and target is not None else None,
            "movements": movements, "outbound_departure": departure_value, "return_departure": return_value,
            "final_arrival": movements[-1].get("arrival") if movements else None,
            "outbound_date": departure_value[:10] if departure_value else None,
            "return_date": return_value[:10] if return_value else None,
            "dates": {"outbound_date": departure_value[:10] if departure_value else None,
                      "return_date": return_value[:10] if return_value else None},
            "origin_airport": first.get("from_node"), "destination_airport": destination_arrival.get("to_node") if destination_arrival else None,
            "return_airport": last.get("to_node"), "stay_nights": stay_nights,
            "stay_minutes": str(stay_minutes) if stay_minutes is not None else None,
            "travel_duration_minutes": str(travel_duration) if travel_duration is not None else None,
            "total_duration_minutes": str(duration) if duration is not None else None,
            "distance_km": str(distance_total) if distance_known and flights else None,
            "distance_basis": distance_basis, "distance_per_currency": str(efficiency) if efficiency is not None else None,
            "last_observed": max(timestamps, key=lambda value: _time(value).timestamp()) if timestamps else None, "quote_observations": quote_states}


def evaluate_session(state: dict, *, evaluation_at: str, request_revision: int | None = None) -> dict:
    """Return all saved alternatives, ranking only those meeting the current request."""
    current = next(r for r in state["requests"] if r["revision"] == request_revision) if request_revision else state["requests"][-1]
    request, revision = copy.deepcopy(current["request"]), current["revision"]
    # A judgment belongs to the requirement's content, not the enclosing Request.
    # A removal or edit breaks continuity even if that ID/text is later restored.
    requirements = {r["revision"]: {v["requirement_id"]: v["text"] for v in r["request"]["intent"].get("additional_requirements", [])}
                    for r in state["requests"]}
    assessments = [a for a in state.get("assessments", []) if a.get("request_revision") in requirements
                   and a.get("requirement_id") in requirements[revision]
                   and all(values.get(a["requirement_id"]) == requirements[revision][a["requirement_id"]]
                           for number, values in requirements.items() if min(a["request_revision"], revision) <= number <= max(a["request_revision"], revision))]
    discoveries = discovered_gateways({**state, "requests": [r for r in state["requests"] if r["revision"] <= revision]})
    request["_gateway_discoveries"] = [d for d in discoveries if d["eligible_for_expansion"]]
    for discovery in request["_gateway_discoveries"]:
        region = request["intent"][discovery["side"]]
        seeds = region.setdefault("seed_gateway_nodes", [])
        if discovery["node"]["node_id"] not in {_node(n) for n in seeds}:
            seeds.append(copy.deepcopy(discovery["node"]))
    records = {}
    signals = []
    candidates = {}
    latest = {}
    for observation in state.get("observations", []):
        admitted_count = 0
        for offer in observation.get("offers", []):
            if offer.get("admission", observation.get("admission", {})).get("status") != "admitted":
                continue
            ref = {"observation_id": observation["observation_id"], "offer_id": offer["offer_id"]}
            records[(ref["observation_id"], ref["offer_id"])] = (observation, offer)
            key = _variant_key(observation, offer)
            captured = _capture_time(observation)
            order = (captured.timestamp(), observation["observation_id"], offer["offer_id"]) if captured else None
            if order is not None and (key not in latest or order > latest[key][0]):
                latest[key] = (order, (ref["observation_id"], ref["offer_id"]))
            candidates[candidate_id([ref])] = [ref]
            admitted_count += 1
        if not admitted_count:
            signals.append({"observation_id": observation["observation_id"], "source": copy.deepcopy(observation.get("source")),
                            "captured_at": observation.get("captured_at"), "admission": copy.deepcopy(observation.get("admission")),
                            "capture_time_status": observation.get("capture_time_status", "known" if _capture_time(observation) else "unknown"),
                            "metadata_issues": copy.deepcopy(observation.get("metadata_issues", [])),
                            "raw": copy.deepcopy(observation.get("raw", {}))})
    for combination in state.get("combinations", []):
        refs = combination.get("offer_refs", [])
        candidates[combination.get("candidate_id") or candidate_id(refs)] = refs
    superseded_refs = {}
    for key, (observation, offer) in records.items():
        captured = _capture_time(observation)
        newest = latest.get(_variant_key(observation, offer))
        if captured and newest and newest[0][0] > captured.timestamp():
            superseded_refs[key] = newest[1]
    explicit_ids = {c.get("candidate_id") or candidate_id(c.get("offer_refs", [])) for c in state.get("combinations", [])}
    entries, alternatives, components, superseded = [], [], [], []
    for identifier, refs in candidates.items():
        result = _evaluate(refs, identifier, records, request, revision, assessments, evaluation_at)
        replaced = [superseded_refs[(r.get("observation_id"), r.get("offer_id"))] for r in refs
                    if (r.get("observation_id"), r.get("offer_id")) in superseded_refs]
        if replaced:
            result["superseded_by"] = [{"observation_id": o, "offer_id": f} for o, f in replaced]
            result["verification"]["overall_status"] = "partial"
            result["required_disclosures"].append("This candidate uses an older Seller Variant snapshot; a newer saved observation is available.")
            if identifier not in explicit_ids:
                result["status"] = "superseded"
                superseded.append(result)
                continue
            result["requirements"].append({"requirement_id": "current_offer_snapshot", "status": "unknown",
                                            "reason": "This explicit combination uses an older seller-variant snapshot; select the updated offer before ranking it."})
        if not result["journey_complete"]:
            result["status"] = "partial_component"
            components.append(result)
            continue
        failed = [r for r in result["requirements"] if r["status"] != "satisfied"]
        result["status"] = "violated" if any(r["status"] == "violated" for r in failed) else "unknown" if failed else "eligible"
        (alternatives if failed else entries).append(result)

    entries.sort(key=comparison_key)
    alternatives.sort(key=comparison_key)
    components.sort(key=lambda e: comparison_key({**e, "ranking_cost": e["component_cost"]}))
    # Different sellers remain visible under one identical journey/ticket/fare
    # structure. Grouping never combines or splits ticket prices.
    grouped = {}
    for entry in entries:
        partitions = []
        for ref in entry["offer_refs"]:
            observation, offer = records[(ref["observation_id"], ref["offer_id"])]
            partitions.append(_variant_key(observation, offer, include_seller=False))
        group_key = tuple(sorted(partitions))
        variant = {key: copy.deepcopy(entry[key]) for key in ("candidate_id", "offer_refs", "prices", "ranking_cost", "mandatory_air_ticket_total", "gateway_complete_cost", "outside_gateway_access", "last_observed", "quote_observations", "requirements", "warnings", "eligibility_scope", "verification", "required_disclosures", "discovery_refs", "gateway_discoveries")}
        if group_key in grouped:
            grouped[group_key]["seller_variants"].append(variant)
        else:
            entry["seller_variants"] = [variant]
            grouped[group_key] = entry
    entries = list(grouped.values())
    for index, entry in enumerate(entries, 1):
        entry["rank"] = index
    return {"request_revision": revision, "evaluated_at": evaluation_at,
            "gateway_discoveries": discoveries,
            "entries": entries, "alternatives": alternatives, "components": components, "signals": signals, "superseded": superseded,
            "missing_requirements": [{"candidate_id": e["candidate_id"], **r} for e in alternatives
                                     for r in e["requirements"] if r["status"] == "unknown"],
            "research": copy.deepcopy(state.get("research", {}))}
