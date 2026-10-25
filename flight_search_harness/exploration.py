"""Evidence-linked challenger feedback over the existing Query and Offer ledger."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .comparison import comparison_key, discovered_gateways, evaluate_session


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
    revision = state["requests"][-1]["revision"]
    projection = evaluate_session(state, evaluation_at=instant)
    candidates = list(projection["entries"])
    # A seller variant may be grouped beneath the same itinerary's cheaper seller.
    for entry in projection["entries"]:
        for variant in entry.get("seller_variants", [])[1:]:
            candidates.append({**entry, **variant})
    observations = {o["observation_id"]: o for o in state["observations"]}
    for query in active:
        challenge = query["challenge"]
        if challenge["request_revision"] != revision:
            # A historical result remains historical; do not recompute it under new requirements.
            continue
        linked = [i for i in query["observation_ids"] if i not in challenge["observation_ids_before"] and i in observations]
        usable = [i for i in linked if observations[i]["admission"]["status"] in {"admitted", "signal"}]
        previous = challenge.get("result", {})
        if (previous.get("status") in {"improved", "not_improved", "no_result", "technical_unavailable"}
                and previous.get("observation_ids") == usable
                and previous.get("query_status") == query["status"]):
            # Subsequent price refreshes may replace a candidate, not the fact
            # that an earlier challenge succeeded under its recorded conditions.
            continue
        matches = [c for c in candidates if any(ref["observation_id"] in usable for ref in c["offer_refs"])]
        baseline = challenge["baseline"]
        matches = [c for c in matches if not baseline or c["candidate_id"] != baseline["candidate_id"]]
        result = {"status": "unresolved", "query_status": query["status"], "observation_ids": usable, "candidate_ids": [c["candidate_id"] for c in matches]}
        if baseline is None:
            result["reason"] = "No eligible baseline was fixed before this query. Findings remain available."
        elif query["status"] in {"failed", "unsupported"}:
            result.update(status="technical_unavailable", reason=query.get("reason", "The attempted method failed; no absence of cheaper options is established."))
        elif matches:
            best = min(matches, key=comparison_key)
            improved = tuple(Decimal(v) for v in _score(best)) < tuple(Decimal(v) for v in baseline["score"])
            result.update(status="improved" if improved else "not_improved", challenger=_snapshot(best, revision),
                          reason="Comparable observed candidate improves the fixed baseline." if improved else "Comparable observed candidates do not improve the fixed baseline.")
        elif query["status"] == "no_result" and usable:
            result.update(status="no_result", reason="The recorded source reported no result for this query scope; this is not a market-wide conclusion.")
        elif query["status"] == "running" and not linked:
            result.update(status="pending", reason="The query has no recorded result yet.")
        else:
            result["reason"] = "No comparable complete candidate is linked yet. A lead or unavailable query does not settle a price comparison."
        result["evaluated_at"] = instant
        challenge["result"] = result


def _flexible_sides(request: dict[str, Any]) -> list[str]:
    if request["journey_shape"] == "random_loop":
        return []
    intent = request["intent"]
    return [side for side in ("origin", "destination")
            if not intent.get(side + "_airports")
            and intent.get(side, {}).get("policy") != "exact"
            and any(n.get("node_type") in {"city_destination", "address", "station"}
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
                      *, evaluation_at: str) -> dict[str, Any]:
    """Expose gaps without inventing work, stopping the agent, or gating delivery."""
    projection = projection if projection is not None else evaluate_session(state, evaluation_at=evaluation_at)
    current = state["requests"][-1]
    revision, request = current["revision"], current["request"]
    entries = projection["entries"]
    best = _snapshot(entries[0], revision) if entries else None
    nodes = discovered_gateways(state)
    observations = {o["observation_id"]: o for o in state["observations"]}
    required = _flexible_sides(request)
    discovery_queries = [q for q in state["queries"] if q["query"].get("purpose") == "airport_discovery" and _same_endpoints(state, q, request)]
    evidenced_sides: set[str] = set()
    for query in discovery_queries:
        if query["status"] != "completed":
            continue
        for identifier in query["observation_ids"]:
            observation = observations.get(identifier, {})
            raw = observation.get("raw", {})
            raw = raw if isinstance(raw, dict) else {}
            discovery = raw.get("gateway_discovery")
            if (observation.get("admission", {}).get("status") == "signal"
                    and observation.get("source", {}).get("provider")
                    and raw.get("evidence_status") != "ai_proposed"
                    and isinstance(discovery, dict) and isinstance(discovery.get("airports"), list)
                    and isinstance(discovery.get("reason"), str) and discovery["reason"].strip()
                    and isinstance(discovery.get("side"), str) and discovery["side"] in {"origin", "destination"}):
                evidenced_sides.add(discovery.get("side"))
    missing_sides = [side for side in required if side not in evidenced_sides]
    opportunities = []
    for node in nodes:
        if not node.get("eligible_for_expansion") or node.get("decision") != "investigate":
            continue
        side, identifier = node["side"], node["node"]["node_id"]
        related = []
        for query in state["queries"]:
            if not _same_endpoints(state, query, request) or query["query"].get("purpose") == "airport_discovery":
                continue
            scope = query["query"]["scope"]
            values = scope.get(side + "_airports", scope.get("origins" if side == "origin" else "destinations", []))
            if isinstance(values, list) and identifier in values:
                related.append(query)
        observed = any(q["status"] in {"completed", "no_result"} and any(
            observations.get(i, {}).get("admission", {}).get("status") in {"admitted", "signal"}
            for i in q["observation_ids"]) for q in related)
        access_unknown = node.get("access", {}).get("status") == "unknown"
        if not observed or access_unknown:
            opportunities.append({"side": side, "node_id": identifier,
                                  "observation_id": node["observation_id"], "reason": node["reason"],
                                  "access": copy.deepcopy(node.get("access")),
                                  "status": "access_unverified" if observed and access_unknown else "method_unavailable" if related and all(q["status"] in {"failed", "unsupported"} for q in related) else "unsearched"})
    challenges = [{"query_id": q["query_id"], **copy.deepcopy(q["challenge"])}
                  for q in state["queries"] if q.get("challenge")]
    current_challenges = [c for c in challenges if c["request_revision"] == revision]
    gaps, actions = [], []
    if not best:
        actions.append("Find and record a usable dated journey to establish a comparison baseline.")
    for side in missing_sides:
        gaps.append({"kind": "airport_discovery", "side": side, "reason": "External discovery of reachable gateways has not been evidenced for this request version."})
        actions.append(f"Discover reachable {side} airports from external airport, route or transport information; do not infer an administrative boundary from the initial seeds.")
    if best and not current_challenges:
        gaps.append({"kind": "challenger", "reason": "No baseline-linked challenge has been recorded."})
        actions.append("Select a promising alternative date, reachable airport, seller or ticket combination and register why it could improve the current baseline.")
    for challenge in current_challenges:
        if challenge["result"]["status"] in {"pending", "unresolved", "technical_unavailable"}:
            gaps.append({"kind": "challenge_unresolved", "query_id": challenge["query_id"], "reason": challenge["result"]["reason"]})
    for opportunity in opportunities:
        gaps.append({"kind": "gateway_opportunity", **opportunity})
    if opportunities:
        actions.append("Price the promising discovered gateways, or record an evidence-based deferral/exclusion; unknown access costs are not zero.")
    if best and current_challenges and not actions:
        actions.append("Assess whether another useful direction can improve the current best; retain and present earlier eligible candidates. Stop only with the actual remaining scope explained.")
    return {
        "current_best": best, "challenges": challenges,
        "airport_discovery": {"required_sides": required, "evidenced_sides": sorted(x for x in evidenced_sides if isinstance(x, str)),
                              "missing_sides": missing_sides, "gateways": nodes},
        "open_opportunities": opportunities, "search_gaps": gaps, "next_actions": actions,
        "completion_scope": "open_questions_remain" if gaps else "bounded_observations_only",
    }
