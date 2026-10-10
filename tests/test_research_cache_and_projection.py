"""Research Cache safety and compact Worker projection specifications."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flight_search_harness.adapters.codex_app_server import CodexAppServerBrowserAdapter
from flight_search_harness.adapters.one_way import OneWayProductAdapter
from flight_search_harness.adapters.random_loop import RandomLoopProductAdapter
from flight_search_harness.adapters.round_trip import RoundTripProductAdapter
from flight_search_harness.mission_workers import ExternalMissionWorker, _rebind_cached_payload
from flight_search_harness.research_cache import ResearchCache
from flight_search_harness.worker_projection import project_item, project_work


ONE_WAY = "one_way"
ROUND_TRIP = "round_trip"
RANDOM_LOOP = "random_loop"
ROOT = Path(__file__).resolve().parents[1]


class ResearchCacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.cache = ResearchCache(self.root)

    def _cache_path(self, scope: str, fingerprint: str) -> Path:
        return self.root / f"{self.cache._token(scope, fingerprint)}.json"

    def test_cache_hash_mismatch_and_expiry_are_misses(self) -> None:
        scope = f"provider-observation:{RANDOM_LOOP}"
        fingerprint = "query-a"
        payload = {"kind": "observations", "product_payload": {"offers": [1]}}
        self.cache.put(scope, fingerprint, payload, ttl_seconds=60)
        self.assertEqual(payload, self.cache.get(scope, fingerprint))

        target = self._cache_path(scope, fingerprint)
        persisted = json.loads(target.read_text(encoding="utf-8"))
        persisted["payload"]["product_payload"]["offers"] = [2]
        target.write_text(json.dumps(persisted), encoding="utf-8")
        self.assertIsNone(self.cache.get(scope, fingerprint))

        self.cache.put(scope, fingerprint, payload, ttl_seconds=60)
        persisted = json.loads(target.read_text(encoding="utf-8"))
        persisted["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        target.write_text(json.dumps(persisted), encoding="utf-8")
        self.assertIsNone(self.cache.get(scope, fingerprint))

    def test_external_worker_does_not_fall_back_to_another_products_cache(self) -> None:
        shared_scope = "fixed-route-observation-bundle"
        fingerprint = "identical-query"
        self.cache.put(
            shared_scope,
            fingerprint,
            {
                "kind": "observation_bundle",
                "product_payload": {"bundle_id": "one-way-bundle"},
            },
            ttl_seconds=60,
        )
        lease = {
            "lease_id": "lease-a",
            "mission_revision": 0,
            "claim": {
                "claim_id": "claim-a",
                "worker_instance_id": "external-test",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "items": [
                {
                    "item_id": "round-trip-research",
                    "kind": "browser_research",
                    "payload": {
                        "work": {
                            "product_id": ROUND_TRIP,
                            "execution": {
                                "cache_scope": shared_scope,
                                "query_fingerprint": fingerprint,
                            },
                        }
                    },
                }
            ],
        }

        def checkpoint(_: dict[str, Any]) -> dict[str, Any]:
            self.fail("a cache entry without the current product scope must be a miss")

        result = ExternalMissionWorker().run(
            lease,
            checkpoint,
            {"cache_root": str(self.root), "lease_ref": "mission/lease.json"},
        )
        self.assertEqual("yield", result["kind"])

    def test_cache_removes_and_rebinds_admission_identity_per_attempt(self) -> None:
        scope = f"provider-observation:{ONE_WAY}"
        fingerprint = "query-identity"
        self.cache.put(
            scope,
            fingerprint,
            {
                "kind": "observation_bundle",
                "bundle_id": "stale-top-level",
                "product_payload": {"bundle_id": "stale-product", "offers": [1]},
            },
            ttl_seconds=60,
        )
        cached = self.cache.get(scope, fingerprint)
        self.assertNotIn("bundle_id", cached)
        self.assertNotIn("bundle_id", cached["product_payload"])

        rebound = _rebind_cached_payload(
            cached,
            {"product_id": ONE_WAY},
            operation_id="lease-a:work-a:attempt-2",
        )
        self.assertRegex(rebound["bundle_id"], r"^cache-bundle-[a-f0-9]{20}$")
        self.assertEqual(
            rebound["bundle_id"], rebound["product_payload"]["bundle_id"]
        )

    def test_random_loop_cache_payload_is_rebound_to_current_request(self) -> None:
        cached = {
            "kind": "observations",
            "product_payload": {
                "search_results": [
                    {
                        "query_id": "old-query",
                        "request_id": "old-request",
                        "parent_id": "old-parent",
                        "acquisition": {"method": "browser"},
                    }
                ]
            },
        }
        work = {
            "product_id": RANDOM_LOOP,
            "brief": {
                "request": {
                    "request_id": "current-request",
                    "parent_id": "current-parent",
                }
            },
        }

        rebound = _rebind_cached_payload(cached, work)
        group = rebound["product_payload"]["search_results"][0]
        self.assertEqual("current-request", group["request_id"])
        self.assertEqual("current-parent", group["parent_id"])
        self.assertNotIn("query_id", group)
        self.assertEqual("cache", group["acquisition"]["method"])
        self.assertEqual("old-request", cached["product_payload"]["search_results"][0]["request_id"])


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
            "request_version": "2.0",
            "journey_shape": "one_way",
            "intent": {
                "base_currency": "XTS",
                "traveler_count": 2,
                "baggage_policy": {
                    "cabin_baggage": "one cabin bag",
                    "checked_bag_count": 1,
                    "paid_baggage_allowed": True,
                },
                "overnight_allowed": False,
                "self_transfer_policy": "avoid",
                "fx_to_base": {"XTS": 1.0},
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
        adapter = RandomLoopProductAdapter()
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
            state = adapter.initialize(
                {
                    "request_version": "2.0",
                    "journey_shape": "random_loop",
                    "intent": {
                        "base_currency": "XTS",
                        "traveler_count": 1,
                        "baggage_policy": {
                            "cabin_baggage": "personal item",
                            "checked_bag_count": 0,
                            "paid_baggage_allowed": True,
                        },
                        "overnight_allowed": True,
                        "self_transfer_policy": "allow",
                        "fx_to_base": {"XTS": 1.0},
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
                            "minimum_leg_efficiency_km_per_base_currency_unit": None,
                            "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
                            "target_complete_loop_efficiency_km_per_base_currency_unit": 2.0,
                            "maximum_leg_price_in_base_currency": None,
                        },
                        "airport_catalog_ref": str(catalog_path),
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
                    "request_version": "2.0",
                    "journey_shape": "round_trip",
                    "intent": {
                        "base_currency": "XTS",
                        "traveler_count": 2,
                        "baggage_policy": {
                            "cabin_baggage": "personal item",
                            "checked_bag_count": 0,
                            "paid_baggage_allowed": True,
                        },
                        "overnight_allowed": True,
                        "self_transfer_policy": "allow",
                        "fx_to_base": {"XTS": 1.0},
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
            {"mode": "one cabin bag; checked_bag_count=1", "paid_baggage": True},
            projection["required_baggage"],
        )
        self.assertIs(projection["overnight_allowed"], False)
        self.assertEqual("avoid", projection["self_transfer_policy"])
        self.assertTrue(projection["time_window"])
        self.assertTrue(projection["date_search"])
        self.assertIsInstance(projection["attempt_schema"], dict)

    def test_real_random_loop_projection_preserves_ordered_provider_protocol(self) -> None:
        item, projection = self._real_random_loop_item_and_projection()

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

        work = item["payload"]["work"]
        changed_rule = copy.deepcopy(work)
        changed_rule["brief"]["evidence_rule"] += " Changed binding rule."
        changed_batch = copy.deepcopy(work)
        changed_batch["brief"]["request"]["required_source_batch"] = ["different"]
        base_fingerprint = projection["execution"]["query_fingerprint"]
        for candidate in (changed_rule, changed_batch):
            changed = project_work(
                candidate,
                research_kind=item["payload"]["research_kind"],
                journey_scope=item["payload"]["journey_scope"],
            )
            self.assertNotEqual(
                base_fingerprint,
                changed["execution"]["query_fingerprint"],
            )

    def test_real_round_trip_projection_preserves_temporal_protocol(self) -> None:
        item, projection = self._real_round_trip_item_and_projection()

        self.assertEqual("priced_offer", projection["research_kind"])
        temporal_execution = projection["temporal_execution"]
        temporal_research = projection["temporal_research_option"]
        self.assertEqual("surface_discovery", temporal_execution["phase"])
        self.assertEqual("ai", temporal_execution["decision_owner"])
        self.assertIs(temporal_execution["scheduler_may_not_publish_or_select_tasks"], True)
        self.assertEqual("research_observed", temporal_research["evidence_status"])
        self.assertIs(temporal_research["coverage_complete"], False)
        self.assertIs(temporal_research["creates_offer_or_movement"], False)

        work = item["payload"]["work"]
        changed_execution = copy.deepcopy(work)
        changed_execution["brief"]["temporal_execution"]["phase"] = "exact_verification"
        changed_research = copy.deepcopy(work)
        changed_research["brief"]["evidence_contract"]["temporal_research_option"][
            "signal_kind"
        ] = "changed_signal"
        base_fingerprint = projection["execution"]["query_fingerprint"]
        for candidate in (changed_execution, changed_research):
            changed = project_work(
                candidate,
                research_kind=item["payload"]["research_kind"],
                journey_scope=item["payload"]["journey_scope"],
            )
            self.assertNotEqual(
                base_fingerprint,
                changed["execution"]["query_fingerprint"],
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

    def test_real_one_way_fingerprint_binds_every_hard_projection_semantic(self) -> None:
        item, base = self._real_one_way_item_and_projection()
        work = item["payload"]["work"]
        research_kind = item["payload"]["research_kind"]
        journey_scope = item["payload"]["journey_scope"]

        def changed(mutator: Any) -> dict[str, Any]:
            candidate = copy.deepcopy(work)
            mutator(candidate["brief"])
            return project_work(
                candidate,
                research_kind=research_kind,
                journey_scope=journey_scope,
            )

        schema_variant = copy.deepcopy(work)
        schema_variant["attempt_schema"] = {
            "type": "object",
            "required": ["changed_contract"],
            "properties": {"changed_contract": {"type": "string"}},
        }
        variants = (
            changed(lambda brief: brief.update({"task_family": "changed-family"})),
            changed(
                lambda brief: brief.update(
                    {"query_scope_ladder": {"scope_ladder": [{"kind": "changed"}]}}
                )
            ),
            changed(
                lambda brief: brief.update(
                    {"corridor_context": {"corridor_id": "changed-corridor"}}
                )
            ),
            changed(
                lambda brief: brief["provider_foundation"]["required_source"].update(
                    {"source_id": "changed-provider"}
                )
            ),
            changed(
                lambda brief: brief["traveler_requirements"].update(
                    {"traveler_count": 5}
                )
            ),
            changed(
                lambda brief: brief["traveler_requirements"].update(
                    {"baggage": {"mode": "no baggage", "paid_baggage": False}}
                )
            ),
            changed(
                lambda brief: brief["traveler_requirements"].update(
                    {"overnight_allowed": True}
                )
            ),
            changed(
                lambda brief: brief["traveler_requirements"].update(
                    {"self_transfer_policy": "never"}
                )
            ),
            changed(
                lambda brief: brief["time_window"].update(
                    {"latest_arrival": "2027-04-06T23:59:59Z"}
                )
            ),
            changed(
                lambda brief: brief["date_search"].update(
                    {"mode": "fully_flexible"}
                )
            ),
            project_work(
                schema_variant,
                research_kind=research_kind,
                journey_scope=journey_scope,
            ),
        )
        base_fingerprint = base["execution"]["query_fingerprint"]
        for variant in variants:
            self.assertNotEqual(
                base_fingerprint,
                variant["execution"]["query_fingerprint"],
            )

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
