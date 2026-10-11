"""Executable specification for public request normalization at the Adapter Seam."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from flight_search_harness.adapters.one_way import OneWayProductAdapter
from flight_search_harness.adapters.random_loop import RandomLoopProductAdapter
from flight_search_harness.adapters.round_trip import RoundTripProductAdapter
from flight_search_harness.contracts import HarnessProtocolError
from flight_search_harness.mission_runner import SearchMissionHarness
from flight_search_harness.request_values import validate_public_request
from flight_search_harness.run_store import RunStore


def node(node_id: str, *, location: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "airport",
        "location": location,
        "country_or_region": "Testland",
        "country_code": "XT",
        "timezone": "Etc/UTC",
        "utc_offset": "+00:00",
    }


def region(region_id: str, airport: dict[str, Any]) -> dict[str, Any]:
    return {
        "region_id": region_id,
        "label": region_id.replace("-", " ").title(),
        "country_codes": ["XT"],
        "policy": "exact",
        "endpoint_policy": "anchor_required",
        "anchor_nodes": [airport],
        "seed_gateway_nodes": [],
        "max_ground_cost": None,
    }


def regional_side(
    region_id: str,
    city_id: str,
    airport: dict[str, Any],
) -> dict[str, Any]:
    city = {
        **node(city_id, location=region_id.replace("-", " ").title()),
        "node_type": "city_destination",
    }
    return {
        "region_id": region_id,
        "label": region_id.replace("-", " ").title(),
        "country_codes": ["XT"],
        "policy": "extended_region",
        "endpoint_policy": "anchor_required",
        "anchor_nodes": [city],
        "seed_gateway_nodes": [airport],
        "max_ground_cost": None,
    }


def shared_intent() -> dict[str, Any]:
    return {
        "base_currency": "XTS",
        "adult_count": 2,
        "baggage_policy": {
            "cabin_baggage": "none_required",
            "checked_bag_count": 0,
            "paid_baggage_allowed": True,
        },
        "overnight_allowed": True,
        "self_transfer_policy": "allow",
    }


def public_one_way_request() -> dict[str, Any]:
    return {
        "request_version": "4.0",
        "journey_shape": "one_way",
        "intent": {
            **shared_intent(),
            "origin": region("origin-region", node("AAA", location="Alpha Airport")),
            "destination": region(
                "destination-region", node("BBB", location="Beta Airport")
            ),
            "departure_window": {
                "start": "2027-03-10",
                "end": "2027-03-20",
            },
        },
    }


def public_random_loop_request() -> dict[str, Any]:
    return {
        "request_version": "4.0",
        "journey_shape": "random_loop",
        "intent": {
            **shared_intent(),
            "departure_airports": ["AAA"],
            "return_airports": ["AAA"],
            "first_departure_window": {
                "start": "2027-04-01T00:00:00+00:00",
                "end": "2027-04-03T00:00:00+00:00",
            },
            "maximum_total_duration_minutes": 4320,
            "min_legs": 2,
            "max_legs": 3,
            "connection_buffer_minutes": 30,
            "max_connection_wait_minutes": 1440,
            "quality_gate": {
                "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
            },
        },
    }


def public_round_trip_request() -> dict[str, Any]:
    return {
        "request_version": "4.0",
        "journey_shape": "round_trip",
        "intent": {
            **shared_intent(),
            "origin": region("origin-region", node("AAA", location="Alpha Airport")),
            "destination": region(
                "destination-region", node("BBB", location="Beta Airport")
            ),
            "outbound_departure_window": {
                "start": "2027-06-01",
                "end": "2027-06-03",
            },
            "return_departure_window": {
                "start": "2027-06-08",
                "end": "2027-06-10",
            },
        },
    }


class _UnreachableKernel:
    product_id = "contract-probe-product"

    def start(self, request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        raise AssertionError("invalid request reached Product Kernel")

    def advance(
        self, product_directory: Path, inbound: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise AssertionError("invalid request reached Product Kernel")


class _UnreachableWorker:
    worker_instance_id = "contract-probe-worker"

    def run(self, lease: dict[str, Any], checkpoint: Any, context: dict[str, Any]) -> Any:
        raise AssertionError("invalid request reached Worker")


class RequestAdapterNormalizationTests(unittest.TestCase):
    def test_cross_field_contract_failures_are_rejected_before_run_creation(self) -> None:
        invalid_requests: list[dict[str, Any]] = []

        equal_window = public_one_way_request()
        equal_window["intent"]["departure_window"] = {
            "start": "2027-03-10",
            "end": "2027-03-10",
        }
        invalid_requests.append(equal_window)

        exact_outside = public_one_way_request()
        exact_outside["intent"]["exact_departure_dates"] = ["2027-03-21"]
        invalid_requests.append(exact_outside)

        missing_ground_modes = public_one_way_request()
        missing_ground_modes["intent"]["allowed_modes"] = ["flight"]
        invalid_requests.append(missing_ground_modes)

        invalid_wait = public_random_loop_request()
        invalid_wait["intent"]["max_connection_wait_minutes"] = 30
        invalid_requests.append(invalid_wait)

        legacy_baggage_text = public_one_way_request()
        legacy_baggage_text["intent"]["baggage_policy"]["cabin_baggage"] = (
            "not_required"
        )
        invalid_requests.append(legacy_baggage_text)

        unsupported_one_way_baggage = public_one_way_request()
        unsupported_one_way_baggage["intent"]["baggage_policy"][
            "cabin_baggage"
        ] = "personal_item"
        invalid_requests.append(unsupported_one_way_baggage)

        ineffective_transfer_preference = public_one_way_request()
        ineffective_transfer_preference["intent"]["self_transfer_policy"] = "avoid"
        invalid_requests.append(ineffective_transfer_preference)

        missing_outbound_offset = public_round_trip_request()
        missing_outbound_offset["intent"]["origin"]["anchor_nodes"][0].pop(
            "utc_offset"
        )
        invalid_requests.append(missing_outbound_offset)

        missing_return_offset = public_round_trip_request()
        missing_return_offset["intent"]["destination"]["anchor_nodes"][0].pop(
            "utc_offset"
        )
        invalid_requests.append(missing_return_offset)

        with tempfile.TemporaryDirectory() as raw_directory:
            store = RunStore(raw_directory)
            kernel = _UnreachableKernel()
            harness = SearchMissionHarness(
                store,
                {
                    "one_way": kernel,
                    "random_loop": kernel,
                    "round_trip": kernel,
                },
            )
            for index, invalid in enumerate(invalid_requests):
                run_id = f"invalid-contract-{index}"
                with self.subTest(index=index):
                    outcome = harness.run(
                        {"kind": "start", "run_id": run_id, "request": invalid},
                        _UnreachableWorker(),
                    )
                    self.assertEqual("failed", outcome["kind"])
                    self.assertFalse(store.run_directory(run_id).exists())

    def test_one_way_canonical_request_preserves_shared_values(self) -> None:
        intent = {
            **shared_intent(),
            "origin": regional_side(
                "origin-region", "alpha-city", node("AAA", location="Alpha Airport")
            ),
            "destination": regional_side(
                "destination-region", "beta-city", node("BBB", location="Beta Airport")
            ),
            "departure_window": {
                "start": "2027-03-10",
                "end": "2027-03-20",
            },
            "maximum_total_duration_minutes": 2880,
        }
        adapter = OneWayProductAdapter()
        with tempfile.TemporaryDirectory() as raw_directory:
            state = adapter.initialize(
                {
                    "request_version": "4.0",
                    "journey_shape": "one_way",
                    "intent": intent,
                },
                Path(raw_directory),
            )

        profile = state["profile"]
        self.assertEqual(2, profile["traveler_count"])
        self.assertEqual(
            {
                "mode": "none_required",
                "paid_baggage": True,
            },
            profile["baggage"],
        )
        self.assertEqual("XTS", profile["currency"])
        self.assertEqual(["ALPHA-CITY"], profile["origin_start_nodes"])
        self.assertEqual(["BETA-CITY"], profile["destination_end_nodes"])
        self.assertEqual(
            "2027-03-10T00:00:00Z", profile["date_window"]["start"]
        )
        self.assertEqual("one_way", profile["trip_scope"])
        self.assertEqual(2880, state["config"]["search"]["max_total_travel_minutes"])
        connector_pairs = {
            (task.get("from_node"), task.get("to_node"))
            for task in state["research_questions"]
            if task.get("request_type") == "unresolved_connector"
        }
        self.assertIn(("ALPHA-CITY", "AAA"), connector_pairs)
        self.assertIn(("BBB", "BETA-CITY"), connector_pairs)

    def test_random_loop_canonical_request_preserves_shared_values(self) -> None:
        intent = {
            **shared_intent(),
            "departure_airports": ["AAA"],
            "return_airports": ["AAA"],
            "first_departure_window": {
                "start": "2027-04-01T00:00:00+00:00",
                "end": "2027-04-03T00:00:00+00:00",
            },
            "maximum_total_duration_minutes": 4320,
            "min_legs": 2,
            "max_legs": 3,
            "connection_buffer_minutes": 30,
            "max_connection_wait_minutes": 1440,
            "quality_gate": {
                "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
                "maximum_leg_price_in_base_currency": 250.0,
            },
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            catalog_path = directory / "airports.json"
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
                    "intent": intent,
                },
                directory,
            )

        config = state["config"]
        self.assertEqual(2, config["traveler_count"])
        self.assertEqual(
            {"cabin_baggage": "none_required", "checked_bag_count": 0},
            config["baggage_policy"],
        )
        self.assertEqual("XTS", config["base_currency"])
        self.assertEqual("2027-04-01T00:00+00:00", config["trip_start"])
        self.assertEqual("2027-04-03T00:00+00:00", config["trip_end"])
        self.assertEqual(72.0, config["preferred_trip_duration_hours"])
        self.assertEqual(250.0, config["quality_gate"]["maximum_leg_price"])
        self.assertEqual(
            "XTS", config["quality_gate"]["maximum_leg_price_currency"]
        )

    def test_round_trip_canonical_request_preserves_shared_values(self) -> None:
        intent = {
            **shared_intent(),
            "baggage_policy": {
                "cabin_baggage": "personal_item",
                "checked_bag_count": 1,
                "paid_baggage_allowed": True,
            },
            "origin": region("origin-region", node("AAA", location="Alpha Airport")),
            "destination": region(
                "destination-region", node("BBB", location="Beta Airport")
            ),
            "outbound_departure_window": {
                "start": "2027-06-01T00:00:00+00:00",
                "end": "2027-06-03T23:59:00+00:00",
            },
            "return_departure_window": {
                "start": "2027-06-08T00:00:00+00:00",
                "end": "2027-06-10T23:59:00+00:00",
            },
            "maximum_outbound_duration_minutes": 1440,
            "maximum_return_duration_minutes": 1800,
            "outbound_exact_departure_dates": ["2027-06-02"],
        }
        adapter = RoundTripProductAdapter()
        with tempfile.TemporaryDirectory() as raw_directory:
            state = adapter.initialize(
                {
                    "request_version": "4.0",
                    "journey_shape": "round_trip",
                    "intent": intent,
                },
                Path(raw_directory),
            )

        scenario = state["scenario"]
        self.assertEqual("XTS", scenario["base_currency"])
        self.assertEqual(2, scenario["intent"]["traveler_count"])
        self.assertEqual(
            {
                "required_items": ["personal_item", "1 checked bag"],
                "paid_baggage_allowed": True,
            },
            scenario["intent"]["baggage_policy"],
        )
        self.assertEqual(
            "2027-06-01T00:00:00+00:00",
            scenario["intent"]["outbound_departure_window"]["start"],
        )
        self.assertEqual(
            "2027-06-10T23:59:00+00:00",
            scenario["intent"]["return_departure_window"]["end"],
        )
        self.assertEqual(
            "independent", scenario["intent"]["date_flexibility"]["mode"]
        )
        self.assertEqual(
            ["2027-06-02"],
            scenario["intent"]["date_flexibility"][
                "outbound_exact_departure_dates"
            ],
        )
        self.assertEqual(
            1440, scenario["intent"]["maximum_outbound_duration_minutes"]
        )
        self.assertEqual(
            1800, scenario["intent"]["maximum_return_duration_minutes"]
        )

if __name__ == "__main__":
    unittest.main()

