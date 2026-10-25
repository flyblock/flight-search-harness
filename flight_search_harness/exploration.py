"""Evidence-linked challenger feedback over the existing Query and Offer ledger."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .comparison import comparison_key, discovered_gateways, evaluate_session, _airports, _convert, _node, _number, _window


REVIEW_CATEGORIES = ("core_city", "nearby_city", "surface_connected", "cross_border", "secondary_or_low_cost", "hub_positioning")


def _scope(entry: dict, observations: dict) -> dict:
    counts = {o.get("traveler_count") for ref in entry["offer_refs"] for o in observations[ref["observation_id"]]["offers"] if o["offer_id"] == ref["offer_id"]}
    origins, destinations = {_node(entry.get("origin_airport"))}, {_node(entry.get("destination_airport"))}
    if not entry.get("journey_complete", True) and entry["movements"]:
        origins, destinations = {_node(entry["movements"][0].get("from_node"))}, {_node(entry["movements"][-1].get("to_node"))}
    if entry.get("return_departure"):
        origins.add(_node(entry.get("return_airport")))
        destinations.update(_node(m.get("from_node")) for m in entry["movements"] if m.get("departure") == entry["return_departure"])
    return {"origin_airports": sorted(origins), "destination_airports": sorted(destinations),
            "outbound_date": entry.get("outbound_date"), "return_date": entry.get("return_date"),
            "adult_count": next(iter(counts)) if len(counts) == 1 else None}


def _gateways(request: dict, projection: dict) -> dict:
    if request["journey_shape"] == "random_loop":
        return {"origin": set(request["intent"]["departure_airports"]), "destination": set(request["intent"]["return_airports"])}
    pools = {side: _airports(request["intent"], side) for side in ("origin", "destination")}
    for d in projection["gateway_discoveries"]:
        if d["eligible_for_expansion"]:
            pools[d["side"]].add(d["node"]["node_id"])
    return pools


def _whole_scope(scope: dict, request: dict, projection: dict) -> bool:
    """Require explicit journey coverage before comparing a lead's price."""
    intent, shape = request["intent"], request["journey_shape"]
    pools = _gateways(request, projection)
    if shape == "random_loop":
        return scope.get("journey_shape") == shape and bool(scope.get("origin_airports")) and bool(scope.get("return_airports")) and all(
            isinstance(scope.get(key), list) and all(isinstance(v, str) and v in intent[pool] for v in scope[key])
            for key, pool in (("origin_airports", "departure_airports"), ("return_airports", "return_airports")))
    endpoints = all(isinstance(scope.get(side + "_airports"), list) and scope[side + "_airports"] and all(
        isinstance(v, str) and v in pools[side] for v in scope[side + "_airports"]) for side in pools)
    outbound = any(scope.get(k) for k in ("outbound_date", "outbound_date_window", "departure_window", "outbound_departure_window"))
    returning = any(scope.get(k) for k in ("return_date", "return_date_window", "return_departure_window", "trip_length_nights"))
    return bool(endpoints and outbound and (returning if shape == "round_trip" else not returning))


def _covers(actual: dict, wanted: dict) -> bool:
    keys = [k for k in ("origin_airports", "destination_airports", "outbound_date", "return_date", "adult_count", "outbound_date_window", "return_date_window", "trip_length_nights") if wanted.get(k) is not None]
    return ("origin_airports" in keys and "destination_airports" in keys and any(k.startswith("outbound_date") for k in keys)
            and all((sorted(wanted[k]) == sorted(actual.get(k, [])) if isinstance(wanted[k], list) and all(isinstance(v, str) for v in wanted[k]) and isinstance(actual.get(k), list)
                     else wanted[k] == actual.get(k)) for k in keys))


def _price_leads(projection: dict, observations: dict) -> list[tuple]:
    leads = []
    for s in projection["signals"]:
        raw = s["raw"] if isinstance(s["raw"], dict) else {}
        scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
        prices = [raw.get("observed_price"), *[o.get("price") for o in observations[s["observation_id"]]["offers"]]]
        leads.extend((s["observation_id"], p, scope, observations[s["observation_id"]]["received_at"]) for p in prices if isinstance(p, dict))
    for e in projection["alternatives"]:
        if price := e.get("ranking_cost") or e.get("gateway_complete_cost"):
            leads.append((e["candidate_id"], price,
                          _scope(e, observations), max(observations[r["observation_id"]]["received_at"] for r in e["offer_refs"])))
    return leads


def _pending_leads(state: dict, projection: dict, observations: dict, request: dict) -> list[tuple]:
    """Reuse follow-up closure for both fare and positioning budget nudges."""
    followed = {identifier for q in state["queries"] if q["status"] in {"completed", "no_result", "unsupported", "failed"}
                for identifier in (q["query"].get("follows_up") if isinstance(q["query"].get("follows_up"), list) else [])
                if isinstance(identifier, str) and identifier in observations}
    candidates = [*projection["entries"], *projection["alternatives"], *projection.get("components", []), *projection["superseded"]]
    used = {e["candidate_id"] for e in candidates} | {r["observation_id"] for e in candidates for r in e["offer_refs"]}
    refs = lambda e: {(r["observation_id"], r["offer_id"]) for r in e["offer_refs"]}
    completed = {e["candidate_id"] for e in projection["alternatives"] if any(refs(e) <= refs(whole) for whole in projection["entries"])}
    complete = [(_scope(e, observations), max(observations[r["observation_id"]]["received_at"] for r in e["offer_refs"])) for e in candidates if e.get("journey_complete", True)]
    pending = []
    for item in _price_leads(projection, observations):
        identifier, price, scope, recorded = item
        if identifier in followed or identifier in completed or (identifier in observations and identifier in used) or not _whole_scope(scope, request, projection) or any(instant > recorded and _covers(actual, scope) for actual, instant in complete):
            continue
        own_query = observations.get(identifier, {}).get("query_id")
        if any(q["query_id"] != own_query and q["status"] == "completed" and q.get("finished_at", "") > recorded
               and _covers(q["query"]["scope"], scope) for q in state["queries"]):
            continue
        pending.append(item)
    return pending


def _lead_access(identifier: str, scope: dict, projection: dict, request: dict) -> list[tuple]:
    """Missing endpoint prices for a lead; estimates remain separate from facts."""
    intent, shape = request["intent"], request["journey_shape"]
    if shape == "random_loop":
        return []
    candidate = next((e for e in projection["alternatives"] if e["candidate_id"] == identifier), {})
    endpoints = candidate.get("verification", {}).get("endpoints", [])
    best_endpoints = projection["entries"][0]["verification"]["endpoints"] if projection["entries"] else []
    discoveries = {(d["side"], d["node"]["node_id"]): d for d in projection["gateway_discoveries"]}
    missing = []
    for side in ("origin", "destination"):
        region = intent.get(side, {})
        anchors = {_node(n) for n in region.get("anchor_nodes", [])}
        directions = ("access", "egress") if shape == "round_trip" else ("access",) if side == "origin" else ("egress",)
        for direction in directions:
            endpoint = next((e for e in endpoints if e["role"] == side + "_" + direction), {})
            gateways = [endpoint["gateway_node_id"]] if endpoint else scope.get(side + "_airports", [])
            for gateway in gateways:
                outside = gateway not in _airports(intent, side)
                complete_cost = intent["ranking_basis"] == "complete_journey_cost"
                required = (region.get("endpoint_policy") == "anchor_required" or complete_cost and any(
                    e["role"] == side + "_" + direction and e["movement_ids"] for e in best_endpoints)) and (complete_cost or outside)
                if not required or gateway in anchors:
                    continue
                if candidate.get("ranking_cost") and (not complete_cost or endpoint.get("status") == "observed_connection"):
                    continue  # This candidate's ranking price already includes it.
                access = discoveries.get((side, gateway), {}).get("access", {})
                basis = access.get("basis", "unknown")
                cost = _convert(access.get("cost"), intent["base_currency"], intent) if basis in {"observed", "estimated"} else None
                # Reuse an explicitly priced path, without searching for paths or
                # allocating a joint fare to invented endpoint prices.
                recorded = [e for e in projection["components"] if e.get("component_cost") and e["movements"]
                            and e["disclosed_mandatory_costs_known"] and not any(r["status"] == "violated" for r in e["requirements"])
                            and (_node(e["movements"][0]["from_node"]) in anchors and _node(e["movements"][-1]["to_node"]) == gateway if direction == "access"
                                 else _node(e["movements"][0]["from_node"]) == gateway and _node(e["movements"][-1]["to_node"]) in anchors)]
                if recorded:
                    cost = min(_number(e["component_cost"]["unrounded_amount"]) for e in recorded)
                    basis = "observed"
                # A flexible airport list is not a purchase covering all airports.
                if len(gateways) != 1 or candidate.get("ranking_cost") and endpoint.get("movement_ids"):
                    cost, basis = None, "unknown"
                missing.append((side, gateway, direction, cost, basis))
    return missing


def _cheaper_leads(state: dict, projection: dict, observations: dict, request: dict, best: Decimal, skip_ids: set[str] | None = None) -> list[str]:
    intent = request["intent"]
    actions, unconverted = [], []
    for identifier, price, scope, _ in _pending_leads(state, projection, observations, request):
        if skip_ids and identifier in skip_ids:
            continue
        value = _convert(price, intent["base_currency"], intent)
        access = _lead_access(identifier, scope, projection, request)
        gateways = frozenset((side, gateway) for side, gateway, _, _, _ in access)
        if value is not None and value < best and access:
            extra = sum((cost for _, _, _, cost, _ in access if cost is not None), Decimal(0))
            total = value + extra
            labels = ", ".join(dict.fromkeys(f"{next(iter(intent.get(side, {}).get('anchor_nodes', [])), {}).get('node_id', side)}{'<->' if request['journey_shape'] == 'round_trip' else '->' if side == 'origin' else '<-'}{gateway}" for side, gateway, _, _, _ in access))
            costs = {basis: sum((cost for *_, cost, b in access if cost is not None and b == basis), Decimal(0))
                     for basis in ("observed", "estimated") if any(cost is not None and b == basis for *_, cost, b in access)}
            access_text = " + ".join(f"{'AI-estimated' if basis == 'estimated' else basis} access {cost:.2f}" for basis, cost in costs.items()) or "known access 0.00"
            if any(cost is None for _, _, _, cost, _ in access):
                if total >= best:
                    continue
                line = f"Positioning {labels} must cost < {best - total:.2f} {intent['base_currency']} (best minus fare {value:.2f} and {access_text}; {'return total; ' if request['journey_shape'] == 'round_trip' else ''}excluding unknown access; price screen only) [{identifier}]."
            elif total >= best:
                continue  # Known access already removes the advantage; nothing to act on.
            else:
                line = f"Via {labels}: fare {value:.2f} + {access_text} = {total:.2f} {intent['base_currency']} < best {best:.2f}; price screen only [{identifier}]."
            actions.append((total, line, gateways))
        elif value is not None and value < best:
            actions.append((value, f"Follow lead {identifier}: observed {value:.2f} {intent['base_currency']} < best {best:.2f}; verify total/scope.", gateways))
        elif value is None and _number(price.get("amount")) is not None and 0 <= _number(price["amount"]) < best and isinstance(price.get("currency"), str):
            unconverted.append(f"Lead {identifier}: FX to {intent['base_currency']} unknown; cheapness unjudged.")
    lines, seen = [], set()
    for _, line, gateways in sorted(actions):
        if gateways & seen:
            continue
        lines.append(line)
        seen.update(gateways)
    return list(dict.fromkeys(lines))[:3] + list(dict.fromkeys(unconverted))[:1]


def _budget_hints(state: dict, projection: dict, observations: dict, request: dict) -> tuple:
    """Subtract recorded prices only; no path search or feasibility inference."""
    intent, shape = request["intent"], request["journey_shape"]
    if not projection["entries"] or intent["ranking_basis"] != "mandatory_air_ticket_total" or shape not in {"one_way", "round_trip"}:
        return [], set(), set()
    best = projection["entries"][0]
    upper = _number(best["ranking_cost"]["unrounded_amount"])
    pools = _gateways(request, projection)
    labels = {side: next((n["node_id"] for n in intent.get(side, {}).get("anchor_nodes", [])), "/".join(sorted(pools[side]))) for side in pools}
    refs = lambda e: frozenset((r["observation_id"], r["offer_id"]) for r in e["offer_refs"])
    parts = [(e, _number(e["component_cost"]["unrounded_amount"]), _node(e["movements"][0]["from_node"]), _node(e["movements"][-1]["to_node"]), refs(e))
             for e in projection["components"] if e.get("component_cost") and e["movements"] and e["disclosed_mandatory_costs_known"]
             and not any(r["status"] == "violated" for r in e["requirements"])
             and all(m["mode"] in intent.get("allowed_modes", [m["mode"]]) for m in e["movements"])]
    parents = [refs(e) for e in [*projection["entries"], *projection["alternatives"], *state["combinations"]]]
    fresh = [p for p in parts if not any(p[4] <= r for r in parents) and not any(p[4] < q[4] for q in parts)]
    routes = {(_node(e["movements"][0]["from_node"]), _node(e["movements"][-1]["to_node"])) for e in projection["components"]
              if e["movements"] and not any(r["status"] == "violated" for r in e["requirements"])}
    hints, priced, positioned = {}, set(), set()

    def add(slack, line, key=None):
        key = key or line
        if slack > 0 and (key not in hints or (-slack, line) < (-hints[key][0], hints[key][1])):
            hints[key] = (slack, line)

    def direction(origins, destinations, cap, origin_label, destination_label, prefix="", other=None):
        available = [p for p in fresh if not other or not p[4] & other[4]]
        prefixes = [p for p in available if p[2] in origins and p[3] not in destinations]
        suffixes = [s for s in available if s[3] in destinations and s[2] not in origins]
        for p in prefixes:
            if not any(a == p[3] and b in destinations for a, b in routes):
                add(cap - p[1], f"{prefix}From {p[3]} reach {destination_label} for < {cap - p[1]:.2f} {intent['base_currency']} [{p[0]['candidate_id']}].", (prefix, "from", p[3]))
        for s in suffixes:
            if not any(a in origins and b == s[2] for a, b in routes):
                add(cap - s[1], f"{prefix}Reach {s[2]} from {origin_label} for < {cap - s[1]:.2f} {intent['base_currency']} [{s[0]['candidate_id']}].", (prefix, "reach", s[2]))
        for p in prefixes:
            for s in suffixes:
                if p[4] & s[4] or other and (p[4] | s[4]) & other[4]:
                    continue
                slack = cap - p[1] - s[1]
                if p[3] == s[2] and not any(p[4] | s[4] <= r for r in parents):
                    total = upper - slack
                    ids = ",".join(x[0]["candidate_id"] for x in ([p, s, other] if other else [p, s]))
                    add(slack, f"Combine {ids}: {total:.2f} vs best {upper:.2f} {intent['base_currency']}.")
                elif p[3] != s[2] and (p[3], s[2]) not in routes:
                    add(slack, f"{prefix}Connect {p[3]}->{s[2]} (any permitted mode) for < {slack:.2f} {intent['base_currency']} [{p[0]['candidate_id']},{s[0]['candidate_id']}].", (prefix, "connect", p[3], s[2]))

    if intent.get("self_transfer_policy", "allow") != "never":
        if shape == "one_way":
            direction(pools["origin"], pools["destination"], upper, labels["origin"], labels["destination"])
        else:
            for name, origin, destination in (("Outbound", "origin", "destination"), ("Return", "destination", "origin")):
                opposite = [p for p in parts if p[2] in pools[destination] and p[3] in pools[origin] and not p[4] <= refs(best)]
                if not opposite:
                    continue
                other = min(opposite, key=lambda p: (p[1], p[0]["candidate_id"]))
                cap = upper - other[1]
                candidates = [p for p in parts if p[2] in pools[origin] and p[3] in pools[destination] and not p[4] & other[4]]
                if not any(p[1] < cap for p in candidates):
                    add(cap, f"{name} one-way must cost < {cap:.2f} {intent['base_currency']} [{other[0]['candidate_id']} opposite].")
                for p in candidates:
                    if not any(p[4] | other[4] <= r for r in parents):
                        add(cap - p[1], f"Combine {p[0]['candidate_id']},{other[0]['candidate_id']}: {p[1] + other[1]:.2f} vs best {upper:.2f} {intent['base_currency']}.")
                direction(pools[origin], pools[destination], cap, labels[origin], labels[destination], name + ": ", other)
    # Positioning budgets subtract the core fare, never a ranking total that
    # already includes that same positioning cost. Unknown-access quotes remain leads.
    prices = [p for p in _price_leads(projection, observations) if p[0] in observations]
    prices.extend((e["candidate_id"], e["gateway_complete_cost"], _scope(e, observations), "")
                  for e in [*projection["entries"], *projection["alternatives"]] if e.get("gateway_complete_cost"))
    active = {p[0] for p in _pending_leads(state, projection, observations, request)} | {e["candidate_id"] for e in [*projection["entries"], *projection["alternatives"]]}
    for identifier, price, scope, _ in prices:
        value = _convert(price, intent["base_currency"], intent)
        if value is None or not _whole_scope(scope, request, projection):
            continue
        for side in pools:
            if intent.get(side, {}).get("endpoint_policy") == "any_admitted_gateway":
                continue
            for gateway in set(scope.get(side + "_airports", [])) - _airports(intent, side):
                priced.add((side, gateway))
                # Every outside lead belongs to its gateway budget, including
                # those displaced by a better fare or the compact hint cap.
                positioned.add(identifier)
                anchors = {n["node_id"] for n in intent.get(side, {}).get("anchor_nodes", [])}
                outward = any(a in anchors and b == gateway for a, b in routes)
                inward = any(a == gateway and b in anchors for a, b in routes)
                joint = any(p[2] in anchors and p[3] in anchors and any(_node(m["to_node"]) == gateway for m in p[0]["movements"]) for p in parts)
                filled = (outward and inward or joint) if shape == "round_trip" else (outward if side == "origin" else inward)
                if identifier not in active or identifier == best["candidate_id"] or filled:
                    continue
                span = "<->" if shape == "round_trip" else "->" if side == "origin" else "<-"
                add(upper - value, f"Positioning {labels[side]}{span}{gateway} must cost < {upper - value:.2f} {intent['base_currency']} ({'return total; ' if shape == 'round_trip' else ''}fare {value:.2f}) [{identifier}].", ("positioning", side, gateway))
    chosen = sorted(hints.values(), key=lambda h: (-h[0], h[1]))[:3]
    lines = [h[1] for h in chosen]
    if lines:
        lines[0] += " Check timing/visa."
    return lines, priced, positioned


def _structure_hint(state: dict, projection: dict, request: dict) -> list[str]:
    if not projection["entries"] or len(projection["entries"][0]["offer_refs"]) != 1:
        return []
    intent, shape = request["intent"], request["journey_shape"]
    tried = {q["query"]["scope"].get("ticketing_structure") for q in state["queries"] if q.get("challenge")
             and (q["observation_ids"] or q["status"] in {"completed", "no_result", "failed", "unsupported"})
             and isinstance(q["query"]["scope"].get("ticketing_structure"), str)}
    observations = {o["observation_id"]: o for o in state["observations"]}
    pools = _gateways(request, projection)
    pieces = [(_node(e["movements"][0]["from_node"]), _node(e["movements"][-1]["to_node"])) for e in projection["components"] if e["movements"]]
    if shape == "round_trip" and all(any(a in pools[o] and b in pools[d] for a, b in pieces) for o, d in (("origin", "destination"), ("destination", "origin"))):
        tried.add("separate_purchases")
    seeds = _airports(intent, "origin") | _airports(intent, "destination")
    if shape == "random_loop":
        seeds = pools["origin"] | pools["destination"]
    endpoints = seeds | {n["node_id"] for side in pools for n in intent.get(side, {}).get("anchor_nodes", [])}
    scopes = [scope for _, _, scope, _ in _price_leads(projection, observations)] + [
        _scope(e, observations) for e in [*projection["entries"], *projection["alternatives"], *projection["components"]]]
    if any(any(isinstance(v, str) and v and v not in endpoints for side in pools
               for v in (scope.get(side + "_airports", []) if isinstance(scope.get(side + "_airports"), list) else [])) for scope in scopes):
        tried.add("hub_positioning")
    for e in [*projection["entries"], *projection["alternatives"], *projection["components"], *projection["superseded"]]:
        if len(e["offer_refs"]) > 1:
            tried.add("separate_purchases")
            if len([m for m in e["movements"] if m.get("mode") == "flight"]) > (2 if shape == "round_trip" else 1):
                tried.add("hub_positioning")
        if shape == "round_trip" and e.get("return_departure") and (e.get("origin_airport") != e.get("return_airport") or any(
                m.get("departure") == e.get("return_departure") and _node(m.get("from_node")) != _node(e.get("destination_airport")) for m in e["movements"])):
            tried.add("open_jaw")
    permitted = []
    if intent.get("self_transfer_policy", "allow") != "never":
        permitted.append("separate_purchases")
        if intent.get("maximum_transfer_count") != 0 and not intent.get("airport_transit", {}).get("airside_only"):
            permitted.append("hub_positioning")
    if shape == "round_trip" and any(side in _flexible_sides(request) or len(intent.get(side + "_airports", intent.get(side, {}).get("seed_gateway_nodes", []))) > 1 for side in ("origin", "destination")):
        permitted.append("open_jaw")
    missing = [s for s in permitted if s not in tried]
    return ["Try permitted untried structures: " + ", ".join(missing) + "."] if missing else []


def _loop_efficiency_hint(projection: dict, request: dict) -> list[str]:
    """Surface efficient priced legs, without claiming a connectable loop."""
    if request["journey_shape"] != "random_loop":
        return []
    intent = request["intent"]
    best = projection["entries"][0] if projection["entries"] else None
    efficiency = _number(best["distance_per_currency"]) if best else None
    def signature(m):
        return (_node(m["from_node"]), _node(m["to_node"]), m.get("departure"), m.get("arrival"),
                tuple((m.get("flight_identity") or {}).get("flight_numbers", [])))
    used = {signature(m) for m in best["movements"] if m["mode"] == "flight"} if best else set()
    legs = {}
    for entry in projection["components"]:
        if (len(entry["movements"]) != 1 or entry["movements"][0]["mode"] != "flight" or not entry.get("component_cost")
                or not entry["disclosed_mandatory_costs_known"] or any(r["status"] == "violated" for r in entry["requirements"])):
            continue
        movement = entry["movements"][0]
        key = signature(movement)
        price = _number(entry["component_cost"]["unrounded_amount"])
        distance, duration = _number(entry.get("distance_km")), _number(entry.get("total_duration_minutes"))
        ceiling = _number(intent.get("quality_gate", {}).get("maximum_leg_price_in_base_currency"))
        budget = _convert(intent.get("budget"), intent["base_currency"], intent)
        if (key in used or price is None or price <= 0 or distance is None or duration is None or duration <= 0
                or "flight" not in intent.get("allowed_modes", ["flight"])
                or duration > intent.get("maximum_total_duration_minutes", duration)
                or ceiling is not None and price > ceiling or budget is not None and price > budget):
            continue
        value = distance / price
        if efficiency is not None and value <= efficiency:
            continue
        numbers = "/".join((movement.get("flight_identity") or {}).get("flight_numbers", [])) or movement.get("service_id", "")
        label = f"{key[0]}->{key[1]} {movement['departure'][:10]} {numbers} {value:.2f}"
        if key not in legs or value > legs[key][0]:
            legs[key] = (value, label)
    ordered = sorted(legs, key=lambda key: (-legs[key][0], key))
    if efficiency is not None:
        return [f"Legs above best {efficiency:.2f} km/{intent['base_currency']}: " + "; ".join(legs[key][1] for key in ordered[:3]) + "."] if ordered else []
    anchors, selected = [], set()
    for role, index, airports in (("departure", 0, intent["departure_airports"]), ("return", 1, intent["return_airports"])):
        window = intent.get("first_departure_window" if role == "departure" else "return_arrival_window", {})
        key = next((key for key in ordered if key[index] in airports
                    and _window(key[2 if role == "departure" else 3], window)
                    and (role != "departure" or key[2][11:16] >= intent.get("first_leg_earliest_local_time", {}).get(key[0], "00:00"))), None)
        if key is not None and key not in selected:
            anchors.append(role + ": " + legs[key][1])
            selected.add(key)
    return [f"Build a loop around recorded legs (km/{intent['base_currency']}): " + "; ".join(anchors) + ". Check connections."] if anchors else []


def _candidate_issues(projection: dict, intent: dict, focus_ids: list[str] | None) -> list[dict]:
    blocked = [*projection["alternatives"], *projection.get("components", [])]
    if projection["entries"]:
        blocked = [e for e in blocked if focus_ids and (e["candidate_id"] in focus_ids or any(r["observation_id"] in focus_ids for r in e["offer_refs"]))]
    issues = []
    for e in blocked:
        failed = {}
        for r in e["requirements"]:
            if r["status"] == "satisfied":
                continue
            key, reason = r["requirement_id"], r["reason"]
            metric = key.removeprefix("minimum_").removeprefix("maximum_")
            if r["status"] == "violated" and metric != key and e.get(metric) is not None and key in intent:
                reason = f"{metric} {e[metric]} {'<' if key.startswith('minimum_') else '>'} {intent[key]}"
            failed[key] = f"{r['status']}: {reason}"
        cost_key = "component_cost" if e["status"] == "partial_component" else "ranking_cost"
        issues.append({"candidate_id": e["candidate_id"], cost_key: copy.deepcopy(e[cost_key]), "failed_checks": failed})
    return issues


def _score(entry: dict[str, Any]) -> list[str]:
    """Exclude only the stable candidate-ID tie breaker from the ranking key."""
    return [str(int(v)) if isinstance(v, bool) else str(v) for v in comparison_key(entry)[:-1]]


def _snapshot(entry: dict[str, Any], revision: int) -> dict[str, Any]:
    return {
        "candidate_id": entry["candidate_id"], "offer_refs": copy.deepcopy(entry["offer_refs"]),
        "request_revision": revision, "objective": entry["objective"],
        "ranking_basis": entry["ranking_basis"], "ranking_cost": copy.deepcopy(entry.get("ranking_cost")),
        "score": _score(entry), "last_observed": entry.get("last_observed"),
        "verification_status": entry.get("verification", {}).get("overall_status", "unknown"),
        "verification_stages": [o.get("stage", "unknown") for o in entry.get("verification", {}).get("ticket_offers", [])],
    }


def attach_challenge(state: dict[str, Any], query: dict[str, Any], instant: str) -> None:
    """Freeze the comparison reference before this Query's findings are recorded."""
    spec = query["query"]
    if spec.get("purpose") != "challenge":
        return
    revision = state["requests"][-1]["revision"]
    entries = evaluate_session(state, evaluation_at=instant)["entries"]
    requested = spec.get("baseline_candidate_id")
    baseline = next((entry for entry in entries if entry["candidate_id"] == requested), None) if requested else (entries[0] if entries else None)
    query["challenge"] = {
        "reason": spec.get("reason", ""), "request_revision": revision,
        "baseline": _snapshot(baseline, revision) if baseline else None,
        "registered_at": instant,
        "observation_ids_before": list(query.get("observation_ids", [])),
        "result": {
            "status": "pending" if baseline else "unresolved",
            "reason": "Await actual findings for this query." if baseline else "No eligible baseline is available for the requested comparison.",
            "observation_ids": [], "candidate_ids": [],
        },
    }


def settle_challenges(state: dict[str, Any], instant: str) -> None:
    """Only actual linked observations and comparable candidates settle a challenge."""
    active = [q for q in state["queries"] if q.get("challenge")]
    if not active:
        return
    projections = {}
    observations = {o["observation_id"]: o for o in state["observations"]}
    for query in active:
        challenge = query["challenge"]
        linked = [i for i in query["observation_ids"] if i not in challenge["observation_ids_before"] and i in observations]
        usable = [i for i in linked if observations[i]["admission"]["status"] in {"admitted", "signal"}]
        previous = challenge.get("result", {})
        if (previous.get("status") in {"improved", "not_improved", "no_result", "technical_unavailable"}
                and previous.get("observation_ids") == usable
                and previous.get("query_status") == query["status"]):
            # Subsequent price refreshes may replace a candidate, not the fact
            # that an earlier challenge succeeded under its recorded conditions.
            continue
        number = challenge["request_revision"]
        if number not in projections:
            # Resolve unfinished comparisons in their own Request context. Later
            # revisions' findings must not be smuggled into a historical result.
            context = {**state, "observations": [o for o in state["observations"] if o.get("request_revision", 1) <= number]}
            known = {o["observation_id"] for o in context["observations"]}
            context["combinations"] = [c for c in state.get("combinations", []) if all(r["observation_id"] in known for r in c["offer_refs"])
                                       and c.get("created_at", "") <= (next((r["recorded_at"] for r in state["requests"] if r["revision"] > number), instant))]
            projections[number] = evaluate_session(context, evaluation_at=instant, request_revision=number)
        projection = projections[number]
        candidates = [variant for entry in projection["entries"]
                      for variant in [{**entry, **v} for v in entry.get("seller_variants", [])] or [entry]]
        matches = [c for c in candidates if any(ref["observation_id"] in usable for ref in c["offer_refs"])]
        baseline = challenge["baseline"]
        requested = query["query"].get("baseline_candidate_id")
        baselines = [c for c in candidates if not requested or c["candidate_id"] == requested]
        if baseline is None and baselines:
            # Prefer an independent comparator; if only one option exists, it
            # establishes a baseline but cannot serve as its own challenger.
            independent = [c for c in baselines if c not in matches]
            baseline = _snapshot(min(independent or baselines, key=comparison_key), number)
            challenge.update(baseline=baseline, baseline_selection="best_eligible_at_evaluation")
        matches = [c for c in matches if not baseline or c["candidate_id"] != baseline["candidate_id"]]
        result = {"status": "unresolved", "query_status": query["status"], "observation_ids": usable, "candidate_ids": [c["candidate_id"] for c in matches]}
        if query["status"] in {"failed", "unsupported"}:
            result.update(status="technical_unavailable", reason=query.get("reason", "The attempted method failed; no absence of cheaper options is established."))
        elif baseline and matches:
            best = min(matches, key=comparison_key)
            improved = tuple(Decimal(v) for v in _score(best)) < tuple(Decimal(v) for v in baseline["score"])
            result.update(status="improved" if improved else "not_improved", challenger=_snapshot(best, number),
                          reason="Comparable observed candidate improves the baseline." if improved else "Comparable observed candidates do not improve the baseline.")
        elif query["status"] == "no_result" and usable:
            result.update(status="no_result", reason="The recorded source reported no result for this query scope; this is not a market-wide conclusion.")
        elif query["status"] == "running" and not linked:
            result.update(status="pending", reason="The query has no recorded result yet.")
        else:
            linked_candidates = [c for c in projection["alternatives"] if any(r["observation_id"] in usable for r in c["offer_refs"])]
            linked_leads = [(identifier, price, scope) for identifier, price, scope, _ in _price_leads(projection, observations) if identifier in usable]
            context_request = next(r["request"] for r in state["requests"] if r["revision"] == number)
            lead_values = []
            for identifier, price, scope in linked_leads:
                value = _convert(price, context_request["intent"]["base_currency"], context_request["intent"])
                access = _lead_access(identifier, scope, projection, context_request)
                lead_values.append(value + sum((a[3] for a in access), Decimal(0)) if value is not None and all(a[3] is not None for a in access) else None)
            unknown_cost = [c for c in linked_candidates if c.get("ranking_cost") is None]
            blocked = unknown_cost or linked_candidates
            issues = list(dict.fromkeys(f"{r['requirement_id']}: {r['reason']}" for c in blocked for r in c["requirements"]
                                        if r["status"] != "satisfied" and r["requirement_id"] != "ranking_cost"))
            if blocked:
                result["reason"] = ("Linked candidate ranking cost is unknown. " if unknown_cost else "Linked candidates are not eligible. ") + "; ".join(issues)
                if any(r["requirement_id"] == "price_conversion" for c in unknown_cost for r in c["requirements"]):
                    result["reason"] += " Add an attributed FX rate via revise."
                result["candidate_ids"] = [c["candidate_id"] for c in linked_candidates]
            elif (baseline and baseline["objective"] == "price" and linked_leads and not any(r["observation_id"] in usable for c in projection.get("components", []) for r in c["offer_refs"])
                  and all(_whole_scope(scope, context_request, projection) for _, _, scope in linked_leads)
                  and all(v is not None and v >= _number(baseline["ranking_cost"]["unrounded_amount"]) for v in lead_values)):
                result.update(status="not_improved", comparison_basis="lead_prices",
                              reason="All linked journey-scope lead prices are at or above the baseline; based on leads, not bookable quotes.")
            else:
                if linked_leads:
                    result["reason"] = ("Linked prices cover components or unconfirmed journey scopes; no full-journey comparison." if not all(_whole_scope(scope, context_request, projection) for _, _, scope in linked_leads)
                                        else "A cheaper or unconverted journey-scope lead still needs a complete quote.")
                else:
                    result["reason"] = ("No eligible baseline is available yet." if baseline is None else "No distinct comparable challenger is linked; this query may only establish the baseline.")
        if challenge.get("baseline_selection") == "best_eligible_at_evaluation":
            result["reason"] += " Baseline selected at evaluation under the query's revision, not fixed before the query."
        result["evaluated_at"] = instant
        challenge["result"] = result


def _flexible_sides(request: dict[str, Any]) -> list[str]:
    if request["journey_shape"] == "random_loop":
        return []
    intent = request["intent"]
    return [side for side in ("origin", "destination")
            if not intent.get(side + "_airports")
            and intent.get(side, {}).get("policy") != "exact"
            and any(n.get("node_type") != "airport"
                    for n in intent.get(side, {}).get("anchor_nodes", []))]


def _same_endpoints(state: dict[str, Any], query: dict[str, Any], request: dict[str, Any]) -> bool:
    """FX or price preferences do not erase previously observed airport geography."""
    previous = next((r["request"] for r in state["requests"] if r["revision"] == query["request_revision"]), None)
    if previous is None:
        return False
    def endpoints(value):
        intent = value["intent"]
        return {side: {"airports": intent.get(side + "_airports"),
                       "policy": intent.get(side, {}).get("policy"),
                       "anchors": intent.get(side, {}).get("anchor_nodes", [])}
                for side in ("origin", "destination")}
    return endpoints(previous) == endpoints(request)


def research_feedback(state: dict[str, Any], projection: dict[str, Any] | None = None,
                      *, evaluation_at: str, focus_ids: list[str] | None = None) -> dict[str, Any]:
    """Expose gaps without inventing work, stopping the agent, or gating delivery."""
    settle_challenges(state, evaluation_at)
    projection = projection if projection is not None else evaluate_session(state, evaluation_at=evaluation_at)
    current = state["requests"][-1]
    revision, request = current["revision"], current["request"]
    entries = projection["entries"]
    best = _snapshot(entries[0], revision) if entries else None
    nodes = discovered_gateways(state)
    observations = {o["observation_id"]: o for o in state["observations"]}
    candidate_issues = _candidate_issues(projection, request["intent"], focus_ids)
    budget_lines, priced_gateways, positioned_ids = _budget_hints(state, projection, observations, request)
    required = _flexible_sides(request)
    queries = {q["query_id"]: q for q in state["queries"]}
    evidenced_sides: set[str] = set()
    reviewed = {side: set() for side in required}
    for observation in observations.values():
        raw = observation.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        discovery = raw.get("gateway_discovery")
        query = queries.get(observation.get("query_id"), {})
        if (observation["admission"]["status"] not in {"signal", "admitted"} or raw.get("evidence_status") == "ai_proposed"
                or query.get("status") in {"failed", "unsupported"} or not _same_endpoints(state, observation, request)
                or not isinstance(discovery, dict) or not isinstance(discovery.get("side"), str) or discovery["side"] not in reviewed):
            continue
        side = discovery["side"]
        # Any completed query can carry source-backed discovery; its purpose label is not the evidence.
        if (query.get("status") == "completed"
                and _same_endpoints(state, query, request) and isinstance(discovery.get("airports"), list)
                and isinstance(discovery.get("reason"), str) and discovery["reason"].strip()):
            evidenced_sides.add(side)
        categories = discovery.get("reviewed_categories", [])
        if isinstance(categories, list):
            reviewed[side].update(c for c in categories if isinstance(c, str) and c in REVIEW_CATEGORIES)
    missing_sides = [side for side in required if side not in evidenced_sides]
    opportunities, gateway_actions = [], []
    for node in nodes:
        if not node.get("eligible_for_expansion") or node.get("decision") in {"defer", "exclude"}:
            continue
        side, identifier = node["side"], node["node"]["node_id"]
        if any(identifier in _scope(e, observations)[side + "_airports"] for e in entries):
            continue
        if best and any(identifier in scope.get(side + "_airports", []) and _whole_scope(scope, request, projection)
                        and (value := _convert(price, request["intent"]["base_currency"], request["intent"])) is not None
                        and value >= _number(best["ranking_cost"]["unrounded_amount"])
                        for _, price, scope, _ in _price_leads(projection, observations)):
            continue
        opportunities.append({"side": side, "node_id": identifier, "observation_id": node["observation_id"],
                              "reason": node["reason"], "access": copy.deepcopy(node["access"]), "status": "no_comparable_candidate"})
        if (best and node["outside_seed_pool"] and request["intent"].get(side, {}).get("endpoint_policy") != "any_admitted_gateway"
                and (side, identifier) not in priced_gateways and request["intent"]["ranking_basis"] == "mandatory_air_ticket_total"):
            access = node["access"]
            cost = _convert(access.get("cost"), request["intent"]["base_currency"], request["intent"]) if access.get("basis") in ("observed", "estimated") else None
            if cost is None:
                gateway_actions.append((True, f"Via {identifier}: access cost/FX unknown; never assume zero."))
            else:
                basis = access["basis"]
                cost *= 2 if request["journey_shape"] == "round_trip" else 1
                cutoff = _number(best["ranking_cost"]["unrounded_amount"]) - cost
                gateway_actions.append((False, f"Via {identifier}: fares < {cutoff:.2f} {request['intent']['base_currency']} (best ranking cost minus {basis} access {cost:.2f}; price screen only)."))
    challenges = [{"query_id": q["query_id"], **copy.deepcopy(q["challenge"])}
                  for q in state["queries"] if q.get("challenge")]
    def comparison_context(value):
        return {"journey_shape": value["journey_shape"], "intent": {k: v for k, v in value["intent"].items() if k not in {"output", "search_limits"}}}
    revisions = {r["revision"]: comparison_context(r["request"]) for r in state["requests"]}
    current_challenges = [c for c in challenges if revisions.get(c["request_revision"]) == revisions[revision]]
    gaps, actions = [], []
    if not best:
        actions.append("No eligible baseline; inspect candidate_issues for failed checks." if candidate_issues else "Find and record a usable dated journey to establish a comparison baseline.")
    else:
        if candidate_issues and not budget_lines:
            actions.append("Inspect failed checks for the newly saved candidates.")
        actions.extend(_cheaper_leads(state, projection, observations, request, _number(best["ranking_cost"]["unrounded_amount"]), positioned_ids))
        actions.extend(budget_lines)
        if not current_challenges:
            actions.append("Challenge the current best: record a purpose=challenge query (other dates, airports, sellers or ticket structures).")
    actions.extend(_loop_efficiency_hint(projection, request))
    review_lines = []
    for side in required:
        missing = [c for c in REVIEW_CATEGORIES if c not in reviewed[side] and (c != "hub_positioning" or request["intent"].get("self_transfer_policy") != "never")]
        if side in missing_sides:
            actions.append(f"Discover reachable {side} airports from external sources ({', '.join(missing)}); borders are not limits." if missing
                           else f"Record the {side} airport discovery under a completed query.")
        elif missing:
            review_lines.append(f"Review {side} categories: " + ", ".join(missing) + ".")
    if (best and request["journey_shape"] in {"one_way", "round_trip"} and request["intent"].get("self_transfer_policy", "allow") != "never"
            and not projection.get("components") and not state.get("combinations")):
        # Budgets for unexpected routes need priced pieces; none exist yet.
        actions.append("Search from both ends: record cheap pieces (origin to anywhere, anywhere to destination, ground links) to get connection budgets.")
    actions.extend(line for _, line in sorted(gateway_actions))
    actions.extend(review_lines)
    actions.extend(_structure_hint(state, projection, request))
    sources = {(str(o["source"].get("provider", "")).strip().casefold(), str(p.get("seller", "")).strip().casefold())
               for o in observations.values() for p in o["offers"] if p["admission"]["status"] == "admitted"}
    if len(sources) == 1:
        actions.append("Cross-check another source; all complete offers use one source.")
    for side in missing_sides:
        gaps.append({"kind": "airport_discovery", "side": side, "reason": "External discovery of reachable gateways has not been evidenced for this request version."})
    if best and not current_challenges:
        gaps.append({"kind": "challenger", "reason": "No baseline-linked challenge has been recorded."})
    for challenge in current_challenges:
        if challenge["result"]["status"] in {"pending", "unresolved", "technical_unavailable"}:
            gaps.append({"kind": "challenge_unresolved", "query_id": challenge["query_id"], "reason": challenge["result"]["reason"]})
    for opportunity in opportunities:
        gaps.append({"kind": "gateway_opportunity", **opportunity})
    if best and current_challenges and not actions:
        actions.append("Assess whether another useful direction can improve the current best; retain and present earlier eligible candidates. Stop only with the actual remaining scope explained.")
    return {
        "current_best": best, "challenges": challenges, "candidate_issues": candidate_issues,
        "airport_discovery": {"required_sides": required, "evidenced_sides": sorted(x for x in evidenced_sides if isinstance(x, str)),
                              "missing_sides": missing_sides, "reviewed_categories": {s: sorted(v) for s, v in reviewed.items()}, "gateways": nodes},
        "open_opportunities": opportunities, "search_gaps": gaps, "next_actions": list(dict.fromkeys(actions)),
        "completion_scope": "open_questions_remain" if gaps else "bounded_observations_only",
    }
