"""Result freshness, price ordering, and compact Worker projection specifications."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flight_search_harness.adapters.codex_app_server import CodexAppServerBrowserAdapter
from flight_search_harness.adapters.one_way import OneWayProductAdapter
from flight_search_harness.adapters.random_loop import RandomLoopProductAdapter
from flight_search_harness.adapters.round_trip import RoundTripProductAdapter
from flight_search_harness.contracts import HarnessProtocolError, result_price_freshness
from flight_search_harness.fixed_route_provider import validate_price_ordering
from flight_search_harness.worker_projection import project_item, project_work


ROOT = Path(__file__).resolve().parents[1]


class ResultFreshnessTestCase(unittest.TestCase):
    def test_completed_result_expires_after_900_seconds(self) -> None:
        expired = result_price_freshness(
            "2026-10-11T08:00:00+00:00",
            now_utc=datetime(2026, 10, 11, 8, 15, tzinfo=timezone.utc),
        )
        self.assertEqual("2026-10-11T08:15:00+00:00", expired["price_valid_until"])
        self.assertEqual("expired", expired["price_freshness"])


class PriceOrderingTestCase(unittest.TestCase):
    def test_fare_requires_generic_lowest_price_method(self) -> None:
        attempt = {
            "kind": "observations",
            "product_payload": {
                "observations": [
                    {
                        "evidence_status": "research_observed",
                        "source_id": "provider-a",
                        "provider": "Provider A",
                        "url": "https://example.invalid/flights",
                        "gateway_candidates": [
                            {
                                "signal": {
                                    "lowest_displayed_total": {
                                        "amount": 5616,
                                        "currency": "CNY",
                                    }
                                }
                            }
                        ],
                    }
                ]
            },
        }
        with self.assertRaisesRegex(
            HarnessProtocolError, "requires price_selection_method"
        ):
            validate_price_ordering(attempt)
        attempt["product_payload"]["observations"][0][
            "price_selection_method"
        ] = "recommended_view"
        with self.assertRaisesRegex(
            HarnessProtocolError, "requires price_selection_method"
        ):
            validate_price_ordering(attempt)
        attempt["product_payload"]["observations"][0][
            "price_selection_method"
        ] = "lowest_price_view"
        validate_price_ordering(attempt)

class WorkerProjectionTestCase(unittest.TestCase):
    @staticmethod
    def _one_way_node(node_id: str, location: str) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "node_type": "airport",
            "location": location,
            "country_or_region": "Testland",
            "country_code": "XT",
            "timezone": "Etc/UTC",
            "utc_offset": "+00:00",
        }

    @classmethod
    def _real_one_way_item_and_projection(
        cls,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        def side(region_id: str, node: dict[str, Any]) -> dict[str, Any]:
            return {
                "region_id": region_id,
                "label": region_id,
                "country_codes": ["XT"],
                "policy": "exact",
                "endpoint_policy": "anchor_required",
                "anchor_nodes": [node],
                "seed_gateway_nodes": [],
                "max_ground_cost": None,
            }

        request = {
            "request_version": "4.0",
            "journey_shape": "one_way",
            "intent": {
                "base_currency": "XTS",
                "adult_count": 2,
                "baggage_policy": {
                    "cabin_baggage": "none_required",
                    "checked_bag_count": 0,
                    "paid_baggage_allowed": True,
                },
                "overnight_allowed": False,
                "self_transfer_policy": "allow",
                "origin": side(
                    "origin-region", cls._one_way_node("AAA", "Alpha Airport")
                ),
                "destination": side(
                    "destination-region", cls._one_way_node("BBB", "Beta Airport")
                ),
                "departure_window": {
                    "start": "2027-04-01",
                    "end": "2027-04-05",
                },
            },
        }
        adapter = OneWayProductAdapter()
        with tempfile.TemporaryDirectory() as raw_directory:
            product_directory = Path(raw_directory)
            state = adapter.initialize(request, product_directory)
            control = adapter.inspect(state)
            action = next(
                value
                for value in control["investigation_actions"]
                if value.get("task_id")
            )
            item = adapter.build_research_items(
                product_directory,
                [action],
                "Projection contract test.",
                lambda prefix, _value: f"{prefix}:projection-contract",
            )[0]
        projection = project_item(
            item,
            full_item_ref="mission/items/projection-contract.json",
            full_item_hash="f" * 64,
        )["payload"]["work"]
        return item, projection

    @classmethod
    def _real_random_loop_item_and_projection(
        cls,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_directory = Path(raw_directory)
            catalog_path = product_directory / "airports.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "airports": {
                            "AAA": {
                                "code": "AAA",
                                "name": "Alpha Airport",
                                "country_code": "XT",
                                "latitude": 0.0,
                                "longitude": 0.0,
                                "timezone": "UTC",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            adapter = RandomLoopProductAdapter(airport_catalog_ref=catalog_path)
            state = adapter.initialize(
                {
                    "request_version": "4.0",
                    "journey_shape": "random_loop",
                    "intent": {
                        "base_currency": "XTS",
                        "adult_count": 1,
                        "baggage_policy": {
                            "cabin_baggage": "none_required",
                            "checked_bag_count": 0,
                            "paid_baggage_allowed": True,
                        },
                        "overnight_allowed": True,
                        "self_transfer_policy": "allow",
                        "departure_airports": ["AAA"],
                        "return_airports": ["AAA"],
                        "first_departure_window": {
                            "start": "2027-05-01T00:00:00+00:00",
                            "end": "2027-05-03T00:00:00+00:00",
                        },
                        "min_legs": 2,
                        "max_legs": 3,
                        "connection_buffer_minutes": 30,
                        "max_connection_wait_minutes": 720,
                        "maximum_total_duration_minutes": 2880,
                        "quality_gate": {
                            "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
                            "maximum_leg_price_in_base_currency": None,
                        },
                    },
                },
                product_directory,
            )
            control = adapter.inspect(state)
            action = control["investigation_actions"][0]
            item = adapter.build_research_items(
                product_directory,
                [action],
                "Projection contract test.",
                lambda prefix, _value: f"{prefix}:random-projection-contract",
            )[0]
        projection = project_item(
            item,
            full_item_ref="mission/items/random-projection-contract.json",
            full_item_hash="e" * 64,
        )["payload"]["work"]
        return item, projection

    @classmethod
    def _real_round_trip_item_and_projection(
        cls,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        def side(region_id: str, node: dict[str, Any]) -> dict[str, Any]:
            return {
                "region_id": region_id,
                "label": region_id,
                "country_codes": ["XT"],
                "policy": "exact",
                "endpoint_policy": "anchor_required",
                "anchor_nodes": [node],
                "seed_gateway_nodes": [],
                "max_ground_cost": None,
            }

        adapter = RoundTripProductAdapter()
        with tempfile.TemporaryDirectory() as raw_directory:
            product_directory = Path(raw_directory)
            state = adapter.initialize(
                {
                    "request_version": "4.0",
                    "journey_shape": "round_trip",
                    "intent": {
                        "base_currency": "XTS",
                        "adult_count": 2,
                        "baggage_policy": {
                            "cabin_baggage": "personal_item",
                            "checked_bag_count": 0,
                            "paid_baggage_allowed": True,
                        },
                        "overnight_allowed": True,
                        "self_transfer_policy": "allow",
                        "origin": side(
                            "origin-region", cls._one_way_node("AAA", "Alpha Airport")
                        ),
                        "destination": side(
                            "destination-region", cls._one_way_node("BBB", "Beta Airport")
                        ),
                        "outbound_departure_window": {
                            "start": "2027-06-01T00:00:00+00:00",
                            "end": "2027-06-03T23:59:00+00:00",
                        },
                        "return_departure_window": {
                            "start": "2027-06-08T00:00:00+00:00",
                            "end": "2027-06-10T23:59:00+00:00",
                        },
                    },
                },
                product_directory,
            )
            control = adapter.inspect(state)
            if not control["investigation_actions"]:
                activation = next(
                    value
                    for value in control["actions"]
                    if str(value.get("kind", "")).startswith("activate")
                )
                activation = {**copy.deepcopy(activation), "reason": "Projection contract test."}
                adapter.apply_control_action(product_directory, activation)
                control = adapter.inspect(adapter.load_state(product_directory))
            action = next(
                value
                for value in control["investigation_actions"]
                if value.get("research_kind") == "priced_offer"
                or value.get("task_id")
            )
            item = adapter.build_research_items(
                product_directory,
                [action],
                "Projection contract test.",
                lambda prefix, _value: f"{prefix}:round-projection-contract",
            )[0]
        projection = project_item(
            item,
            full_item_ref="mission/items/round-projection-contract.json",
            full_item_hash="d" * 64,
        )["payload"]["work"]
        return item, projection

    def test_real_one_way_projection_is_executable_without_full_brief(self) -> None:
        item, projection = self._real_one_way_item_and_projection()

        self.assertTrue(projection["research_kind"])
        self.assertTrue(projection["task_family"])
        self.assertTrue(projection["request_type"])
        self.assertTrue(projection["query"])
        self.assertTrue(projection["query_scope_ladder"])
        self.assertIsNotNone(projection["corridor_context"])
        effective_source = projection["assigned_source"] or projection["required_source"]
        self.assertTrue(effective_source["source_id"])
        self.assertEqual(2, projection["traveler_count"])
        self.assertEqual(
            {"mode": "none_required", "paid_baggage": True},
            projection["required_baggage"],
        )
        self.assertIs(projection["overnight_allowed"], False)
        self.assertEqual("allow", projection["self_transfer_policy"])
        self.assertTrue(projection["time_window"])
        self.assertTrue(projection["date_search"])
        self.assertEqual(
            "lowest_total_price", projection["price_ordering"]["objective"]
        )
        self.assertIsInstance(projection["attempt_schema"], dict)

    def test_real_random_loop_projection_preserves_ordered_provider_protocol(self) -> None:
        _, projection = self._real_random_loop_item_and_projection()

        self.assertEqual("random_loop_search", projection["research_kind"])
        batch = projection["query"]["required_source_batch"]
        self.assertGreaterEqual(len(batch), 2)
        protocol = projection["provider_batch_protocol"]
        self.assertIs(protocol["binding"], True)
        self.assertEqual(batch, protocol["required_source_batch"])
        rule = projection["evidence_rule"]
        self.assertEqual(rule, protocol["execution_and_stop_rule"])
        for required_text in (
            "scope_partitions",
            "source_disposition=retry_current",
            "source_disposition=advance_source",
            "source_unavailable",
            "scope_unsupported",
            "Stop after payable",
        ):
            self.assertIn(required_text, rule)

    def test_real_round_trip_projection_preserves_temporal_protocol(self) -> None:
        _, projection = self._real_round_trip_item_and_projection()

        self.assertEqual("priced_offer", projection["research_kind"])
        temporal_execution = projection["temporal_execution"]
        temporal_research = projection["temporal_research_option"]
        self.assertEqual("surface_discovery", temporal_execution["phase"])
        self.assertEqual("ai", temporal_execution["decision_owner"])
        self.assertIs(temporal_execution["scheduler_may_not_publish_or_select_tasks"], True)
        self.assertEqual("research_observed", temporal_research["evidence_status"])
        self.assertIs(temporal_research["coverage_complete"], False)
        self.assertIs(temporal_research["creates_offer_or_movement"], False)
        self.assertEqual(
            "lowest_total_price", projection["price_ordering"]["objective"]
        )

    def test_region_pool_projection_preserves_complete_execution_scope(self) -> None:
        query = {
            "trip_type": "region_pool_fare_screen",
            "origin_gateway_node_ids": ["AAA", "AAX"],
            "destination_gateway_node_ids": ["BBB", "BBX"],
            "outbound_departure_window": {
                "start": "2027-06-01T00:00:00+00:00",
                "end": "2027-06-03T23:59:00+00:00",
            },
            "return_departure_window": {
                "start": "2027-06-08T00:00:00+00:00",
                "end": "2027-06-10T23:59:00+00:00",
            },
            "minimum_stay_nights": 7,
            "maximum_stay_nights": 9,
        }
        projection = project_work(
            {
                "work_version": "1.0",
                "product_id": "round_trip",
                "task_id": "task-region-pool-fare-screen-g1",
                "hypothesis_id": "hypothesis-gateway-destination",
                "brief": {
                    "research_kind": "region_pool_fare_screen",
                    "query_shape": copy.deepcopy(query),
                    "traveler_count": 1,
                    "baggage_policy": {
                        "required_items": [],
                        "paid_baggage_allowed": True,
                    },
                    "evidence_contract": {
                        "evidence_status": "research_observed",
                        "provider_scope": {
                            "coverage_semantics": "provider_ranked_window"
                        },
                        "rule": "Execute Provider-ranked airport partitions.",
                    },
                },
                "attempt_schema": {"type": "object"},
            },
            research_kind="region_pool_fare_screen",
        )

        self.assertEqual(query, projection["query"])
        self.assertEqual(
            "provider_ranked_window",
            projection["admission"]["provider_scope"]["coverage_semantics"],
        )

    def test_codex_serializes_real_random_and_round_trip_policy_projections(self) -> None:
        _, one_way_projection = self._real_one_way_item_and_projection()
        _, random_projection = self._real_random_loop_item_and_projection()
        _, round_projection = self._real_round_trip_item_and_projection()

        class CapturingAdapter(CodexAppServerBrowserAdapter):
            def __init__(self) -> None:
                super().__init__(cwd=ROOT)
                self.requests: list[dict[str, Any]] = []

            def _run_structured(self, _context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                self.requests.append(
                    json.loads(kwargs["prompt"].split("\n\n", 1)[1])
                )
                return {
                    "payload_json": json.dumps(
                        {"kind": "observations", "product_payload": {}}
                    ),
                    "_browser_state": {},
                }

        browser = CapturingAdapter()
        browser.execute(one_way_projection, "operation:one-way", {})
        browser.execute(random_projection, "operation:random", {})
        browser.execute(round_projection, "operation:round", {})

        projected = [request["work_projection"] for request in browser.requests]
        self.assertTrue(projected[0]["query_scope_ladder"])
        self.assertTrue(projected[1]["provider_batch_protocol"])
        self.assertTrue(projected[1]["evidence_rule"])
        self.assertTrue(projected[2]["temporal_execution"])
        self.assertTrue(projected[2]["temporal_research_option"])

    def test_codex_app_server_batch_serializes_canonical_projections(self) -> None:
        _, projection = self._real_one_way_item_and_projection()

        class CapturingBatchAdapter(CodexAppServerBrowserAdapter):
            def __init__(self) -> None:
                super().__init__(cwd=ROOT)
                self.request: dict[str, Any] | None = None

            def _run_structured(self, _context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                self.request = json.loads(kwargs["prompt"].split("\n\n", 1)[1])
                return {
                    "items": [
                        {
                            "item_id": task["item_id"],
                            "payload_json": json.dumps(
                                {
                                    "kind": "observations",
                                    "source": {},
                                    "observations": [],
                                }
                            ),
                        }
                        for task in self.request["tasks"]
                    ],
                    "_browser_state": {},
                }

        browser = CapturingBatchAdapter()
        result = browser.execute_batch(
            [
                {
                    "item_id": "item-a",
                    "action_id": "operation:a",
                    "work": projection,
                },
                {
                    "item_id": "item-b",
                    "action_id": "operation:b",
                    "work": copy.deepcopy(projection),
                },
            ],
            {},
        )

        assert browser.request is not None
        self.assertEqual({"item-a", "item-b"}, set(result["attempts_by_item"]))
        for task in browser.request["tasks"]:
            self.assertEqual(projection, task["work_projection"])
            self.assertNotIn("brief", task)


if __name__ == "__main__":
    unittest.main()
