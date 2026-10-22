#!/usr/bin/env python3
"""Materialize an AI-owned round-trip date-pair research catalog.

This module is deliberately a research aide, not a search engine.  It turns
already observed date-pair signals and already admitted gateways into stable
candidate records.  It never invents dates, selects a winner, promotes a
calendar signal to a verified offer, or hides candidates behind a quota.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
SIGNAL_KINDS = {"joint_date_pair", "split_date_pair", "directional_date"}
ADMITTED_GATEWAY_STATUSES = {
    None,
    "accepted",
    "exact_anchor",
    "research_observed",
    "user_declared",
}


class RoundTripTemporalSchedulerError(ValueError):
    """Raised when the scheduler input contract is structurally invalid."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoundTripTemporalSchedulerError(f"{field} must be a non-empty string")
    return value.strip()


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise RoundTripTemporalSchedulerError(f"{field} must be an ISO date or date-time")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text).date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise RoundTripTemporalSchedulerError(
            f"{field} must be an ISO date or date-time"
        ) from exc


def _window(value: Any, field: str) -> tuple[date, date]:
    if not isinstance(value, dict):
        raise RoundTripTemporalSchedulerError(f"{field} must be an object")
    start = _date(value.get("start"), f"{field}.start")
    end = _date(value.get("end"), f"{field}.end")
    if end < start:
        raise RoundTripTemporalSchedulerError(f"{field}.end must not precede start")
    return start, end


def _money(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RoundTripTemporalSchedulerError(f"{field} must be a money object")
    amount = value.get("amount")
    currency = value.get("currency")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(float(amount))
        or float(amount) < 0
    ):
        raise RoundTripTemporalSchedulerError(f"{field}.amount must be a finite non-negative number")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise RoundTripTemporalSchedulerError(f"{field}.currency must be an ISO-style uppercase code")
    return {"amount": amount, "currency": currency}


def _intent(raw: dict[str, Any]) -> dict[str, Any]:
    scenario = raw.get("scenario")
    if isinstance(scenario, dict) and isinstance(scenario.get("intent"), dict):
        return scenario["intent"]
    if isinstance(raw.get("intent"), dict):
        return raw["intent"]
    return raw


def _gateway_block(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("accepted_gateways", raw.get("gateway_candidates"))
    if not isinstance(block, dict):
        raise RoundTripTemporalSchedulerError(
            "input must contain accepted_gateways or gateway_candidates"
        )
    return block


def _gateway_id(item: Any, field: str) -> tuple[str, str | None]:
    if isinstance(item, str):
        return _text(item, field), None
    if not isinstance(item, dict):
        raise RoundTripTemporalSchedulerError(f"{field} must be a gateway id or object")
    node = item.get("node") if isinstance(item.get("node"), dict) else item
    return _text(node.get("node_id"), f"{field}.node_id"), item.get("evidence_status")


def _accepted_gateways(raw: dict[str, Any]) -> tuple[set[str], set[str], dict[str, Any]]:
    block = _gateway_block(raw)
    result: dict[str, set[str]] = {"origin": set(), "destination": set()}
    ignored: dict[str, list[dict[str, str]]] = {"origin": [], "destination": []}
    for side in ("origin", "destination"):
        entries = block.get(side)
        if not isinstance(entries, list):
            raise RoundTripTemporalSchedulerError(f"gateways.{side} must be an array")
        for index, item in enumerate(entries):
            node_id, status = _gateway_id(item, f"gateways.{side}[{index}]")
            if status in ADMITTED_GATEWAY_STATUSES:
                result[side].add(node_id)
            else:
                ignored[side].append({"node_id": node_id, "reason": f"gateway_status_{status}"})
    if not result["origin"] or not result["destination"]:
        raise RoundTripTemporalSchedulerError(
            "at least one admitted origin and destination gateway is required"
        )
    return result["origin"], result["destination"], ignored


def _stay_constraints(raw: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    source = raw.get("stay_constraints", intent.get("stay_constraints", {}))
    if source is None:
        source = {}
    if not isinstance(source, dict):
        raise RoundTripTemporalSchedulerError("stay_constraints must be an object")
    exact = source.get("exact_nights", source.get("stay_nights"))
    minimum = source.get(
        "minimum_nights",
        source.get("minimum_stay_nights", intent.get("minimum_stay_nights", 0)),
    )
    maximum = source.get(
        "maximum_nights",
        source.get("maximum_stay_nights", intent.get("maximum_stay_nights")),
    )
    if exact is not None:
        minimum = maximum = exact
    for field, value in (("minimum_nights", minimum), ("maximum_nights", maximum)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise RoundTripTemporalSchedulerError(
                f"stay_constraints.{field} must be a non-negative integer"
            )
    if maximum is not None and maximum < minimum:
        raise RoundTripTemporalSchedulerError(
            "stay_constraints.maximum_nights must not be below minimum_nights"
        )
    return {
        "minimum_nights": minimum,
        "maximum_nights": maximum,
        "explicit": bool(source) or any(
            key in intent for key in ("minimum_stay_nights", "maximum_stay_nights")
        ),
    }


def _signal_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("temporal_signals", value.get("research_signals", value.get("signals")))
    if not isinstance(value, list):
        raise RoundTripTemporalSchedulerError("research_signals must be an array or wrapper object")
    if not all(isinstance(item, dict) for item in value):
        raise RoundTripTemporalSchedulerError("research_signals entries must be objects")
    return copy.deepcopy(value)


def _signal_id(signal: dict[str, Any]) -> str:
    value = signal.get("signal_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    canonical = json.dumps(signal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"signal-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def _gateway_pattern(signal: dict[str, Any]) -> dict[str, dict[str, str]]:
    origin = signal.get("origin_gateway_node_id", signal.get("origin_gateway_node"))
    destination = signal.get(
        "destination_gateway_node_id", signal.get("destination_gateway_node")
    )
    outbound_from = signal.get("outbound_origin_gateway_node_id", origin)
    outbound_to = signal.get("outbound_destination_gateway_node_id", destination)
    return_from = signal.get("return_origin_gateway_node_id", destination)
    return_to = signal.get("return_destination_gateway_node_id", origin)
    return {
        "outbound": {
            "from_gateway": _text(outbound_from, "signal.outbound origin gateway"),
            "to_gateway": _text(outbound_to, "signal.outbound destination gateway"),
        },
        "return": {
            "from_gateway": _text(return_from, "signal.return origin gateway"),
            "to_gateway": _text(return_to, "signal.return destination gateway"),
        },
    }


def _candidate_id(
    kind: str,
    gateways: dict[str, dict[str, str]],
    outbound_date: date,
    return_date: date,
) -> str:
    identity = {
        "signal_kind": kind,
        "gateway_pattern": gateways,
        "outbound_date": outbound_date.isoformat(),
        "return_date": return_date.isoformat(),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    readable = "-".join(
        (
            "joint" if kind == "joint_date_pair" else "split",
            gateways["outbound"]["from_gateway"],
            gateways["outbound"]["to_gateway"],
            outbound_date.isoformat(),
            return_date.isoformat(),
        )
    ).casefold()
    readable = re.sub(r"[^a-z0-9-]+", "-", readable).strip("-")[:62]
    return f"date-pair-{readable}-{digest}"


def _source_ref(signal: dict[str, Any], signal_id: str, kind: str) -> dict[str, Any]:
    ref = {
        "signal_id": signal_id,
        "signal_kind": kind,
        "evidence_status": "research_observed",
        "evidence_role": "research_only",
    }
    for field in ("source_id", "provider", "url", "observed_at", "observation_id"):
        if signal.get(field) is not None:
            ref[field] = copy.deepcopy(signal[field])
    return ref


def _price_observation(
    signal: dict[str, Any], signal_id: str, kind: str
) -> dict[str, Any]:
    if kind == "joint_date_pair":
        if signal.get("outbound_atomic_total") is not None or signal.get("return_atomic_total") is not None:
            raise RoundTripTemporalSchedulerError(
                "joint_date_pair cannot allocate its atomic total to directions"
            )
        total = _money(signal.get("atomic_total"), "signal.atomic_total")
        units = []
        if total is not None:
            units.append(
                {
                    "atomic_role": "joint_round_trip",
                    "atomic_total": total,
                    "indivisible": True,
                    "signal_id": signal_id,
                }
            )
        return {
            "signal_id": signal_id,
            "price_semantics_kind": "observed_joint_atomic_total",
            "atomic_units": units,
            "joint_total_indivisible": True,
            "directional_allocation": None,
            "split_optimistic_sum": None,
        }

    if signal.get("atomic_total") is not None:
        raise RoundTripTemporalSchedulerError(
            "split_date_pair requires directional atomic totals, not one combined atomic_total"
        )
    outbound = _money(signal.get("outbound_atomic_total"), "signal.outbound_atomic_total")
    returning = _money(signal.get("return_atomic_total"), "signal.return_atomic_total")
    units = []
    if outbound is not None:
        units.append(
            {
                "atomic_role": "outbound_one_way",
                "atomic_total": outbound,
                "indivisible": True,
                "signal_id": signal_id,
            }
        )
    if returning is not None:
        units.append(
            {
                "atomic_role": "return_one_way",
                "atomic_total": returning,
                "indivisible": True,
                "signal_id": signal_id,
            }
        )
    arithmetic = None
    if outbound is not None and returning is not None and outbound["currency"] == returning["currency"]:
        arithmetic = {
            "amount": round(float(outbound["amount"]) + float(returning["amount"]), 2),
            "currency": outbound["currency"],
            "role": "research_only_directional_sum",
            "verified_offer": False,
        }
    return {
        "signal_id": signal_id,
        "price_semantics_kind": "independent_directional_atomic_totals",
        "atomic_units": units,
        "joint_total_indivisible": False,
        "directional_allocation": "two_independent_atomic_one_way_signals",
        "split_optimistic_sum": arithmetic,
        "optimistic_sum_is_not_joint_atomic_total": True,
        "optimistic_sum_unavailable_reason": (
            "currency_mismatch_no_fx_inference"
            if outbound is not None
            and returning is not None
            and outbound["currency"] != returning["currency"]
            else None
        ),
    }


def _directional_record(signal: dict[str, Any], signal_id: str) -> dict[str, Any]:
    direction = signal.get("direction")
    if direction not in {"outbound", "return"}:
        raise RoundTripTemporalSchedulerError(
            "directional_date.direction must be outbound or return"
        )
    from_gateway = signal.get(
        "from_gateway_node_id",
        signal.get(
            "from_gateway", signal.get("from_node", signal.get("origin_gateway_node_id"))
        ),
    )
    to_gateway = signal.get(
        "to_gateway_node_id",
        signal.get(
            "to_gateway", signal.get("to_node", signal.get("destination_gateway_node_id"))
        ),
    )
    total = _money(signal.get("atomic_total"), "signal.atomic_total")
    if total is None:
        raise RoundTripTemporalSchedulerError(
            "directional_date from a priced task requires atomic_total"
        )
    record = {
        "signal_id": signal_id,
        "direction": direction,
        "from_gateway": _text(from_gateway, "signal.from_gateway_node_id"),
        "to_gateway": _text(to_gateway, "signal.to_gateway_node_id"),
        "departure_date": _date(signal.get("departure_date"), "signal.departure_date"),
        "atomic_total": total,
        "source_ref": _source_ref(signal, signal_id, "directional_date"),
    }
    if signal.get("destination_arrival_date") is not None:
        record["destination_arrival_date"] = _date(
            signal["destination_arrival_date"],
            "signal.destination_arrival_date",
        ).isoformat()
    return record


def _directional_pair_price_observation(
    outbound: dict[str, Any], returning: dict[str, Any]
) -> dict[str, Any]:
    outbound_total = outbound["atomic_total"]
    return_total = returning["atomic_total"]
    same_currency = outbound_total["currency"] == return_total["currency"]
    optimistic = None
    if same_currency:
        optimistic = {
            "amount": round(
                float(outbound_total["amount"]) + float(return_total["amount"]), 2
            ),
            "currency": outbound_total["currency"],
            "role": "research_only_directional_sum",
            "verified_offer": False,
        }
    pair_identity = f"{outbound['signal_id']}|{returning['signal_id']}"
    pair_id = f"directional-pair-{hashlib.sha256(pair_identity.encode('utf-8')).hexdigest()[:12]}"
    return {
        "signal_id": pair_id,
        "signal_ids": [outbound["signal_id"], returning["signal_id"]],
        "price_semantics_kind": "combined_independent_directional_date_signals",
        "atomic_units": [
            {
                "atomic_role": "outbound_one_way",
                "atomic_total": copy.deepcopy(outbound_total),
                "indivisible": True,
                "signal_id": outbound["signal_id"],
            },
            {
                "atomic_role": "return_one_way",
                "atomic_total": copy.deepcopy(return_total),
                "indivisible": True,
                "signal_id": returning["signal_id"],
            },
        ],
        "joint_total_indivisible": False,
        "directional_allocation": "two_independent_atomic_one_way_signals",
        "split_optimistic_sum": optimistic,
        "optimistic_sum_is_not_joint_atomic_total": True,
        "optimistic_sum_unavailable_reason": (
            None if same_currency else "currency_mismatch_no_fx_inference"
        ),
    }


def _constraint_assessment(
    signal: dict[str, Any],
    outbound: date,
    returning: date,
    outbound_window: tuple[date, date],
    return_window: tuple[date, date],
    stay: dict[str, Any],
    exact_outbound: set[str],
    exact_return: set[str],
) -> dict[str, Any]:
    rough_nights = (returning - outbound).days
    observed_arrival = None
    if signal.get("destination_arrival_date") is not None:
        observed_arrival = _date(
            signal["destination_arrival_date"], "signal.destination_arrival_date"
        )
    stay_nights = (returning - observed_arrival).days if observed_arrival else None
    checks = {
        "outbound_inside_window": outbound_window[0] <= outbound <= outbound_window[1],
        "return_inside_window": return_window[0] <= returning <= return_window[1],
        "return_not_before_outbound": returning >= outbound,
        "outbound_exact_date_satisfied": not exact_outbound or outbound.isoformat() in exact_outbound,
        "return_exact_date_satisfied": not exact_return or returning.isoformat() in exact_return,
    }
    if observed_arrival is not None:
        checks.update(
            {
                "destination_precedes_return": stay_nights >= 0,
                "minimum_stay_satisfied": stay_nights >= stay["minimum_nights"],
                "maximum_stay_satisfied": (
                    stay["maximum_nights"] is None
                    or stay_nights <= stay["maximum_nights"]
                ),
            }
        )
        stay_status = (
            "observed_timeline_compatible"
            if all(
                checks[key]
                for key in (
                    "destination_precedes_return",
                    "minimum_stay_satisfied",
                    "maximum_stay_satisfied",
                )
            )
            else "observed_timeline_incompatible"
        )
    else:
        checks.update(
            {
                "destination_precedes_return": None,
                "minimum_stay_satisfied": None,
                "maximum_stay_satisfied": None,
            }
        )
        stay_status = "requires_exact_timeline_verification"
    failed = [name for name, passed in checks.items() if passed is False]
    return {
        "hard_compatible": not failed,
        "checks": checks,
        "failed_checks": failed,
        "stay_status": stay_status,
        "stay_nights": stay_nights,
        "rough_departure_date_gap_nights": rough_nights,
        "rough_gap_is_non_binding": True,
        "stay_measurement": (
            "observed_destination_arrival_to_return_departure_date"
            if observed_arrival is not None
            else "departure_date_gap_advisory_only"
        ),
        "requires_exact_timeline_verification": observed_arrival is None,
        "stay_is_offer_timeline_verified": False,
    }


def _candidate_rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    observations = candidate["atomic_price_semantics"]["observations"]
    required_units = 1 if candidate["signal_kind"] == "joint_date_pair" else 2
    complete_price_signal = any(
        len(item["atomic_units"]) >= required_units for item in observations
    )
    return (
        not candidate["constraint_assessment"]["hard_compatible"],
        not complete_price_signal,
        0 if candidate["signal_kind"] == "joint_date_pair" else 1,
        -len(candidate["research_signal_refs"]),
        candidate["candidate_id"],
    )


def _add_candidate(
    candidates_by_id: dict[str, dict[str, Any]],
    *,
    kind: str,
    gateways: dict[str, dict[str, str]],
    outbound: date,
    returning: date,
    constraint_signal: dict[str, Any],
    source_refs: list[dict[str, Any]],
    price_observation: dict[str, Any],
    outbound_window: tuple[date, date],
    return_window: tuple[date, date],
    stay: dict[str, Any],
    exact_outbound: set[str],
    exact_return: set[str],
) -> None:
    candidate_id = _candidate_id(kind, gateways, outbound, returning)
    if candidate_id not in candidates_by_id:
        ticket_structure = (
            "joint_round_trip" if kind == "joint_date_pair" else "split_one_ways"
        )
        candidates_by_id[candidate_id] = {
            "candidate_id": candidate_id,
            "candidate_kind": "round_trip_date_pair_research",
            "signal_kind": kind,
            "ticket_structure": ticket_structure,
            "gateway_pattern": copy.deepcopy(gateways),
            "outbound_date": outbound.isoformat(),
            "return_date": returning.isoformat(),
            "constraint_assessment": _constraint_assessment(
                constraint_signal,
                outbound,
                returning,
                outbound_window,
                return_window,
                stay,
                exact_outbound,
                exact_return,
            ),
            "research_signal_refs": [],
            "atomic_price_semantics": {
                "observations": [],
                "consolidated_candidate_total": None,
                "verified_offer": False,
                "rule": (
                    "A joint total is one indivisible atomic signal and has no invented directional allocation."
                    if kind == "joint_date_pair"
                    else "Outbound and return totals remain two independent atomic one-way signals; any same-currency optimistic sum is research arithmetic only."
                ),
            },
            "admission_boundary": {
                "evidence_role": "research_only",
                "verified_observed": False,
                "payable_offer": False,
                "requires_specific_offer_verification": True,
            },
        }
    candidate = candidates_by_id[candidate_id]
    candidate["research_signal_refs"].extend(copy.deepcopy(source_refs))
    candidate["atomic_price_semantics"]["observations"].append(
        copy.deepcopy(price_observation)
    )


def _exact_date_sets(intent: dict[str, Any]) -> tuple[set[str], set[str]]:
    flexibility = intent.get("date_flexibility", {})
    if not isinstance(flexibility, dict):
        return set(), set()
    shared_mode = flexibility.get("mode", "fully_flexible")
    outbound_mode = flexibility.get("outbound_mode", shared_mode)
    return_mode = flexibility.get("return_mode", shared_mode)
    outbound = flexibility.get("outbound_exact_departure_dates", []) if outbound_mode == "exact_dates" else []
    returning = flexibility.get("return_exact_departure_dates", []) if return_mode == "exact_dates" else []
    return (
        {str(item) for item in outbound} if isinstance(outbound, list) else set(),
        {str(item) for item in returning} if isinstance(returning, list) else set(),
    )


def schedule_round_trip_date_pairs(
    raw_input: dict[str, Any],
    research_signals: Any | None = None,
    *,
    advisory_limit: int = 8,
) -> dict[str, Any]:
    """Return all materializable candidates plus non-binding AI research advice."""
    if not isinstance(raw_input, dict):
        raise RoundTripTemporalSchedulerError("input must be an object")
    if isinstance(advisory_limit, bool) or not isinstance(advisory_limit, int) or advisory_limit < 0:
        raise RoundTripTemporalSchedulerError("advisory_limit must be a non-negative integer")

    intent = _intent(raw_input)
    outbound_window = _window(
        intent.get("outbound_departure_window"), "intent.outbound_departure_window"
    )
    return_window = _window(
        intent.get("return_departure_window"), "intent.return_departure_window"
    )
    stay = _stay_constraints(raw_input, intent)
    exact_outbound, exact_return = _exact_date_sets(intent)
    origin_gateways, destination_gateways, ignored_gateways = _accepted_gateways(raw_input)
    signal_source = research_signals
    if signal_source is None:
        signal_source = raw_input.get("temporal_signals", raw_input.get("research_signals", raw_input.get("signals")))
    signals = _signal_items(signal_source)

    candidates_by_id: dict[str, dict[str, Any]] = {}
    unmaterialized: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    directional_records: list[dict[str, Any]] = []

    for index, signal in enumerate(signals):
        signal_id = _signal_id(signal)
        if signal_id in seen_signal_ids:
            unmaterialized.append(
                {"signal_id": signal_id, "reason": "duplicate_signal_id", "input_index": index}
            )
            continue
        seen_signal_ids.add(signal_id)
        kind = signal.get("signal_kind", signal.get("kind"))
        if kind not in SIGNAL_KINDS:
            unmaterialized.append(
                {"signal_id": signal_id, "reason": "unsupported_signal_kind", "input_index": index}
            )
            continue
        if signal.get("evidence_status") != "research_observed":
            reason = (
                "verified_offer_must_use_evidence_ingest_not_research_scheduler"
                if signal.get("evidence_status") == "verified_observed"
                else "signal_is_not_research_observed"
            )
            unmaterialized.append({"signal_id": signal_id, "reason": reason, "input_index": index})
            continue
        try:
            if kind == "directional_date":
                record = _directional_record(signal, signal_id)
                expected_from = (
                    origin_gateways if record["direction"] == "outbound" else destination_gateways
                )
                expected_to = (
                    destination_gateways if record["direction"] == "outbound" else origin_gateways
                )
                missing_roles = []
                if record["from_gateway"] not in expected_from:
                    missing_roles.append(f"{record['direction']}_from_gateway")
                if record["to_gateway"] not in expected_to:
                    missing_roles.append(f"{record['direction']}_to_gateway")
                if missing_roles:
                    unmaterialized.append(
                        {
                            "signal_id": signal_id,
                            "reason": "gateway_not_admitted",
                            "gateway_roles": missing_roles,
                            "input_index": index,
                        }
                    )
                    continue
                directional_records.append(record)
                continue

            gateways = _gateway_pattern(signal)
            required_gateway_membership = (
                (gateways["outbound"]["from_gateway"], origin_gateways, "outbound_origin"),
                (gateways["outbound"]["to_gateway"], destination_gateways, "outbound_destination"),
                (gateways["return"]["from_gateway"], destination_gateways, "return_origin"),
                (gateways["return"]["to_gateway"], origin_gateways, "return_destination"),
            )
            missing = [role for node_id, admitted, role in required_gateway_membership if node_id not in admitted]
            if missing:
                unmaterialized.append(
                    {
                        "signal_id": signal_id,
                        "reason": "gateway_not_admitted",
                        "gateway_roles": missing,
                        "input_index": index,
                    }
                )
                continue
            constraint_signal = signal
            if signal.get("temporal_signal_version") == "2.0":
                dates = signal.get("itinerary_dates")
                if not isinstance(dates, dict):
                    raise RoundTripTemporalSchedulerError(
                        "Temporal Signal v2 requires itinerary_dates"
                    )
                outbound = _date(
                    dates.get("outbound_departure_local_date"),
                    "signal.itinerary_dates.outbound_departure_local_date",
                )
                returning = _date(
                    dates.get("return_departure_local_date"),
                    "signal.itinerary_dates.return_departure_local_date",
                )
                constraint_signal = {
                    **signal,
                    "destination_arrival_date": dates.get(
                        "outbound_destination_arrival_local_date"
                    ),
                }
            else:
                outbound = _date(signal.get("outbound_date"), "signal.outbound_date")
                returning = _date(signal.get("return_date"), "signal.return_date")
            price_observation = _price_observation(signal, signal_id, kind)
            _add_candidate(
                candidates_by_id,
                kind=kind,
                gateways=gateways,
                outbound=outbound,
                returning=returning,
                constraint_signal=constraint_signal,
                source_refs=[_source_ref(signal, signal_id, kind)],
                price_observation=price_observation,
                outbound_window=outbound_window,
                return_window=return_window,
                stay=stay,
                exact_outbound=exact_outbound,
                exact_return=exact_return,
            )
        except RoundTripTemporalSchedulerError as exc:
            unmaterialized.append(
                {"signal_id": signal_id, "reason": "invalid_signal", "detail": str(exc), "input_index": index}
            )

    outbound_records = [
        item for item in directional_records if item["direction"] == "outbound"
    ]
    return_records = [
        item for item in directional_records if item["direction"] == "return"
    ]
    gateway_pairable_signal_ids: set[str] = set()
    directional_pair_observation_count = 0
    for outbound_record in outbound_records:
        for return_record in return_records:
            if (
                outbound_record["from_gateway"] != return_record["to_gateway"]
                or outbound_record["to_gateway"] != return_record["from_gateway"]
            ):
                continue
            gateway_pairable_signal_ids.update(
                (outbound_record["signal_id"], return_record["signal_id"])
            )
            gateways = {
                "outbound": {
                    "from_gateway": outbound_record["from_gateway"],
                    "to_gateway": outbound_record["to_gateway"],
                },
                "return": {
                    "from_gateway": return_record["from_gateway"],
                    "to_gateway": return_record["to_gateway"],
                },
            }
            _add_candidate(
                candidates_by_id,
                kind="split_date_pair",
                gateways=gateways,
                outbound=outbound_record["departure_date"],
                returning=return_record["departure_date"],
                constraint_signal=(
                    {
                        "destination_arrival_date": outbound_record[
                            "destination_arrival_date"
                        ]
                    }
                    if outbound_record.get("destination_arrival_date") is not None
                    else {}
                ),
                source_refs=[
                    outbound_record["source_ref"],
                    return_record["source_ref"],
                ],
                price_observation=_directional_pair_price_observation(
                    outbound_record, return_record
                ),
                outbound_window=outbound_window,
                return_window=return_window,
                stay=stay,
                exact_outbound=exact_outbound,
                exact_return=exact_return,
            )
            directional_pair_observation_count += 1

    unpaired_directional_signals = [
        {
            "signal_id": item["signal_id"],
            "direction": item["direction"],
            "reason": "no_reverse_gateway_directional_signal_supplied",
        }
        for item in directional_records
        if item["signal_id"] not in gateway_pairable_signal_ids
    ]

    materialized_candidates = list(candidates_by_id.values())
    for candidate in materialized_candidates:
        candidate["research_signal_refs"] = sorted(
            {
                item["signal_id"]: item
                for item in candidate["research_signal_refs"]
            }.values(),
            key=lambda item: item["signal_id"],
        )
        candidate["atomic_price_semantics"]["observations"].sort(
            key=lambda item: item["signal_id"]
        )
    incompatible_candidates = sorted(
        [
            item
            for item in materialized_candidates
            if not item["constraint_assessment"]["hard_compatible"]
        ],
        key=lambda item: item["candidate_id"],
    )
    candidates = [
        item
        for item in materialized_candidates
        if item["constraint_assessment"]["hard_compatible"]
    ]
    materialized_signal_ids = {
        ref["signal_id"]
        for item in materialized_candidates
        for ref in item["research_signal_refs"]
    }
    candidates.sort(key=lambda item: item["candidate_id"])
    ordered = sorted(candidates, key=_candidate_rank)
    ordered_ids = [item["candidate_id"] for item in ordered]

    suggested: list[str] = []
    if advisory_limit:
        # Preserve a joint baseline and a split challenger when both exist.
        for kind in ("joint_date_pair", "split_date_pair"):
            match = next((item for item in ordered if item["signal_kind"] == kind), None)
            if match is not None and len(suggested) < advisory_limit:
                suggested.append(match["candidate_id"])
        for item in ordered:
            if len(suggested) >= advisory_limit:
                break
            if item["candidate_id"] not in suggested:
                suggested.append(item["candidate_id"])

    return {
        "scheduler_version": VERSION,
        "decision_owner": "ai",
        "binding": False,
        "candidate_catalog": {
            "complete_for_materializable_supplied_observed_signals": True,
            "candidate_count": len(candidates),
            "candidates": candidates,
        },
        "advisory_order": {
            "decision_owner": "ai",
            "binding": False,
            "candidate_ids": ordered_ids,
            "basis": "constraint compatibility, signal completeness, joint-baseline visibility, evidence diversity, then stable id",
            "does_not_choose_for_ai": True,
        },
        "suggested_subset": {
            "decision_owner": "ai",
            "binding": False,
            "visibility_limit": advisory_limit,
            "candidate_ids": suggested,
            "outside_subset_candidate_ids": [item for item in ordered_ids if item not in suggested],
            "outside_subset_remains_available_to_ai": True,
            "limit_is_not_a_query_budget_or_search_stop": True,
        },
        "input_contract": {
            "outbound_departure_window": {
                "start": outbound_window[0].isoformat(),
                "end": outbound_window[1].isoformat(),
            },
            "return_departure_window": {
                "start": return_window[0].isoformat(),
                "end": return_window[1].isoformat(),
            },
            "stay_constraints": stay,
            "accepted_origin_gateway_ids": sorted(origin_gateways),
            "accepted_destination_gateway_ids": sorted(destination_gateways),
            "ignored_gateway_entries": ignored_gateways,
        },
        "coverage": {
            "supplied_signal_count": len(signals),
            "materialized_signal_count": len(materialized_signal_ids),
            "directional_signal_count": len(directional_records),
            "directional_pair_observation_count": directional_pair_observation_count,
            "unpaired_directional_signals": unpaired_directional_signals,
            "hard_incompatible_signal_groups": incompatible_candidates,
            "unmaterialized_signals": sorted(
                unmaterialized, key=lambda item: (item.get("input_index", 0), item["signal_id"])
            ),
            "world_or_window_enumerated": False,
            "claims_market_exhaustion": False,
            "candidate_pool_exhaustion_required": False,
            "calendar_signals_promoted_to_verified_offers": 0,
            "unsearched_scope_remains_unknown": True,
        },
        "ai_instruction": (
            "Inspect any catalog candidate, including candidates outside the suggested subset. "
            "Choose the next gateway/date/Provider question yourself, verify a concrete payable offer in the browser, "
            "and send verified offers through the evidence/session path rather than back through this scheduler."
        ),
    }


def _read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Session-shaped or compact scheduler input JSON")
    parser.add_argument("--signals", help="Optional separate research-signals JSON")
    parser.add_argument("--advisory-limit", type=int, default=8)
    parser.add_argument("--output", help="Write JSON here; stdout when omitted")
    args = parser.parse_args(argv)
    try:
        result = schedule_round_trip_date_pairs(
            _read(args.input),
            _read(args.signals) if args.signals else None,
            advisory_limit=args.advisory_limit,
        )
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (OSError, json.JSONDecodeError, RoundTripTemporalSchedulerError) as exc:
        print(f"round_trip_temporal_scheduler: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
