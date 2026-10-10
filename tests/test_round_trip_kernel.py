"""Product-level executable specifications for the round-trip Product Kernel."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path
from typing import Any


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
    return {
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


def initialize(
    *,
    regional: bool = False,
    required_baggage_items: list[str] | None = None,
    paid_baggage_allowed: bool = False,
) -> dict[str, Any]:
    return SESSION.validate_session(
        SESSION.initialize_session(
            MATERIALIZER.materialize_intent(
                intent(
                    regional=regional,
                    required_baggage_items=required_baggage_items,
                    paid_baggage_allowed=paid_baggage_allowed,
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
    movements: list[dict[str, Any]] | None = None,
    offers: list[dict[str, Any]] | None = None,
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
        "movements": copy.deepcopy(movements or []),
        "offers": copy.deepcopy(offers or []),
        "observations": [],
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


def challenge_proposal(side_name: str) -> dict[str, Any]:
    return {
        "proposal_version": SESSION.AI_EXPLORATION.VERSION,
        "side": side_name,
        "region_id": f"{side_name}-region",
        "reviewed_categories": sorted(
            SESSION.AI_EXPLORATION.REGION_SCOPE_REVIEW_CATEGORIES
        ),
        "additional_candidates": [],
        "no_additional_candidate_reason": "Every required category was reviewed; no additional airport fits the declared limits.",
        "challenge_reason": "Independent adversarial scope review completed.",
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
    challenge_task_id = f"task-region-scope-challenge-{side_name}-g1"
    state = SESSION.ingest_evidence(
        state,
        evidence(
            f"bundle-{side_name}-challenge",
            challenge_task_id,
            "ai_proposed",
            ai_proposals=[
                {
                    "proposal_type": "region_scope_challenge",
                    "proposal": challenge_proposal(side_name),
                }
            ],
        ),
    )
    gateway_task_id = f"task-gateway-{side_name}-{gateway_id.lower()}-g1"
    state = SESSION.ingest_evidence(
        state,
        evidence(
            f"bundle-{side_name}-gateway",
            gateway_task_id,
            "research_observed",
            gateway_candidates=[
                {
                    "candidate_id": f"observed-{side_name}-{gateway_id}",
                    "side": side_name,
                    "node": gateway,
                    "signal": {"kind": "observed_connectivity"},
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = initialize(
        required_baggage_items=required_baggage_items,
        paid_baggage_allowed=paid_baggage_allowed,
    )
    suggestion = GENERATOR.generate_suggestions(state)["suggestions"][0]
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
    *, expires_at: str = "2099-01-01T00:00:00+00:00"
) -> dict[str, Any]:
    state, _ = route_state(expires_at=expires_at)
    state = SESSION.record_hypothesis_decision(
        state, "complete", reason="Every required atomic question has terminal Evidence."
    )
    state = SESSION.validate_session(state)
    structural_task = next(
        task
        for task in state["research_tasks"]
        if task["hypothesis_id"] == SESSION.STRUCTURAL_DISCOVERY_HYPOTHESIS
    )
    state = SESSION.record_hypothesis_decision(
        state,
        "select",
        task_id=structural_task["task_id"],
        reason="Assess a materially different structural challenger.",
    )
    state = SESSION.ingest_evidence(
        state,
        evidence(
            "bundle-structural",
            structural_task["task_id"],
            "ai_proposed",
            ai_proposals=[
                {
                    "proposal_type": "structural_challengers",
                    "proposal": {
                        "proposal_version": SESSION.AI_EXPLORATION.VERSION,
                        "proposals": [],
                        "no_material_challenger_reason": "No materially different structure fits this exact-airport request.",
                    },
                }
            ],
        ),
    )
    state = SESSION.record_hypothesis_decision(
        state, "complete", reason="Structural challenger discovery is terminal."
    )
    state = SESSION.record_stop_assessment(
        state, "No unresolved evidence-backed challenger remains."
    )
    return SESSION.validate_session(state)


class RoundTripKernelTests(unittest.TestCase):
    def test_persisted_session_omits_reconstructable_adapter_projections(self) -> None:
        persisted = SESSION.initialize_session(
            MATERIALIZER.materialize_intent(intent(regional=True))
        )

        self.assertIn("hypotheses", persisted)
        self.assertIn("active_hypothesis_id", persisted)
        self.assertIn("challenger_dispositions", persisted)
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
        self.assertNotIn("dispositions", persisted["structural_exploration"])

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

    def test_dual_scope_passes_and_gateway_coverage_are_required(self) -> None:
        state = initialize(regional=True)
        state = complete_regional_side(state, "origin", "AAA")
        state = complete_regional_side(state, "destination", "BBB")

        for side_name in ("origin", "destination"):
            scope = state["region_scopes"][side_name]
            self.assertEqual(scope["status"], "defined")
            self.assertTrue(scope["challenge_history"])
            self.assertNotIn("pending", scope["coverage"].values())
            self.assertTrue(
                any(
                    item.get("evidence_status") == "research_observed"
                    for item in state["gateway_candidates"][side_name]
                )
            )
        self.assertGreater(state["product_revision"], 0)

    def test_joint_and_split_atomic_offers_both_reach_solver(self) -> None:
        state, _ = route_state()

        kinds = state["result"]["portfolio_kind_counts"]
        self.assertGreater(kinds["joint_round_trip"], 0)
        self.assertGreater(kinds["split_one_ways"], 0)
        self.assertEqual(
            state["result"]["best_portfolio"]["portfolio_kind"],
            "joint_round_trip",
        )
        self.assertNotIn("solver_projection", state["result"])

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

    def test_challenger_stop_assessment_is_revision_bound_and_finalizes(self) -> None:
        state = complete_for_finalization()
        assessment = state["stop_assessment"]

        self.assertEqual(assessment["assessed_revision"], state["product_revision"])
        self.assertTrue(SESSION._finalization_audit(state)["can_finalize"])
        finalized, result = SESSION.finalize_session(state, "Product audit passed.")
        self.assertEqual(finalized["finalization_status"], "final")
        self.assertEqual(result["search_status"], "final")
        self.assertGreater(finalized["product_revision"], state["product_revision"])
        self.assertTrue(SESSION._finalization_audit(finalized)["can_finalize"])

    def test_expired_incumbent_can_fall_back_then_receive_fresh_stop(self) -> None:
        state = complete_for_finalization()
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
            state = SESSION.record_stop_assessment(
                state, "The surviving split portfolio has no unresolved challenger."
            )
            finalized, result = SESSION.finalize_session(state, "Fresh audit passed.")
            self.assertEqual(result["best_portfolio"]["portfolio_kind"], "split_one_ways")
            self.assertEqual(finalized["finalization_status"], "final")
        finally:
            SESSION._now = original_now


if __name__ == "__main__":
    unittest.main()
