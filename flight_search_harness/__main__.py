"""CLI for the single-ledger, agent-directed Flight Search Harness."""
from __future__ import annotations
import argparse
import json
import re
import sys
from .research import ResearchStore, read_json, now
from .suite_layout import SuiteLayout


def _money(value):
    return {k: value[k] for k in ("amount", "currency")} if value else None


def _itinerary(entry):
    lines = []
    for m in entry.get("movements", []):
        identity = m.get("flight_identity") if isinstance(m.get("flight_identity"), dict) else {}
        carriers = list(dict.fromkeys(v for v in (identity.get("marketing_carrier"), identity.get("operating_carrier"), m.get("operator")) if isinstance(v, str) and v))
        numbers = identity.get("flight_numbers", [])
        service = "/".join(numbers) if isinstance(numbers, list) and all(isinstance(n, str) for n in numbers) else ""
        service = service or m.get("service_id", "")
        lines.append(f"{m.get('from_node')}>{m.get('to_node')} {m.get('departure')}/{m.get('arrival')} {'/'.join(carriers) or m.get('mode', '')} {service}".strip())
    return lines


def _candidate(entry, observations):
    tickets = []
    for ref in entry["offer_refs"]:
        observation = observations.get(ref["observation_id"], {})
        offer = next((o for o in observation.get("offers", []) if o["offer_id"] == ref["offer_id"]), {})
        ticket = {k: v for k, v in offer.items() if k in {
            "price", "seller", "price_scope", "traveler_count", "verification_stage", "included_baggage", "checked_bag_count",
            "ticketing_structure", "restrictions", "ticket_context", "refundability", "change_policy", "membership_required",
            "cabin", "fare_brand", "fare_basis", "fare_type", "price_type", "mandatory_baggage_cost", "price_includes_mandatory_baggage",
            "requires_accommodation", "required_accommodation_cost", "price_includes_required_accommodation", "mandatory_costs_complete"} and v not in (None, [], {})}
        ticket.update(provider=observation.get("source", {}).get("provider"), captured_at=observation.get("captured_at"))
        ticket.setdefault("verification_stage", "unknown")
        tickets.append(ticket)
    checks = [r for r in entry["requirements"] if r["status"] != "satisfied"]
    repeated = {f"{r['requirement_id']}: {r['status']}. {r['reason']}" for r in checks}
    disclosures = []
    for line in entry["required_disclosures"]:
        if line in repeated:
            continue
        for ref in entry["offer_refs"]:
            line = line.removeprefix(f"Offer {ref['offer_id']}: ")
        if re.match(r"(?:missing|invalid): .*\b(?:page_url|booking_url|url):", line):
            continue
        if line not in disclosures:
            disclosures.append(line)
    result = {"candidate_id": entry["candidate_id"], "ranking_cost": _money(entry["ranking_cost"]),
              "dates": entry["dates"], "itinerary": _itinerary(entry), "tickets": tickets,
              "verification_status": entry["verification"]["overall_status"]}
    result.update({k: entry[k] for k in ("rank", "status", "stay_nights", "travel_duration_minutes", "distance_per_currency") if entry.get(k) is not None})
    if entry.get("outside_gateway_access"):
        result["mandatory_air_ticket_total"] = _money(entry["mandatory_air_ticket_total"])
        result["outside_gateway_access"] = [
            {k: _money(v) if k == "cost" else v for k, v in c.items()} for c in entry["outside_gateway_access"]]
    if entry["status"] == "partial_component":
        result.pop("ranking_cost")
        result["component_cost"] = _money(entry["component_cost"])
        result["dates"] = {"departure_date": entry["movements"][0]["departure"][:10] if entry["movements"] else None, "arrival_date": entry["final_arrival"][:10] if entry.get("final_arrival") else None}
    if checks:
        result["requirements"] = checks
    if disclosures:
        result["required_disclosures"] = disclosures
    # Other sellers are useful choices; omit the duplicate primary snapshot.
    if len(entry.get("seller_variants", [])) > 1:
        result["seller_variants"] = [_candidate({**entry, **v, "seller_variants": []}, observations) for v in entry["seller_variants"][1:]]
    return result


def _feedback(feedback, entries, *, delivered=False):
    best = feedback["current_best"]
    if best:
        entry = next((e for e in entries if e["candidate_id"] == best["candidate_id"]), {})
        best = {"candidate_id": best["candidate_id"]}
        if not delivered:
            best.update(ranking_cost=_money(entry.get("ranking_cost")), itinerary=_itinerary(entry))
            if entry.get("outside_gateway_access"):
                best["mandatory_air_ticket_total"] = _money(entry["mandatory_air_ticket_total"])
                best["outside_gateway_access"] = [{k: _money(v) if k == "cost" else v for k, v in c.items()} for c in entry["outside_gateway_access"]]
    challenges, counts = [], {}
    for c in feedback["challenges"]:
        r = c["result"]
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        line = f"{c['query_id']} r{c['request_revision']} {r['status']}"
        if r["status"] in {"pending", "unresolved", "technical_unavailable"}:
            reason = r["reason"]
            line += ": " + reason[:160] + ("…" if len(reason) > 160 else "")
        if c.get("baseline_selection"):
            line += f" ({c['baseline_selection']})"
        if r.get("comparison_basis"):
            line += f" ({r['comparison_basis']})"
        challenges.append(line)
    gaps = [{k: v for k, v in g.items() if k in {"kind", "side", "node_id", "query_id", "status"}}
            for g in feedback["search_gaps"] if g["kind"] != "challenge_unresolved"]
    result = {"current_best": best, "challenge_counts": counts, "challenges": challenges[-3:],
            "search_gap_count": len(gaps), "search_gaps": gaps[:6],
            "next_actions": list(dict.fromkeys(feedback["next_actions"]))[:6], "completion_scope": feedback["completion_scope"]}
    if feedback.get("candidate_issues"):
        result.update(candidate_issue_count=len(feedback["candidate_issues"]), candidate_issues=[
            {k: _money(v) if k in {"ranking_cost", "component_cost"} else v for k, v in issue.items()} for issue in feedback["candidate_issues"][:3]])
    return result


def compact_response(value, state):
    """Project CLI decisions without changing the complete Store/API records."""
    from .comparison import evaluate_session
    result = value.get("result", value)
    projection = result if "entries" in result else evaluate_session(state, evaluation_at=now())
    observations = {o["observation_id"]: o for o in state["observations"]}
    if "entries" in result:
        compact = {k: result[k] for k in ("run_id", "request_revision", "research", "claim_scope", "total_ranked", "unlinked_observation_ids") if result.get(k)}
        compact.update({k: state["requests"][-1]["request"]["intent"][k] for k in ("base_currency", "ranking_basis", "objective")})
        compact["eligibility_scope"] = "ticket_and_declared_constraints"
        for key in ("entries", "alternatives", "components"):
            compact[key] = [_candidate(e, observations) for e in result[key]]
        listed = [e for key in ("entries", "alternatives") for e in compact[key]]
        listed.extend(v for e in list(listed) for v in e.get("seller_variants", []))
        shared = [d for d in listed[0].get("required_disclosures", []) if len(listed) > 1 and all(d in e.get("required_disclosures", []) for e in listed[1:])] if listed else []
        if shared:
            compact["shared_required_disclosures"] = shared
            for e in listed:
                remaining = [d for d in e.get("required_disclosures", []) if d not in shared]
                if remaining:
                    e["required_disclosures"] = remaining
                else:
                    e.pop("required_disclosures", None)
        compact["signals"] = []
        for s in result["signals"]:
            raw = s["raw"] if isinstance(s["raw"], dict) else {}
            signal = {"observation_id": s["observation_id"], "provider": (s.get("source") or {}).get("provider"), "admission": s["admission"]["status"]}
            signal.update({k: raw[k] for k in ("observed_price", "scope", "verification_stage", "missing_facts") if raw.get(k)})
            signal["note"] = str(raw.get("source_text", ""))[:180] + ("…" if len(str(raw.get("source_text", ""))) > 180 else "")
            prices = [o["price"] for o in observations[s["observation_id"]].get("offers", []) if "price" in o]
            if prices:
                signal["prices"] = prices
            if s["admission"].get("issues"):
                signal["issues"] = s["admission"]["issues"]
            compact["signals"].append(signal)
        compact["superseded_count"] = len(result["superseded"])
        value = {"kind": value["kind"], "result": compact}
    else:
        compact = {k: v for k, v in value.items() if k in {"kind", "run_id", "request_revision", "research", "observation_count", "query_count", "candidate_id", "repeat_requires_reason", "saved", "duplicates", "query_errors", "query_warnings", "unlinked_observation_ids", "source_run", "recording"} and v not in (None, [], {})}
        if value.get("admissions"):
            compact["admissions"] = [{k: v for k, v in a.items() if v != []} for a in value["admissions"]]
        if value.get("query"):
            compact["query"] = {k: v for k, v in value["query"].items() if k in {"query_id", "status", "reason", "registration_warnings"}}
        if value.get("assessment"):
            compact["assessment"] = {k: value["assessment"][k] for k in ("candidate_id", "requirement_id", "status", "request_revision")}
        if "queries" in value:
            compact["queries"] = [{"query_id": q["query_id"], "status": q["status"], **q["query"]} for q in value["queries"]]
        if "recording" in value:
            recordings = value["recording"] if isinstance(value["recording"], list) else [value["recording"]]
            compact["recording"] = [{k: v for k, v in r.items() if k in {"saved", "duplicates", "admissions", "query_errors", "query_warnings"} and v} for r in recordings]
            if not isinstance(value["recording"], list):
                compact["recording"] = compact["recording"][0]
        value = compact
    if state["requests"][-1]["request"]["intent"]["ranking_basis"] == "mandatory_air_ticket_total":
        compact["cost_coverage"] = "tickets_internal_surface_and_outside_gateway_access"
    if "research_feedback" in result:
        compact["research_feedback"] = _feedback(result["research_feedback"], projection["entries"], delivered="entries" in result)
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", help="Defaults to external runtime_data_root/runs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("clock", help="Read the actual UTC clock when collecting source observations; no Run is created")
    start = sub.add_parser("start", help="Start a fresh Request and Run")
    start.add_argument("--request", required=True)
    start.add_argument("--run-id")
    start.add_argument("--verbose", action="store_true", help="Return complete records")
    for name in ("status", "resume", "stop", "query", "record", "combine", "assess", "revise", "deliver"):
        command = sub.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name != "deliver":
            command.add_argument("--verbose", action="store_true", help="Return complete records")
        if name in {"record", "combine", "assess"}:
            command.add_argument("--input", required=True)
        if name == "revise":
            command.add_argument("--request", required=True)
        if name == "stop":
            command.add_argument("--reason", required=True)
        if name == "query":
            command.add_argument("--query", required=True)
            command.add_argument("--retry-reason")
        if name == "deliver":
            command.add_argument("--view", choices=("ranked", "matrix", "full", "ticket-quotes", "presentation"))
            command.add_argument("--limit", type=int)
    legacy = sub.add_parser("import-legacy", help="Import observations from one identified Run, preserving its originals")
    legacy.add_argument("--source-run", required=True)
    legacy.add_argument("--request", required=True)
    legacy.add_argument("--run-id")
    legacy.add_argument("--verbose", action="store_true", help="Return complete records")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "clock":
            print(json.dumps({"utc_now": now(), "time_standard": "ISO 8601 UTC"}))
            return 0
        root = args.runs_root or SuiteLayout.discover(anchor=__file__).runtime_data_root() / "runs"
        store = ResearchStore(root)
        command = args.command
        if command == "start":
            value = store.start(read_json(args.request), args.run_id)
        elif command == "revise":
            value = store.revise(args.run_id, read_json(args.request))
        elif command == "query":
            value = store.query(args.run_id, read_json(args.query), args.retry_reason)
        elif command in {"record", "combine", "assess"}:
            value = getattr(store, command)(args.run_id, read_json(args.input))
        elif command == "deliver":
            view = "ranked" if args.view in {"ticket-quotes", "presentation"} else args.view
            value = store.deliver(args.run_id, view, args.limit)
        elif command == "status":
            value = store.summary(store.load(args.run_id))
        elif command == "resume":
            value = store.set_research(args.run_id, "active")
        elif command == "stop":
            value = store.set_research(args.run_id, "stopped", args.reason)
        else:
            value = store.import_legacy(args.source_run, read_json(args.request), args.run_id)
        detailed = getattr(args, "verbose", False) or "session" in value or "cells" in value.get("result", {})
        if not detailed:
            value = compact_response(value, store.load(value.get("run_id", args.run_id)))
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False, indent=2 if detailed else None,
                         separators=None if detailed else (",", ":"), allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        pointer = "; see workflow §1 (Request)" if args.command in {"start", "revise"} else ""
        print(json.dumps({"kind": "command_error", "message": str(exc) + pointer}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
