"""Product-level executable specifications for the round-trip Product Kernel."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path
from typing import Any

from flight_search_harness.adapters.round_trip import RoundTripProductAdapter
from flight_search_harness.result_projection import summarize


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT
    / "product"
    / "round-trip"
    / "scripts"
)


def load_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_module("test_round_trip_materializer", "round_trip_intent_materializer.py")
SESSION = load_module("test_round_trip_session", "round_trip_session.py")
GENERATOR = load_module("test_round_trip_generator", "round_trip_hypothesis_generator.py")
EVIDENCE_ADAPTER = load_module(
    "test_round_trip_evidence_adapter", "round_trip_evidence_adapter.py"
)
BROWSER_BRIEF = load_module(
    "test_round_trip_browser_task_brief", "round_trip_browser_task_brief.py"
)
TEMPORAL_SCHEDULER = load_module(
    "test_round_trip_temporal_scheduler", "round_trip_temporal_scheduler.py"
)


def node(node_id: str, *, node_type: str, location: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "location": location,
        "country_or_region": "Testland",
        "country_code": "XT",
        "timezone": "Etc/UTC",
        "utc_offset": "+00:00",
    }


def side(
    side_id: str,
    anchor: dict[str, Any],
    *,
    policy: str,
    seed_gateway: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "region_id": side_id,
        "label": side_id.title(),
        "country_codes": ["XT"],
        "policy": policy,
        "endpoint_policy": "anchor_required",
        "anchor_nodes": [anchor],
        "max_ground_cost": None,
    }
    if seed_gateway is not None:
        value["seed_gateway_nodes"] = [seed_gateway]
    return value


def intent(
    *,
    regional: bool = False,
    required_baggage_items: list[str] | None = None,
    paid_baggage_allowed: bool = False,
    maximum_outbound_duration_minutes: int | None = None,
    maximum_return_duration_minutes: int | None = None,
) -> dict[str, Any]:
    origin_airport = node("AAA", node_type="airport", location="Alpha Airport")
    destination_airport = node("BBB", node_type="airport", location="Beta Airport")
    origin_anchor = (
        node("alpha-city", node_type="city_destination", location="Alpha City")
        if regional
        else origin_airport
    )
    destination_anchor = (
        node("beta-city", node_type="city_destination", location="Beta City")
        if regional
        else destination_airport
    )
    value = {
        "intent_id": "round-trip-contract-test",
        "trip_scope": "round_trip",
        "trip_mode": "fixed_destination",
        "currency": "XTS",
        "traveler_count": 1,
        "baggage_policy": {
            "required_items": list(required_baggage_items or []),
            "paid_baggage_allowed": paid_baggage_allowed,
        },
        "overnight_allowed": True,
        "self_transfer_policy": "allow",
        "origin": side(
            "origin-region",
            origin_anchor,
            policy="extended_region" if regional else "exact",
            seed_gateway=origin_airport if regional else None,
        ),
        "destination": side(
            "destination-region",
            destination_anchor,
            policy="extended_region" if regional else "exact",
            seed_gateway=destination_airport if regional else None,
        ),
        "outbound_departure_window": {
            "start": "2027-06-01T00:00:00+00:00",
            "end": "2027-06-03T23:59:00+00:00",
        },
        "return_departure_window": {
            "start": "2027-06-08T00:00:00+00:00",
            "end": "2027-06-10T23:59:00+00:00",
        },
    }
    if maximum_outbound_duration_minutes is not None:
        value["maximum_outbound_duration_minutes"] = (
            maximum_outbound_duration_minutes
        )
    if maximum_return_duration_minutes is not None:
        value["maximum_return_duration_minutes"] = maximum_return_duration_minutes
    return value


def initialize(
    *,
    regional: bool = False,
    required_baggage_items: list[str] | None = None,
    paid_baggage_allowed: bool = False,
    maximum_outbound_duration_minutes: int | None = None,
    maximum_return_duration_minutes: int | None = None,
) -> dict[str, Any]:
    return SESSION.validate_session(
        SESSION.initialize_session(
            MATERIALIZER.materialize_intent(
                intent(
                    regional=regional,
                    required_baggage_items=required_baggage_items,
                    paid_baggage_allowed=paid_baggage_allowed,
                    maximum_outbound_duration_minutes=(
                        maximum_outbound_duration_minutes
                    ),
                    maximum_return_duration_minutes=(
                        maximum_return_duration_minutes
                    ),
                )
            )
        )
    )


def evidence(
    bundle_id: str,
    task_id: str,
    status: str,
    *,
    coverage_complete: bool = True,
    nodes: list[dict[str, Any]] | None = None,
    gateway_candidates: list[dict[str, Any]] | None = None,
    ai_proposals: list[dict[str, Any]] | None = None,
    temporal_signals: list[dict[str, Any]] | None = None,
    provider_scopes: list[dict[str, Any]] | None = None,
    movements: list[dict[str, Any]] | None = None,
    offers: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_version": SESSION.SESSION_VERSION,
        "bundle_id": bundle_id,
        "task_id": task_id,
        "evidence_status": status,
        "coverage_complete": coverage_complete,
        "nodes": copy.deepcopy(nodes or []),
        "gateway_candidates": copy.deepcopy(gateway_candidates or []),
        "ai_proposals": copy.deepcopy(ai_proposals or []),
        "temporal_signals": copy.deepcopy(temporal_signals or []),
        "provider_scopes": copy.deepcopy(provider_scopes or []),
        "movements": copy.deepcopy(movements or []),
        "offers": copy.deepcopy(offers or []),
        "observations": copy.deepcopy(observations or []),
    }


def scope_proposal(side_name: str, gateway: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_version": SESSION.AI_EXPLORATION.VERSION,
        "side": side_name,
        "region_id": f"{side_name}-region",
        "candidates": [
            {
                "candidate_id": f"scope-{side_name}-{gateway['node_id']}",
                "node": gateway,
                "roles": ["primary_airport"],
                "inclusion_reason": "Declared seed is a plausible primary gateway.",
            }
        ],
        "excluded_seed_nodes": [],
        "scope_reason": "A bounded primary airport scope for the declared region.",
    }


def ranked_provider_scope(
    task: dict[str, Any],
    *,
    observation_id: str,
    origins: list[str] | None = None,
    destinations: list[str] | None = None,
    disposition: str = "winner",
    signal_ids: list[str] | None = None,
) -> dict[str, Any]:
    query = task["query_shape"]
    return {
        "observation_id": observation_id,
        "source_id": "kiwi-com",
        "observed_at": "2026-10-10T00:00:00+00:00",
        "price_selection_method": "price_sorted_api",
        "coverage_semantics": "provider_ranked_window",
        "scope_disposition": disposition,
        "origin_gateway_node_ids": list(
            origins or query["origin_gateway_node_ids"]
        ),
        "destination_gateway_node_ids": list(
            destinations or query["destination_gateway_node_ids"]
        ),
        "outbound_departure_window": {
            key: str(value)[:10]
            for key, value in query["outbound_departure_window"].items()
        },
        "return_departure_window": {
            key: str(value)[:10]
            for key, value in query["return_departure_window"].items()
        },
        "minimum_stay_nights": query.get("minimum_stay_nights"),
        "maximum_stay_nights": query.get("maximum_stay_nights"),
        "temporal_signal_ids": list(signal_ids or []),
    }


def complete_regional_side(
    state: dict[str, Any], side_name: str, gateway_id: str
) -> dict[str, Any]:
    scope_task_id = f"task-region-scope-{side_name}-g1"
    state = SESSION.record_hypothesis_decision(
        state,
        "select",
        task_id=scope_task_id,
        reason=f"Define {side_name} scope.",
    )
    gateway = next(
        item
        for item in state["scenario"]["nodes"]
        if item["node_id"] == gateway_id
    )
    state = SESSION.ingest_evidence(
        state,
        evidence(
            f"bundle-{side_name}-scope",
            scope_task_id,
            "ai_proposed",
            ai_proposals=[
                {
                    "proposal_type": "region_scope",
                    "proposal": scope_proposal(side_name, gateway),
                }
            ],
        ),
    )
    screen_task = next(
        (
            item
            for item in state["research_tasks"]
            if item.get("research_kind") == "region_pool_fare_screen"
            and item.get("requirement_status") == "open"
        ),
        None,
    )
    if screen_task is not None:
        signal_id = "screen-aaa-bbb-20270601-20270608"
        provider_scope = ranked_provider_scope(
            screen_task,
            observation_id="obs-region-pool-fare-screen",
            signal_ids=[signal_id],
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-region-pool-fare-screen",
                screen_task["task_id"],
                "research_observed",
                observations=[
                    {
                        "evidence_status": "research_observed",
                        "provider_scope": copy.deepcopy(provider_scope),
                    }
                ],
                provider_scopes=[provider_scope],
                temporal_signals=[
                    {
                        "signal_id": signal_id,
                        "signal_kind": "joint_date_pair",
                        "origin_gateway_node_id": "AAA",
                        "destination_gateway_node_id": "BBB",
                        "outbound_origin_gateway_node_id": "AAA",
                        "outbound_destination_gateway_node_id": "BBB",
                        "return_origin_gateway_node_id": "BBB",
                        "return_destination_gateway_node_id": "AAA",
                        "outbound_date": "2027-06-01",
                        "return_date": "2027-06-08",
                        "atomic_total": {"amount": 100, "currency": "XTS"},
                    }
                ],
            ),
        )
    return SESSION.record_hypothesis_decision(
        state,
        "complete",
        reason=f"{side_name} scope and gateway coverage are complete.",
    )


def movement(
    movement_id: str,
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    flight_number: str,
) -> dict[str, Any]:
    return {
        "movement_id": movement_id,
        "from_node": origin,
        "to_node": destination,
        "departure": departure,
        "arrival": arrival,
        "mode": "flight",
        "operator": "Example Air",
        "service_id": flight_number,
        "flight_identity": {
            "marketing_carrier": "EA",
            "flight_numbers": [flight_number],
        },
    }


def surface_movement(
    movement_id: str,
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    *,
    mode: str = "bus",
) -> dict[str, Any]:
    return {
        "movement_id": movement_id,
        "from_node": origin,
        "to_node": destination,
        "departure": departure,
        "arrival": arrival,
        "mode": mode,
        "operator": "Example Surface Operator",
        "service_id": movement_id,
    }


def offer(
    offer_id: str,
    scope: str,
    movement_ids: list[str],
    amount: float,
    *,
    direction: str | None = None,
    expires_at: str = "2099-01-01T00:00:00+00:00",
    mandatory_baggage_cost: float = 0,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "offer_id": offer_id,
        "provider_offer_id": f"provider-{offer_id}",
        "source_id": "example-air",
        "pricing_scope": "atomic_offer",
        "journey_scope": scope,
        "atomic": True,
        "movement_ids": movement_ids,
        "price": {"amount": amount, "currency": "XTS"},
        "mandatory_baggage_cost": {
            "amount": mandatory_baggage_cost,
            "currency": "XTS",
        },
        "included_baggage": [],
        "provider": "Example Air",
        "seller": "Example Air",
        "url": "https://example.invalid/offer",
        "observed_at": "2026-10-10T00:00:00+00:00",
        "expires_at": expires_at,
        "evidence_status": "verified_observed",
    }
    if direction is not None:
        value["direction"] = direction
    return value


def route_state(
    *,
    expires_at: str = "2099-01-01T00:00:00+00:00",
    temporal_first: bool = False,
    required_baggage_items: list[str] | None = None,
    paid_baggage_allowed: bool = False,
    mandatory_baggage_cost: float = 0,
    include_split: bool = False,
    maximum_outbound_duration_minutes: int | None = None,
    maximum_return_duration_minutes: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = initialize(
        required_baggage_items=required_baggage_items,
        paid_baggage_allowed=paid_baggage_allowed,
        maximum_outbound_duration_minutes=maximum_outbound_duration_minutes,
        maximum_return_duration_minutes=maximum_return_duration_minutes,
    )
    suggestion = GENERATOR.generate_suggestions(state)["suggestions"][0]
    if include_split:
        suggestion["advisory_basis"]["split_verification_indicated"] = True
    state = SESSION.validate_session(SESSION.add_route_hypothesis(state, suggestion))
    hypothesis_id = suggestion["hypothesis_id"]
    tasks = {
        task["journey_scope"] + (f":{task.get('direction')}" if task.get("direction") else ""):
        task["task_id"]
        for task in state["research_tasks"]
        if task["hypothesis_id"] == hypothesis_id
    }
    joint_task = tasks["joint_round_trip"]
    state = SESSION.record_hypothesis_decision(
        state, "select", task_id=joint_task, reason="Research the atomic joint baseline."
    )
    if temporal_first:
        temporal = evidence(
            "bundle-temporal-joint",
            joint_task,
            "research_observed",
            coverage_complete=False,
            temporal_signals=[
                {"signal_id": "signal-joint", "signal_kind": "joint_date_pair"}
            ],
        )
        state = SESSION.ingest_evidence(state, temporal)
        state = SESSION.record_temporal_selection(
            state,
            joint_task,
            {
                "candidate_id": "candidate-joint",
                "origin": "ai_proposed",
                "outbound_date": "2027-06-01",
                "return_date": "2027-06-08",
                "basis_signal_ids": ["signal-joint"],
            },
            "Verify the strongest observed joint date pair.",
        )
    outbound = movement(
        "movement-outbound",
        "AAA",
        "BBB",
        "2027-06-01T08:00:00+00:00",
        "2027-06-01T10:00:00+00:00",
        "EA101",
    )
    returning = movement(
        "movement-return",
        "BBB",
        "AAA",
        "2027-06-08T09:00:00+00:00",
        "2027-06-08T11:00:00+00:00",
        "EA102",
    )
    joint_evidence = evidence(
        "bundle-joint",
        joint_task,
        "verified_observed",
        movements=[outbound, returning],
        offers=[
            offer(
                "offer-joint",
                "joint_round_trip",
                ["movement-outbound", "movement-return"],
                150,
                expires_at=expires_at,
                mandatory_baggage_cost=mandatory_baggage_cost,
            )
        ],
    )
    state = SESSION.ingest_evidence(state, joint_evidence)
    if include_split:
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-outbound",
                tasks["one_way:outbound"],
                "verified_observed",
                movements=[outbound],
                offers=[
                    offer(
                        "offer-outbound",
                        "one_way",
                        ["movement-outbound"],
                        90,
                        direction="outbound",
                        expires_at=expires_at,
                        mandatory_baggage_cost=mandatory_baggage_cost,
                    )
                ],
            ),
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-return",
                tasks["one_way:return"],
                "verified_observed",
                movements=[returning],
                offers=[
                    offer(
                        "offer-return",
                        "one_way",
                        ["movement-return"],
                        90,
                        direction="return",
                        expires_at=expires_at,
                        mandatory_baggage_cost=mandatory_baggage_cost,
                    )
                ],
            ),
        )
    return SESSION.validate_session(state), joint_evidence


def complete_for_finalization(
    *,
    expires_at: str = "2099-01-01T00:00:00+00:00",
    include_split: bool = False,
) -> dict[str, Any]:
    state, _ = route_state(expires_at=expires_at, include_split=include_split)
    state = SESSION.record_hypothesis_decision(
        state, "complete", reason="Every required atomic question has terminal Evidence."
    )
    return SESSION.validate_session(state)


class RoundTripKernelTests(unittest.TestCase):
    def test_exact_anchors_are_admitted_to_temporal_scheduling(self) -> None:
        state = initialize()
        schedule = TEMPORAL_SCHEDULER.schedule_round_trip_date_pairs(
            state,
            [
                {
                    "signal_id": "signal-exact-anchor-pair",
                    "signal_kind": "joint_date_pair",
                    "evidence_status": "research_observed",
                    "origin_gateway_node_id": "AAA",
                    "destination_gateway_node_id": "BBB",
                    "outbound_origin_gateway_node_id": "AAA",
                    "outbound_destination_gateway_node_id": "BBB",
                    "return_origin_gateway_node_id": "BBB",
                    "return_destination_gateway_node_id": "AAA",
                    "outbound_date": "2027-06-01",
                    "return_date": "2027-06-08",
                    "atomic_total": {"amount": 125, "currency": "XTS"},
                }
            ],
        )

        self.assertEqual(schedule["candidate_catalog"]["candidate_count"], 1)
        self.assertEqual(
            schedule["input_contract"]["accepted_origin_gateway_ids"], ["AAA"]
        )
        self.assertEqual(
            schedule["input_contract"]["accepted_destination_gateway_ids"],
            ["BBB"],
        )

    def test_gateway_fare_aliases_normalize_and_ambiguous_prices_fail(self) -> None:
        observation = {
            "observation_version": EVIDENCE_ADAPTER.VERSION,
            "bundle_id": "bundle-gateway-fare",
            "task_id": "task-gateway-origin-aaa-g1",
            "observations": [
                {
                    "observation_id": "observation-gateway-fare",
                    "evidence_status": "research_observed",
                    "source_id": "google-flights",
                    "provider": "Google Flights",
                    "url": "https://www.google.com/travel/flights",
                    "observed_at": "2026-10-10T00:00:00+00:00",
                    "gateway_candidates": [
                        {
                            "candidate_id": "observed-origin-AAA",
                            "side": "origin",
                            "node": node(
                                "AAA",
                                node_type="airport",
                                location="Alpha Airport",
                            ),
                            "signal": {
                                "signal_kind": "dated_fare",
                                "lowest_displayed_total": {
                                    "amount": 125,
                                    "currency": "XTS",
                                },
                                "destination_gateway_node_id": "BBB",
                            },
                        }
                    ],
                }
            ],
        }

        adapted = EVIDENCE_ADAPTER.adapt_observations(
            observation, trusted_at="2026-10-10T01:00:00+00:00"
        )
        signal = adapted["gateway_candidates"][0]["signal"]
        self.assertEqual(signal["kind"], "dated_fare")
        self.assertEqual(
            signal["dated_fare_amount"], {"amount": 125.0, "currency": "XTS"}
        )
        self.assertEqual(signal["opposite_gateway_node_id"], "BBB")
        self.assertEqual(signal["date_coverage"], "sampled_date_pair")
        self.assertNotIn("signal_kind", signal)
        self.assertNotIn("lowest_displayed_total", signal)
        self.assertNotIn("destination_gateway_node_id", signal)

        destination_observation = copy.deepcopy(observation)
        destination_candidate = destination_observation["observations"][0][
            "gateway_candidates"
        ][0]
        destination_candidate["candidate_id"] = "observed-destination-BBB"
        destination_candidate["side"] = "destination"
        destination_candidate["node"] = node(
            "BBB", node_type="airport", location="Beta Airport"
        )
        destination_candidate["signal"].pop("destination_gateway_node_id")
        destination_candidate["signal"]["origin_gateway_node_id"] = "AAA"
        destination_signal = EVIDENCE_ADAPTER.adapt_observations(
            destination_observation,
            trusted_at="2026-10-10T01:00:00+00:00",
        )["gateway_candidates"][0]["signal"]
        self.assertEqual(destination_signal["opposite_gateway_node_id"], "AAA")
        self.assertNotIn("origin_gateway_node_id", destination_signal)

        missing_pair = copy.deepcopy(observation)
        del missing_pair["observations"][0]["gateway_candidates"][0]["signal"][
            "destination_gateway_node_id"
        ]
        with self.assertRaisesRegex(
            EVIDENCE_ADAPTER.EvidenceAdapterError,
            "opposite_gateway_node_id",
        ):
            EVIDENCE_ADAPTER.adapt_observations(
                missing_pair, trusted_at="2026-10-10T01:00:00+00:00"
            )

        conflicting_alias = copy.deepcopy(observation)
        conflicting_alias["observations"][0]["gateway_candidates"][0]["signal"][
            "dated_fare_amount"
        ] = {"amount": 130, "currency": "XTS"}
        with self.assertRaisesRegex(
            EVIDENCE_ADAPTER.EvidenceAdapterError, "conflicts"
        ):
            EVIDENCE_ADAPTER.adapt_observations(
                conflicting_alias, trusted_at="2026-10-10T01:00:00+00:00"
            )

        conflicting_pair = copy.deepcopy(observation)
        conflicting_pair["observations"][0]["gateway_candidates"][0]["signal"][
            "opposite_gateway_node_id"
        ] = "CCC"
        with self.assertRaisesRegex(
            EVIDENCE_ADAPTER.EvidenceAdapterError, "conflicts"
        ):
            EVIDENCE_ADAPTER.adapt_observations(
                conflicting_pair, trusted_at="2026-10-10T01:00:00+00:00"
            )

    def test_pool_screen_ranks_pair_and_dates_without_seed_priority(self) -> None:
        state = initialize()
        state["gateway_candidates"]["origin"].append(
            {
                "candidate_id": "observed-origin-ALT",
                "side": "origin",
                "node": node("ALT", node_type="airport", location="Alternate Origin"),
                "signal": {"kind": "region_pool_screen_covered"},
                "evidence_status": "research_observed",
            }
        )
        state["gateway_candidates"]["destination"].append(
            {
                "candidate_id": "observed-destination-DST",
                "side": "destination",
                "node": node(
                    "DST", node_type="airport", location="Alternate Destination"
                ),
                "signal": {"kind": "region_pool_screen_covered"},
                "evidence_status": "research_observed",
            }
        )
        state["research_tasks"].append(
            {
                "task_id": "task-region-pool-fare-screen-g1",
                "research_kind": "region_pool_fare_screen",
            }
        )
        state["scenario"]["fx_snapshot"] = {
            "rates": {"XTS": 1.0, "EUR": 2.0}
        }
        state["temporal_signals"] = [
            {
                "signal_id": "screen-seed-expensive",
                "signal_kind": "joint_date_pair",
                "research_task_id": "task-region-pool-fare-screen-g1",
                "origin_gateway_node_id": "AAA",
                "destination_gateway_node_id": "BBB",
                "outbound_origin_gateway_node_id": "AAA",
                "outbound_destination_gateway_node_id": "BBB",
                "return_origin_gateway_node_id": "DST",
                "return_destination_gateway_node_id": "ALT",
                "outbound_date": "2027-06-01",
                "return_date": "2027-06-08",
                "atomic_total": {"amount": 400, "currency": "EUR"},
            },
            {
                "signal_id": "screen-alt-cheap",
                "signal_kind": "joint_date_pair",
                "research_task_id": "task-region-pool-fare-screen-g1",
                "origin_gateway_node_id": "ALT",
                "destination_gateway_node_id": "DST",
                "outbound_origin_gateway_node_id": "ALT",
                "outbound_destination_gateway_node_id": "DST",
                "return_origin_gateway_node_id": "DST",
                "return_destination_gateway_node_id": "ALT",
                "outbound_date": "2027-06-02",
                "return_date": "2027-06-09",
                "atomic_total": {"amount": 600, "currency": "XTS"},
            },
        ]

        result = GENERATOR.generate_suggestions(
            state, max_closed_pairs=2, include_open_jaw=False
        )
        suggestions = result["suggestions"]
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(
            suggestions[0]["screen_candidate"]["signal_id"], "screen-alt-cheap"
        )
        self.assertEqual(
            suggestions[0]["screen_candidate"]["outbound_date"], "2027-06-02"
        )
        self.assertEqual(suggestions[0]["origin_gateway_nodes"], ["ALT"])
        self.assertEqual(suggestions[0]["destination_gateway_nodes"], ["DST"])
        self.assertEqual(suggestions[1]["route_shape"], "regional_open_jaw")
        self.assertEqual(suggestions[1]["origin_gateway_nodes"], ["AAA", "ALT"])
        self.assertEqual(
            suggestions[1]["destination_gateway_nodes"], ["BBB", "DST"]
        )
        state["result"]["best_portfolio"] = {
            "total_mandatory_cost": {"amount": 650, "currency": "XTS"}
        }
        remaining = GENERATOR.generate_suggestions(
            state, max_closed_pairs=2, include_open_jaw=False
        )["suggestions"]
        self.assertEqual(
            [item["screen_candidate"]["signal_id"] for item in remaining],
            ["screen-alt-cheap"],
        )
        state["result"]["best_portfolio"]["total_mandatory_cost"]["amount"] = 550
        self.assertEqual(
            GENERATOR.generate_suggestions(
                state, max_closed_pairs=2, include_open_jaw=False
            )["suggestions"],
            [],
        )

    def test_primary_destination_sample_keeps_flexible_gateway_work_open(self) -> None:
        state = initialize(regional=True)
        state = complete_regional_side(state, "origin", "AAA")
        destination_gateway = next(
            item for item in state["scenario"]["nodes"] if item["node_id"] == "BBB"
        )
        scope_task_id = "task-region-scope-destination-g1"
        state = SESSION.record_hypothesis_decision(
            state,
            "select",
            task_id=scope_task_id,
            reason="Define destination scope.",
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-destination-scope-window",
                scope_task_id,
                "ai_proposed",
                ai_proposals=[
                    {
                        "proposal_type": "region_scope",
                        "proposal": scope_proposal(
                            "destination", destination_gateway
                        ),
                    }
                ],
            ),
        )
        screen_task = next(
            item
            for item in state["research_tasks"]
            if item.get("research_kind") == "region_pool_fare_screen"
        )
        self.assertEqual(
            screen_task["query_shape"]["origin_gateway_node_ids"], ["AAA"]
        )
        self.assertEqual(
            screen_task["query_shape"]["destination_gateway_node_ids"], ["BBB"]
        )
        brief = BROWSER_BRIEF.task_to_brief(state, screen_task["task_id"])
        self.assertEqual(brief["research_kind"], "region_pool_fare_screen")
        self.assertEqual("kiwi-com", brief["source_options"][1]["source_id"])
        self.assertEqual(
            brief["evidence_contract"]["provider_scope"]["coverage_semantics"],
            "provider_ranked_window",
        )

        sampled = evidence(
            "bundle-region-pool-sampled",
            screen_task["task_id"],
            "research_observed",
            observations=[
                {
                    "evidence_status": "research_observed",
                    "date_coverage": "sampled_date_pair",
                }
            ],
            temporal_signals=[
                {
                    "signal_id": "screen-sampled-aaa-bbb",
                    "signal_kind": "joint_date_pair",
                    "origin_gateway_node_id": "AAA",
                    "destination_gateway_node_id": "BBB",
                    "outbound_origin_gateway_node_id": "AAA",
                    "outbound_destination_gateway_node_id": "BBB",
                    "return_origin_gateway_node_id": "BBB",
                    "return_destination_gateway_node_id": "AAA",
                    "outbound_date": "2027-06-01",
                    "return_date": "2027-06-08",
                    "atomic_total": {"amount": 125, "currency": "XTS"},
                }
            ],
        )
        sampled_state = SESSION.ingest_evidence(state, sampled)
        sampled_task = next(
            item
            for item in sampled_state["research_tasks"]
            if item["task_id"] == screen_task["task_id"]
        )
        self.assertEqual("open", sampled_task["requirement_status"])
        self.assertIn(
            "screen-sampled-aaa-bbb",
            {item["signal_id"] for item in sampled_state["temporal_signals"]},
        )

        completed_input = {
                "observation_version": SESSION.SESSION_VERSION,
                "bundle_id": "bundle-region-pool-bounded-window",
                "task_id": screen_task["task_id"],
                "coverage_complete": False,
                "observations": [
                    {
                        "observation_id": "obs-region-pool-bounded-window",
                        "source_id": "kiwi-com",
                        "evidence_status": "research_observed",
                        "provider": "Kiwi.com",
                        "url": "https://www.kiwi.com/",
                        "observed_at": "2026-10-10T00:00:00+00:00",
                        "price_selection_method": "price_sorted_api",
                        "provider_scope": {
                            "coverage_semantics": "provider_ranked_window",
                            "origin_gateway_node_ids": ["AAA"],
                            "destination_gateway_node_ids": ["BBB"],
                            "outbound_departure_window": {
                                "start": "2027-06-01",
                                "end": "2027-06-03",
                            },
                            "return_departure_window": {
                                "start": "2027-06-08",
                                "end": "2027-06-10",
                            },
                            "minimum_stay_nights": None,
                            "maximum_stay_nights": None,
                        },
                        "temporal_signals": copy.deepcopy(sampled["temporal_signals"]),
                    }
                ],
            }
        manual_grid = copy.deepcopy(completed_input)
        manual_grid["bundle_id"] = "bundle-region-pool-manual-grid"
        manual_grid["observations"][0].update(
            {
                "source_id": "google-flights",
                "provider": "Google Flights",
                "price_selection_method": "lowest_price_view",
            }
        )
        with self.assertRaisesRegex(
            EVIDENCE_ADAPTER.EvidenceAdapterError, "price_sorted_api"
        ):
            EVIDENCE_ADAPTER.adapt_observations(
                manual_grid,
                trusted_at="2026-10-10T00:01:00+00:00",
            )

        completed = EVIDENCE_ADAPTER.adapt_observations(
            completed_input,
            trusted_at="2026-10-10T00:01:00+00:00",
        )
        state = SESSION.validate_session(SESSION.ingest_evidence(state, completed))
        task = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == screen_task["task_id"]
        )
        self.assertEqual(task["requirement_status"], "satisfied")
        signal = next(
            item
            for item in state["temporal_signals"]
            if item["signal_id"] == "screen-sampled-aaa-bbb"
        )
        self.assertEqual(signal["outbound_date"], "2027-06-01")
        self.assertEqual(signal["return_date"], "2027-06-08")

    def test_pool_screen_completes_only_after_airport_partitions_exactly_cover(self) -> None:
        state = initialize(regional=True)
        alternate_origin = node(
            "AAX", node_type="airport", location="Alternate Alpha Airport"
        )
        alternate_destination = node(
            "BBX", node_type="airport", location="Alternate Beta Airport"
        )
        state["scenario"]["nodes"].extend(
            [alternate_origin, alternate_destination]
        )

        for side_name, primary_id, alternate in (
            ("origin", "AAA", alternate_origin),
            ("destination", "BBB", alternate_destination),
        ):
            task_id = f"task-region-scope-{side_name}-g1"
            state = SESSION.record_hypothesis_decision(
                state, "select", task_id=task_id, reason=f"Define {side_name} scope."
            )
            primary = next(
                item for item in state["scenario"]["nodes"] if item["node_id"] == primary_id
            )
            proposal = scope_proposal(side_name, primary)
            proposal["candidates"].append(
                {
                    "candidate_id": f"scope-{side_name}-{alternate['node_id']}",
                    "node": alternate,
                    "roles": ["secondary_airport"],
                    "inclusion_reason": "A bounded alternate airport for partition coverage.",
                }
            )
            state = SESSION.ingest_evidence(
                state,
                evidence(
                    f"bundle-{side_name}-scope-two-airports",
                    task_id,
                    "ai_proposed",
                    ai_proposals=[
                        {"proposal_type": "region_scope", "proposal": proposal}
                    ],
                ),
            )
            if side_name == "origin":
                state = SESSION.record_hypothesis_decision(
                    state,
                    "complete",
                    reason="Origin scope is defined before destination discovery.",
                )

        screen_task = next(
            item
            for item in state["research_tasks"]
            if item.get("research_kind") == "region_pool_fare_screen"
        )
        self.assertEqual(
            {"AAA", "AAX"}, set(screen_task["query_shape"]["origin_gateway_node_ids"])
        )
        self.assertEqual(
            {"BBB", "BBX"},
            set(screen_task["query_shape"]["destination_gateway_node_ids"]),
        )

        first_signal = {
            "signal_id": "screen-partition-aaa-bbb",
            "signal_kind": "joint_date_pair",
            "origin_gateway_node_id": "AAA",
            "destination_gateway_node_id": "BBB",
            "outbound_origin_gateway_node_id": "AAA",
            "outbound_destination_gateway_node_id": "BBB",
            "return_origin_gateway_node_id": "BBB",
            "return_destination_gateway_node_id": "AAA",
            "outbound_date": "2027-06-01",
            "return_date": "2027-06-08",
            "atomic_total": {"amount": 125, "currency": "XTS"},
        }
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-region-pool-partition-aaa",
                screen_task["task_id"],
                "research_observed",
                coverage_complete=True,
                observations=[{"evidence_status": "research_observed"}],
                temporal_signals=[first_signal],
                provider_scopes=[
                    ranked_provider_scope(
                        screen_task,
                        observation_id="obs-region-pool-partition-aaa",
                        origins=["AAA"],
                        destinations=["BBB", "BBX"],
                        signal_ids=[first_signal["signal_id"]],
                    )
                ],
            ),
        )
        pending_task = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == screen_task["task_id"]
        )
        self.assertEqual("open", pending_task["requirement_status"])
        self.assertEqual(2, len(SESSION.pool_screen_progress(state, screen_task["task_id"])["remaining_pairs"]))

        second_signal = {
            **copy.deepcopy(first_signal),
            "signal_id": "screen-partition-aax-bbx",
            "origin_gateway_node_id": "AAX",
            "destination_gateway_node_id": "BBX",
            "outbound_origin_gateway_node_id": "AAX",
            "outbound_destination_gateway_node_id": "BBX",
            "return_origin_gateway_node_id": "BBX",
            "return_destination_gateway_node_id": "AAX",
            "atomic_total": {"amount": 115, "currency": "XTS"},
        }
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-region-pool-partition-aax",
                screen_task["task_id"],
                "research_observed",
                observations=[{"evidence_status": "research_observed"}],
                temporal_signals=[second_signal],
                provider_scopes=[
                    ranked_provider_scope(
                        screen_task,
                        observation_id="obs-region-pool-partition-aax",
                        origins=["AAX"],
                        destinations=["BBB", "BBX"],
                        signal_ids=[second_signal["signal_id"]],
                    )
                ],
            ),
        )
        completed_task = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == screen_task["task_id"]
        )
        self.assertEqual("satisfied", completed_task["requirement_status"])
        self.assertTrue(
            SESSION.pool_screen_progress(state, screen_task["task_id"])[
                "coverage_complete"
            ]
        )

    def test_pool_scope_accepts_the_complete_stay_feasible_window_envelope(self) -> None:
        task = {
            "query_shape": {
                "origin_gateway_node_ids": ["AAA"],
                "destination_gateway_node_ids": ["BBB"],
                "outbound_departure_window": {
                    "start": "2026-12-16T00:00:00+00:00",
                    "end": "2027-01-10T23:59:00+00:00",
                },
                "return_departure_window": {
                    "start": "2026-12-16T00:00:00+00:00",
                    "end": "2027-01-10T23:59:00+00:00",
                },
                "minimum_stay_nights": 14,
                "maximum_stay_nights": 21,
            }
        }
        provider_scope = {
            "origin_gateway_node_ids": ["AAA"],
            "destination_gateway_node_ids": ["BBB"],
            "outbound_departure_window": {
                "start": "2026-12-16",
                "end": "2026-12-27",
            },
            "return_departure_window": {
                "start": "2026-12-30",
                "end": "2027-01-10",
            },
            "minimum_stay_nights": 14,
            "maximum_stay_nights": 21,
            "coverage_semantics": "provider_ranked_window",
            "price_selection_method": "price_sorted_api",
            "scope_disposition": "winner",
            "temporal_signal_ids": ["screen-winner"],
        }

        self.assertEqual(
            {("AAA", "BBB")}, SESSION._pool_scope_pairs(task, provider_scope)
        )
        provider_scope["outbound_departure_window"]["end"] = "2026-12-26"
        with self.assertRaisesRegex(
            SESSION.RoundTripSessionError, "complete feasible date windows"
        ):
            SESSION._pool_scope_pairs(task, provider_scope)

    def test_surface_egress_uses_exit_buffer_and_export_lists_movements(self) -> None:
        state, _ = route_state()
        hypothesis_id = next(
            hypothesis_id
            for hypothesis_id, node_value in state["hypothesis_tree"]["nodes"].items()
            if node_value.get("hypothesis_kind") == "complete_journey"
        )
        schedule = SESSION._exact_surface_schedule(state, hypothesis_id)
        self.assertEqual(
            schedule["origin-egress-return"]["not_before"],
            "2027-06-08T12:00:00+00:00",
        )
        self.assertEqual(
            ["2027-06-01", "2027-05-31"],
            schedule["origin-access-outbound"]["service_dates"],
        )
        self.assertEqual(
            ["2027-06-08", "2027-06-09"],
            schedule["origin-egress-return"]["service_dates"],
        )

        exported = SESSION.export_result(state, search_status="draft")
        self.assertEqual(
            [item["movement_id"] for item in exported["journey_movements"]],
            ["movement-outbound", "movement-return"],
        )
        self.assertEqual(
            [item["direction"] for item in exported["journey_movements"]],
            ["outbound", "return"],
        )
        summary = summarize(
            "round_trip", exported, full_result_ref="product/final.json"
        )
        self.assertEqual(summary["movements"], exported["journey_movements"])

    def test_time_infeasible_surface_evidence_reopens_same_requirement(self) -> None:
        state = initialize(regional=True)
        state = complete_regional_side(state, "origin", "AAA")
        state = complete_regional_side(state, "destination", "BBB")
        state = SESSION.validate_session(state)
        suggestion = GENERATOR.generate_suggestions(state)["suggestions"][0]
        state = SESSION.validate_session(
            SESSION.add_route_hypothesis(state, suggestion)
        )
        hypothesis_id = suggestion["hypothesis_id"]
        joint_task = next(
            task
            for task in state["research_tasks"]
            if task["hypothesis_id"] == hypothesis_id
            and task.get("journey_scope") == "joint_round_trip"
        )
        state = SESSION.record_hypothesis_decision(
            state,
            "select",
            task_id=joint_task["task_id"],
            reason="Verify the regional joint fare before airport access.",
        )
        outbound = movement(
            "movement-regional-outbound",
            "AAA",
            "BBB",
            "2027-06-01T08:00:00+00:00",
            "2027-06-01T10:00:00+00:00",
            "EA201",
        )
        returning = movement(
            "movement-regional-return",
            "BBB",
            "AAA",
            "2027-06-08T09:00:00+00:00",
            "2027-06-08T11:00:00+00:00",
            "EA202",
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-regional-joint",
                joint_task["task_id"],
                "verified_observed",
                movements=[outbound, returning],
                offers=[
                    offer(
                        "offer-regional-joint",
                        "joint_round_trip",
                        [
                            "movement-regional-outbound",
                            "movement-regional-return",
                        ],
                        150,
                    )
                ],
            ),
        )
        access_task = next(
            task
            for task in state["research_tasks"]
            if task["hypothesis_id"] == hypothesis_id
            and task.get("query_shape", {}).get("surface_role")
            == "origin-access-outbound"
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-access-single-provider-no-result",
                access_task["task_id"],
                "no_result",
                coverage_complete=True,
                observations=[
                    {
                        "evidence_status": "no_result",
                        "reason": "One coach Provider returned no result.",
                    }
                ],
            ),
        )
        access_after_no_result = next(
            task
            for task in state["research_tasks"]
            if task["task_id"] == access_task["task_id"]
        )
        self.assertEqual("open", access_after_no_result["requirement_status"])
        too_early_access = surface_movement(
            "movement-access-before-window",
            "alpha-city",
            "AAA",
            "2027-05-31T05:00:00+00:00",
            "2027-05-31T05:50:00+00:00",
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-access-before-window",
                access_task["task_id"],
                "verified_observed",
                movements=[too_early_access],
                offers=[
                    offer(
                        "offer-access-before-window",
                        "surface",
                        [too_early_access["movement_id"]],
                        8,
                    )
                ],
            ),
        )
        access_after_early_offer = next(
            task
            for task in state["research_tasks"]
            if task["task_id"] == access_task["task_id"]
        )
        self.assertEqual("open", access_after_early_offer["requirement_status"])
        self.assertEqual(2, access_after_early_offer["attempt_count"])
        self.assertNotIn(
            too_early_access["movement_id"],
            {item["movement_id"] for item in state["movements"]},
        )
        egress_task = next(
            task
            for task in state["research_tasks"]
            if task["hypothesis_id"] == hypothesis_id
            and task.get("query_shape", {}).get("surface_role")
            == "origin-egress-return"
        )
        self.assertEqual(
            "2027-06-08T12:00:00+00:00",
            egress_task["query_shape"]["schedule_context"]["not_before"],
        )
        egress_task["query_shape"]["pool_constraints"]["max_time_minutes"] = 100

        early = surface_movement(
            "movement-egress-too-early",
            "AAA",
            "alpha-city",
            "2027-06-08T11:34:00+00:00",
            "2027-06-08T14:30:00+00:00",
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-egress-too-early",
                egress_task["task_id"],
                "verified_observed",
                coverage_complete=True,
                movements=[early],
                offers=[
                    offer(
                        "offer-egress-too-early",
                        "surface",
                        [early["movement_id"]],
                        10,
                    )
                ],
            ),
        )
        reopened = next(
            task
            for task in state["research_tasks"]
            if task["task_id"] == egress_task["task_id"]
        )
        self.assertEqual("open", reopened["requirement_status"])
        self.assertEqual(1, reopened["attempt_count"])
        self.assertNotIn(
            early["movement_id"],
            {item["movement_id"] for item in state["movements"]},
        )
        self.assertIn(
            early["movement_id"],
            {
                movement["movement_id"]
                for bundle in state["evidence_bundles"]
                if bundle["bundle_id"] == "bundle-egress-too-early"
                for movement in bundle["movements"]
            },
        )
        control = RoundTripProductAdapter().inspect(state)
        self.assertIn(
            egress_task["task_id"],
            {item.get("task_id") for item in control["investigation_actions"]},
        )
        self.assertFalse(
            any(
                action.get("hypothesis_id") == hypothesis_id
                and action.get("disposition") == "hard_dead"
                for action in control["actions"]
            )
        )

        valid_local = surface_movement(
            "movement-egress-valid-local",
            "AAA",
            "surface-hub",
            "2027-06-08T12:04:00+00:00",
            "2027-06-08T12:30:00+00:00",
        )
        valid_coach = surface_movement(
            "movement-egress-valid-coach",
            "surface-hub",
            "alpha-city",
            "2027-06-08T12:50:00+00:00",
            "2027-06-08T13:40:00+00:00",
            mode="coach",
        )
        state = SESSION.ingest_evidence(
            state,
            evidence(
                "bundle-egress-valid",
                egress_task["task_id"],
                "verified_observed",
                nodes=[
                    node(
                        "surface-hub",
                        node_type="station",
                        location="Surface Hub",
                    )
                ],
                movements=[valid_local, valid_coach],
                offers=[
                    offer(
                        "offer-egress-valid-local",
                        "surface",
                        [valid_local["movement_id"]],
                        0,
                    ),
                    offer(
                        "offer-egress-valid-coach",
                        "surface",
                        [valid_coach["movement_id"]],
                        12,
                    )
                ],
            ),
        )
        satisfied = next(
            task
            for task in state["research_tasks"]
            if task["task_id"] == egress_task["task_id"]
        )
        self.assertEqual("satisfied", satisfied["requirement_status"])
        self.assertEqual(2, satisfied["attempt_count"])

    def test_persisted_session_omits_reconstructable_adapter_projections(self) -> None:
        persisted = SESSION.initialize_session(
            MATERIALIZER.materialize_intent(intent(regional=True))
        )

        self.assertIn("hypotheses", persisted)
        self.assertIn("active_hypothesis_id", persisted)
        for field in (
            "active_hypothesis",
            "hypothesis_tree",
            "hypothesis_history",
            "hypothesis_outcomes",
            "portfolio_challenger_dispositions",
            "portfolio_exploration_archive",
            "portfolio_exploration_view",
            "query_ledger",
            "offer_sets",
            "finalization_history",
        ):
            self.assertNotIn(field, persisted)
        self.assertTrue(
            all("status" not in task for task in persisted["research_tasks"])
        )

        hydrated = SESSION.validate_session(persisted)
        self.assertIn("hypothesis_tree", hydrated)
        self.assertIn("status", hydrated["research_tasks"][0])

    def test_exact_and_regional_initialization_are_distinct(self) -> None:
        exact = initialize()
        regional = initialize(regional=True)

        self.assertEqual(exact["product_revision"], 0)
        self.assertEqual(exact["region_scopes"]["origin"]["status"], "not_required")
        self.assertEqual(
            exact["hypothesis_tree"]["nodes"][SESSION.ORIGIN_GATEWAY_HYPOTHESIS]["status"],
            "complete",
        )
        self.assertEqual(regional["region_scopes"]["origin"]["status"], "awaiting_ai")
        self.assertEqual(regional["region_scopes"]["destination"]["status"], "awaiting_ai")
        self.assertNotIn("backtrack_history", regional["hypothesis_tree"])
        self.assertNotIn("anchor_edge_ids", next(iter(regional["research_tasks"])))
        self.assertNotIn("query_ledger", regional)
        self.assertNotIn("offer_sets", regional)
        self.assertNotIn("portfolio_challenger_history", regional)
        self.assertNotIn("finalization_history", regional)

        original = copy.deepcopy(exact)
        validated = SESSION.validate_session(exact)
        self.assertEqual(exact, original)
        self.assertEqual(validated, original)

    def test_single_scope_pass_and_gateway_coverage_are_required(self) -> None:
        state = initialize(regional=True)
        state = complete_regional_side(state, "origin", "AAA")
        state = complete_regional_side(state, "destination", "BBB")

        for side_name in ("origin", "destination"):
            scope = state["region_scopes"][side_name]
            self.assertEqual(scope["status"], "defined")
            self.assertNotIn("pending", scope["coverage"].values())
            self.assertTrue(
                any(
                    item.get("evidence_status") == "research_observed"
                    for item in state["gateway_candidates"][side_name]
                )
            )
        self.assertGreater(state["product_revision"], 0)

    def test_joint_and_split_atomic_offers_both_reach_solver(self) -> None:
        baseline, _ = route_state()
        baseline_hypothesis_id = baseline["active_hypothesis_id"]
        baseline_scopes = {
            task.get("journey_scope")
            for task in baseline["research_tasks"]
            if task.get("hypothesis_id") == baseline_hypothesis_id
        }
        self.assertEqual({"joint_round_trip"}, baseline_scopes)

        state, _ = route_state(include_split=True)

        kinds = state["result"]["portfolio_kind_counts"]
        self.assertGreater(kinds["joint_round_trip"], 0)
        self.assertGreater(kinds["split_one_ways"], 0)
        self.assertEqual(
            state["result"]["best_portfolio"]["portfolio_kind"],
            "joint_round_trip",
        )
        self.assertNotIn("solver_projection", state["result"])

        at_limit, _ = route_state(
            maximum_outbound_duration_minutes=120,
            maximum_return_duration_minutes=120,
        )
        too_tight, _ = route_state(maximum_outbound_duration_minutes=119)
        self.assertIsNotNone(at_limit["result"]["best_portfolio"])
        self.assertIsNone(too_tight["result"]["best_portfolio"])

    def test_paid_baggage_permission_requires_a_visible_mandatory_cost(self) -> None:
        allowed, _ = route_state(
            required_baggage_items=["cabin bag"],
            paid_baggage_allowed=True,
            mandatory_baggage_cost=25,
        )
        forbidden, _ = route_state(
            required_baggage_items=["cabin bag"],
            paid_baggage_allowed=False,
            mandatory_baggage_cost=25,
        )
        unpriced, _ = route_state(
            required_baggage_items=["cabin bag"],
            paid_baggage_allowed=True,
            mandatory_baggage_cost=0,
        )

        self.assertIsNotNone(allowed["result"]["best_portfolio"])
        self.assertEqual(
            allowed["result"]["best_portfolio"]["paid_baggage_items_by_offer"][
                "offer-joint"
            ],
            ["cabin bag"],
        )
        self.assertIsNone(forbidden["result"]["best_portfolio"])
        self.assertIsNone(unpriced["result"]["best_portfolio"])

    def test_temporal_research_advances_same_task_to_payable_verification(self) -> None:
        state, _ = route_state(temporal_first=True)
        joint_task = next(
            task
            for task in state["research_tasks"]
            if task.get("journey_scope") == "joint_round_trip"
        )

        self.assertEqual(joint_task["temporal_state"]["phase"], "terminal")
        self.assertEqual(
            joint_task["temporal_state"]["active_selection"]["candidate_id"],
            "candidate-joint",
        )
        self.assertIn("signal-joint", joint_task["temporal_state"]["signal_refs"])
        self.assertIn("offer-joint", {item["offer_id"] for item in state["offers"]})

    def test_duplicate_evidence_is_idempotent_but_conflicts_are_rejected(self) -> None:
        state, joint_evidence = route_state()
        revision = state["product_revision"]
        replayed = SESSION.ingest_evidence(state, joint_evidence)

        self.assertEqual(SESSION.validate_session(replayed), state)
        self.assertEqual(replayed["product_revision"], revision)
        conflicting = copy.deepcopy(joint_evidence)
        conflicting["coverage_complete"] = False
        with self.assertRaisesRegex(SESSION.RoundTripSessionError, "conflicting content"):
            SESSION.ingest_evidence(state, conflicting)

    def test_complete_verified_portfolio_finalizes_without_audit_ceremony(self) -> None:
        state = complete_for_finalization()

        self.assertTrue(SESSION._finalization_audit(state)["can_finalize"])
        self.assertFalse(
            any(
                task.get("research_kind") == "structural_challenger_definition"
                for task in state["research_tasks"]
            )
        )
        finalized, result = SESSION.finalize_session(state, "Verified portfolio complete.")
        self.assertEqual(finalized["finalization_status"], "final")
        self.assertEqual(result["search_status"], "final")
        self.assertGreater(finalized["product_revision"], state["product_revision"])
        self.assertTrue(SESSION._finalization_audit(finalized)["can_finalize"])

    def test_expired_incumbent_can_fall_back_and_finalize(self) -> None:
        state = complete_for_finalization(include_split=True)
        joint = next(item for item in state["offers"] if item["offer_id"] == "offer-joint")
        joint["expires_at"] = "2030-01-01T00:00:00+00:00"
        original_now = SESSION._now
        SESSION._now = lambda: "2040-01-01T00:00:00+00:00"
        try:
            self.assertTrue(SESSION._refresh_time_sensitive_state(state))
            state = SESSION.validate_session(state)
            self.assertEqual(
                state["result"]["best_portfolio"]["portfolio_kind"],
                "split_one_ways",
            )
            self.assertTrue(state["hypotheses"])
            finalized, result = SESSION.finalize_session(state, "Fresh portfolio selected.")
            self.assertEqual(result["best_portfolio"]["portfolio_kind"], "split_one_ways")
            self.assertEqual(finalized["finalization_status"], "final")
        finally:
            SESSION._now = original_now


if __name__ == "__main__":
    unittest.main()
