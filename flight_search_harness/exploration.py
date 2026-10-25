"""Evidence-linked challenger feedback over the existing Query and Offer ledger."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .comparison import comparison_key, discovered_gateways, evaluate_session, _airports, _convert, _node, _number


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


def _whole_scope(scope: dict, request: dict, projection: dict) -> bool:
    """Require explicit journey coverage before comparing a lead's price."""
    intent, shape = request["intent"], request["journey_shape"]
    pools = {side: _airports(intent, side) for side in ("origin", "destination")}
    for d in projection["gateway_discoveries"]:
        if d["eligible_for_expansion"]:
            pools[d["side"]].add(d["node"]["node_id"])
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
        if e.get("ranking_cost"):
            leads.append((e["candidate_id"], e["ranking_cost"],
                          _scope(e, observations), max(observations[r["observation_id"]]["received_at"] for r in e["offer_refs"])))
    return leads


def _cheaper_leads(state: dict, projection: dict, observations: dict, request: dict, best: Decimal) -> list[str]:
    """Follow up once; neither a partial price nor a completed follow-up is a gap."""
    intent = request["intent"]
    candidates = [*projection["entries"], *projection["alternatives"], *projection.get("components", []), *projection["superseded"]]
    used = {e["candidate_id"] for e in candidates} | {r["observation_id"] for e in candidates for r in e["offer_refs"]}
    complete = [(_scope(e, observations), max(observations[r["observation_id"]]["received_at"] for r in e["offer_refs"])) for e in candidates if e.get("journey_complete", True)]
    actions, unconverted = [], []
    for identifier, price, scope, recorded in _price_leads(projection, observations):
        if identifier in used or not _whole_scope(scope, request, projection) or any(instant > recorded and _covers(actual, scope) for actual, instant in complete):
            continue
        own_query = observations.get(identifier, {}).get("query_id")
        if any(q["query_id"] != own_query and q["status"] == "completed" and q.get("finished_at", "") > recorded
               and _covers(q["query"]["scope"], scope) for q in state["queries"]):
            continue
        value = _convert(price, intent["base_currency"], intent)
        if value is not None and value < best:
            actions.append((value, f"Follow lead {identifier}: observed {value:.2f} {intent['base_currency']} < best {best:.2f}; verify total/scope."))
        elif value is None and _number(price.get("amount")) is not None and 0 <= _number(price["amount"]) < best and isinstance(price.get("currency"), str):
            unconverted.append(f"Lead {identifier}: FX to {intent['base_currency']} unknown; cheapness unjudged.")
    return list(dict.fromkeys(line for _, line in sorted(actions))) + list(dict.fromkeys(unconverted))[:1]


def _structure_hint(state: dict, projection: dict, request: dict) -> list[str]:
    if not projection["entries"] or len(projection["entries"][0]["offer_refs"]) != 1:
        return []
    intent, shape = request["intent"], request["journey_shape"]
    tried = {q["query"]["scope"].get("ticketing_structure") for q in state["queries"] if q.get("challenge")
             and (q["observation_ids"] or q["status"] in {"completed", "no_result", "failed", "unsupported"})
             and isinstance(q["query"]["scope"].get("ticketing_structure"), str)}
    for e in [*projection["entries"], *projection["alternatives"], *projection["superseded"]]:
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
            linked_leads = [(price, scope) for identifier, price, scope, _ in _price_leads(projection, observations) if identifier in usable]
            context_request = next(r["request"] for r in state["requests"] if r["revision"] == number)
            lead_values = [_convert(price, context_request["intent"]["base_currency"], context_request["intent"]) for price, _ in linked_leads]
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
                  and all(_whole_scope(scope, context_request, projection) for _, scope in linked_leads)
                  and all(v is not None and v >= _number(baseline["ranking_cost"]["unrounded_amount"]) for v in lead_values)):
                result.update(status="not_improved", comparison_basis="lead_prices",
                              reason="All linked journey-scope lead prices are at or above the baseline; based on leads, not bookable quotes.")
            else:
                if linked_leads:
                    result["reason"] = ("Linked prices cover components or unconfirmed journey scopes; no full-journey comparison." if not all(_whole_scope(scope, context_request, projection) for _, scope in linked_leads)
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
        if (query.get("status") == "completed" and query["query"].get("purpose") == "airport_discovery"
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
        if best and request["intent"]["ranking_basis"] == "mandatory_air_ticket_total":
            access = node["access"]
            cost = _convert(access.get("cost"), request["intent"]["base_currency"], request["intent"])
            if cost is None:
                gateway_actions.append((True, f"Via {identifier}: access cost/FX unknown; never assume zero."))
            else:
                basis = access.get("basis") if access.get("basis") in ("observed", "estimated") else "basis unspecified"
                cutoff = _number(best["ranking_cost"]["unrounded_amount"]) - cost
                gateway_actions.append((False, f"Via {identifier}: fares < {cutoff:.2f} {request['intent']['base_currency']} (best ticket minus {basis} access {cost:.2f}; price screen only)."))
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
        if candidate_issues:
            actions.append("Inspect failed checks for the newly saved candidates.")
        actions.extend(_cheaper_leads(state, projection, observations, request, _number(best["ranking_cost"]["unrounded_amount"])))
        if not current_challenges:
            actions.append("Challenge the current best: record a purpose=challenge query (other dates, airports, sellers or ticket structures).")
    review_lines = []
    for side in required:
        missing = [c for c in REVIEW_CATEGORIES if c not in reviewed[side] and (c != "hub_positioning" or request["intent"].get("self_transfer_policy") != "never")]
        if side in missing_sides:
            actions.append(f"Discover reachable {side} airports from external sources ({', '.join(missing)}); borders are not limits.")
        elif missing:
            review_lines.append(f"Review {side} categories: " + ", ".join(missing) + ".")
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
