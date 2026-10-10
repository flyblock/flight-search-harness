"""Real start-to-Outcome acceptance paths for all three Product Kernels."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from flight_search_harness.adapters import (
    OneWayProductAdapter,
    RandomLoopProductAdapter,
    RoundTripProductAdapter,
)
from flight_search_harness.mission_runner import SearchMissionHarness
from flight_search_harness.mission_workers import ExternalMissionWorker
from flight_search_harness.product_kernel import AdapterProductKernel
from flight_search_harness.run_store import RunStore


def _node(identifier: str, node_type: str = "airport") -> dict[str, Any]:
    return {
        "node_id": identifier,
        "node_type": node_type,
        "location": identifier,
        "country_or_region": "Testland",
        "country_code": "XT",
        "timezone": "Etc/UTC",
        "utc_offset": "+00:00",
    }


def _side(region_id: str, airport: str) -> dict[str, Any]:
    return {
        "region_id": region_id,
        "label": region_id,
        "country_codes": ["XT"],
        "policy": "exact",
        "endpoint_policy": "anchor_required",
        "anchor_nodes": [_node(airport)],
        "seed_gateway_nodes": [],
        "max_ground_cost": None,
    }


def _shared() -> dict[str, Any]:
    return {
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
    }


class FullStackAcceptanceTests(unittest.TestCase):
    def test_real_products_reach_a_persisted_external_worker_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            airport_catalog = root / "airports.json"
            airport_catalog.write_text(
                json.dumps(
                    {
                        "airports": {
                            code: {
                                "code": code,
                                "name": code,
                                "country_code": "XT",
                                "latitude": float(index),
                                "longitude": float(index),
                                "timezone": "UTC",
                            }
                            for index, code in enumerate(("AAA", "BBB"), 1)
                        }
                    }
                ),
                encoding="utf-8",
            )
            cases = {
                "one_way": {
                    "request_version": "2.0",
                    "journey_shape": "one_way",
                    "intent": {
                        **_shared(),
                        "origin": _side("origin", "AAA"),
                        "destination": _side("destination", "BBB"),
                        "departure_window": {
                            "start": "2027-03-01",
                            "end": "2027-03-05",
                        },
                    },
                },
                "round_trip": {
                    "request_version": "2.0",
                    "journey_shape": "round_trip",
                    "intent": {
                        **_shared(),
                        "origin": _side("origin", "AAA"),
                        "destination": _side("destination", "BBB"),
                        "outbound_departure_window": {
                            "start": "2027-03-01T00:00:00+00:00",
                            "end": "2027-03-03T23:59:00+00:00",
                        },
                        "return_departure_window": {
                            "start": "2027-03-08T00:00:00+00:00",
                            "end": "2027-03-10T23:59:00+00:00",
                        },
                    },
                },
                "random_loop": {
                    "request_version": "2.0",
                    "journey_shape": "random_loop",
                    "intent": {
                        **_shared(),
                        "departure_airports": ["AAA"],
                        "return_airports": ["AAA"],
                        "first_departure_window": {
                            "start": "2027-03-01T00:00:00+00:00",
                            "end": "2027-03-03T00:00:00+00:00",
                        },
                        "maximum_total_duration_minutes": 2880,
                        "min_legs": 2,
                        "max_legs": 3,
                        "connection_buffer_minutes": 30,
                        "max_connection_wait_minutes": 1440,
                        "airport_catalog_ref": str(airport_catalog),
                        "quality_gate": {
                            "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
                            "target_complete_loop_efficiency_km_per_base_currency_unit": 2.0,
                        },
                    },
                },
            }
            adapters = (
                OneWayProductAdapter(),
                RoundTripProductAdapter(),
                RandomLoopProductAdapter(),
            )
            store = RunStore(root / "runs")
            harness = SearchMissionHarness(
                store,
                {
                    adapter.journey_shape: AdapterProductKernel(adapter)
                    for adapter in adapters
                },
            )
            for shape, request in cases.items():
                with self.subTest(shape=shape):
                    run_id = f"full-stack-{shape.replace('_', '-')}"
                    outcome = harness.run(
                        {"kind": "start", "run_id": run_id, "request": request},
                        ExternalMissionWorker(),
                    )
                    self.assertEqual("blocked", outcome["kind"])
                    self.assertEqual("execute_mission_lease", outcome["action_required"]["type"])
                    self.assertEqual("blocked", store.load_manifest(run_id)["status"])
                    self.assertTrue((store.product_directory(run_id) / "session.json").is_file())
                    active = harness.missions.active_lease(
                        harness.missions.load(run_id)
                    )
                    self.assertTrue(active and active["items"])


if __name__ == "__main__":
    unittest.main()
