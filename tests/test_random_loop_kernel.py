"""Product-level executable specification for the random-loop Product Kernel."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = ROOT / "product" / "random-loop"


def _load_engine() -> Any:
    path = PRODUCT_ROOT / "scripts" / "route_engine.py"
    spec = importlib.util.spec_from_file_location("test_random_loop_route_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load random-loop Product Kernel: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_engine()


AIRPORTS = {
    "AAA": {
        "code": "AAA",
        "name": "Alpha Airport",
        "country_code": "AA",
        "latitude": 0.0,
        "longitude": 0.0,
        "timezone": "UTC",
    },
    "AAB": {
        "code": "AAB",
        "name": "Alpha Second Airport",
        "country_code": "AA",
        "latitude": 1.0,
        "longitude": 0.0,
        "timezone": "UTC",
    },
    "BBB": {
        "code": "BBB",
        "name": "Beta Airport",
        "country_code": "BB",
        "latitude": 0.0,
        "longitude": 10.0,
        "timezone": "UTC",
    },
    "CCC": {
        "code": "CCC",
        "name": "Gamma Airport",
        "country_code": "CC",
        "latitude": 0.0,
        "longitude": 20.0,
        "timezone": "UTC",
    },
}


def config(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "departure_airports": ["AAA"],
        "return_airports": ["AAA"],
        "trip_start": "2026-01-01T00:00:00+00:00",
        "trip_end": "2026-01-03T00:00:00+00:00",
        "min_legs": 2,
        "max_legs": 2,
        "connection_buffer_minutes": 30,
        "max_connection_wait_minutes": 1440,
        "preferred_trip_duration_hours": 72,
        "traveler_count": 1,
        "baggage_policy": {
            "cabin_baggage": "one personal item",
            "checked_bag_count": 0,
        },
        "base_currency": "XTS",
        "fx_to_base": {"XTS": 1.0},
        "quality_gate": {
            "minimum_leg_efficiency_km_per_base_currency_unit": None,
            "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
            "target_complete_loop_efficiency_km_per_base_currency_unit": 2.0,
            "maximum_leg_price": None,
        },
    }
    value.update(copy.deepcopy(overrides))
    return value


def request(session: dict[str, Any], *, parent_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
    projected = ENGINE.build_search_requests(session)
    matches = [
        item
        for item in projected
        if (parent_id is None or item["parent_id"] == parent_id)
        and (kind is None or item["request_kind"] == kind)
    ]
    if not matches:
        raise AssertionError(
            f"no request matched parent={parent_id!r}, kind={kind!r}: "
            f"{projected!r}"
        )
    return matches[0]


def source(source_id: str, sequence: int) -> dict[str, Any]:
    return {
        "name": source_id,
        "url": f"https://example.test/{source_id}/{sequence}",
        "observed_at": f"2026-01-01T{sequence:02d}:00:00+00:00",
    }


def offer(
    origin: str,
    destination: str,
    *,
    departure: str,
    arrival: str,
    price: float,
    flight_no: str,
) -> dict[str, Any]:
    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "price": price,
        "currency": "XTS",
        "distance_km": 1000.0,
        "flight_no": flight_no,
        "booking_url": f"https://book.example.test/{flight_no}",
        "notes": [],
    }


def group(
    issued: dict[str, Any],
    *,
    query_id: str,
    observation_kind: str,
    offers: list[dict[str, Any]] | None = None,
    coverage_complete: bool = True,
    scope_partitions: list[dict[str, Any]] | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "request_id": issued["request_id"],
        "parent_id": issued["parent_id"],
        "source_id": issued["required_source_id"],
        "observation_kind": observation_kind,
        "source": source(issued["required_source_id"], sequence),
        "offers": copy.deepcopy(offers or []),
        "submitted_scope": {
            "origins": copy.deepcopy(issued["origins"]),
            "destinations": copy.deepcopy(issued["destinations"]),
            "earliest_departure": issued["earliest_departure"],
            "latest_departure": issued["latest_departure"],
        },
        "coverage_complete": coverage_complete,
        "scope_partitions": copy.deepcopy(scope_partitions or []),
        "acquisition": {"method": "browser"},
    }


def expand(session: dict[str, Any], *groups: dict[str, Any]) -> dict[str, Any]:
    return ENGINE.expand_session(session, {"search_results": list(groups)})


class RandomLoopKernelTestCase(unittest.TestCase):
    def test_ordered_dfs_preserves_sibling_and_backtracks_after_dead_end(self) -> None:
        session = ENGINE.init_session(config(max_legs=3), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="root-offers",
                observation_kind="payable",
                offers=[
                    offer(
                        "AAA",
                        "BBB",
                        departure="2026-01-01T08:00:00+00:00",
                        arrival="2026-01-01T10:00:00+00:00",
                        price=10.0,
                        flight_no="AB100",
                    ),
                    offer(
                        "AAA",
                        "CCC",
                        departure="2026-01-01T09:00:00+00:00",
                        arrival="2026-01-01T12:00:00+00:00",
                        price=30.0,
                        flight_no="AC100",
                    ),
                ],
            ),
        )

        first_id = session["ordered_dfs"]["next_candidate_id"]
        frontier = ENGINE.route_states(session, "frontier")
        self.assertEqual(
            "BBB",
            next(
                item["current_airport"] for item in frontier if item["id"] == first_id
            ),
        )
        sibling_id = next(
            item["id"] for item in frontier if item["current_airport"] == "CCC"
        )

        continuation = request(session, parent_id=first_id, kind="continuation")
        session = expand(
            session,
            group(
                continuation,
                query_id="bbb-dead-end",
                observation_kind="no_market_result",
            ),
        )

        self.assertEqual(sibling_id, session["ordered_dfs"]["next_candidate_id"])
        self.assertIn(sibling_id, set(session["frontier"]))
        self.assertGreaterEqual(session["audit"]["backtrack_count"], 1)

    def test_route_found_requires_a_complete_quality_qualified_loop(self) -> None:
        session = ENGINE.init_session(config(), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="outbound",
                observation_kind="payable",
                offers=[
                    offer(
                        "AAA",
                        "BBB",
                        departure="2026-01-01T08:00:00+00:00",
                        arrival="2026-01-01T10:00:00+00:00",
                        price=20.0,
                        flight_no="AB200",
                    )
                ],
            ),
        )
        parent_id = session["ordered_dfs"]["next_candidate_id"]
        closure = request(session, parent_id=parent_id, kind="closure")
        session = expand(
            session,
            group(
                closure,
                query_id="return",
                observation_kind="payable",
                offers=[
                    offer(
                        "BBB",
                        "AAA",
                        departure="2026-01-01T12:00:00+00:00",
                        arrival="2026-01-01T14:00:00+00:00",
                        price=20.0,
                        flight_no="BA200",
                    )
                ],
                sequence=2,
            ),
        )

        audit = ENGINE.finalization_audit(session)
        self.assertTrue(audit["can_finalize"])
        self.assertEqual("route_found", audit["outcome"])
        finalized, result = ENGINE.finalize_result(session, "Contract test passed")
        self.assertEqual("final", finalized["finalization_status"])
        self.assertEqual("route_found", result["outcome"])
        self.assertEqual(2, len(result["selected_route"]["segments"]))
        self.assertIn("search_audit", result)
        self.assertNotIn("source_order_ledger", result)

    def test_bounded_no_result_requires_exhaustion_and_complete_coverage(self) -> None:
        session = ENGINE.init_session(config(), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="azair-miss",
                observation_kind="no_market_result",
            ),
        )
        self.assertEqual("open", ENGINE.finalization_audit(session)["outcome"])
        challenger = request(session, parent_id="root", kind="initial")
        self.assertEqual("google-flights", challenger["required_source_id"])

        session = expand(
            session,
            group(
                challenger,
                query_id="google-miss",
                observation_kind="no_market_result",
                sequence=2,
            ),
        )
        audit = ENGINE.finalization_audit(session)
        self.assertTrue(audit["can_finalize"])
        self.assertEqual("bounded_no_result", audit["outcome"])
        self.assertTrue(audit["scope_coverage_audit"]["complete"])

    def test_query_budget_exhaustion_cannot_finalize_as_no_result(self) -> None:
        session = ENGINE.init_session(
            config(search_budget={"max_browser_queries": 1}), AIRPORTS
        )
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="only-query",
                observation_kind="source_unavailable",
                coverage_complete=False,
            ),
        )

        audit = ENGINE.finalization_audit(session)
        self.assertFalse(audit["can_finalize"])
        self.assertEqual("open", audit["outcome"])
        self.assertIn("incomplete_scope_coverage", audit["blockers"])
        self.assertEqual([], ENGINE.build_search_requests(session))

    def test_provider_fallback_and_exact_scope_partitioning(self) -> None:
        fallback_session = ENGINE.init_session(config(), AIRPORTS)
        root = request(fallback_session, parent_id="root", kind="initial")
        fallback_session = expand(
            fallback_session,
            group(
                root,
                query_id="azair-unavailable",
                observation_kind="source_unavailable",
                coverage_complete=False,
            ),
        )
        fallback = request(fallback_session, parent_id="root", kind="initial")
        self.assertEqual("google-flights", fallback["required_source_id"])
        self.assertNotIn("source_order_ledger", fallback_session)
        root_coverage = fallback_session["scope_coverage"]["requests"]["root:initial"]
        self.assertEqual(
            1,
            root_coverage["partitions"]["root:initial"]["starting_source_index"],
        )

        partition_session = ENGINE.init_session(
            config(departure_airports=["AAA", "AAB"]), AIRPORTS
        )
        root = request(partition_session, parent_id="root", kind="initial")
        base_scope = {
            "destinations": copy.deepcopy(root["destinations"]),
            "earliest_departure": root["earliest_departure"],
            "latest_departure": root["latest_departure"],
        }
        partitions = [
            {
                "scope": {**base_scope, "origins": ["AAA"]},
                "source_disposition": "retry_current",
            },
            {
                "scope": {**base_scope, "origins": ["AAB"]},
                "source_disposition": "advance_source",
            },
        ]
        partition_session = expand(
            partition_session,
            group(
                root,
                query_id="azair-partition",
                observation_kind="scope_unsupported",
                coverage_complete=False,
                scope_partitions=partitions,
            ),
        )
        by_origin = {
            tuple(item["origins"]): item["required_source_id"]
            for item in ENGINE.build_search_requests(partition_session)
        }
        self.assertEqual("azair", by_origin[("AAA",)])
        self.assertEqual("google-flights", by_origin[("AAB",)])

    def test_complete_loop_quality_gate_rejects_underperforming_route(self) -> None:
        strict_gate = {
            "minimum_leg_efficiency_km_per_base_currency_unit": None,
            "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1000.0,
            "target_complete_loop_efficiency_km_per_base_currency_unit": 1000.0,
            "maximum_leg_price": None,
        }
        session = ENGINE.init_session(config(quality_gate=strict_gate), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="quality-outbound",
                observation_kind="payable",
                offers=[
                    offer(
                        "AAA",
                        "BBB",
                        departure="2026-01-01T08:00:00+00:00",
                        arrival="2026-01-01T10:00:00+00:00",
                        price=20.0,
                        flight_no="AB300",
                    )
                ],
            ),
        )
        closure = request(
            session,
            parent_id=session["ordered_dfs"]["next_candidate_id"],
            kind="closure",
        )
        session = expand(
            session,
            group(
                closure,
                query_id="quality-return",
                observation_kind="payable",
                offers=[
                    offer(
                        "BBB",
                        "AAA",
                        departure="2026-01-01T12:00:00+00:00",
                        arrival="2026-01-01T14:00:00+00:00",
                        price=20.0,
                        flight_no="BA300",
                    )
                ],
                sequence=2,
            ),
        )
        self.assertFalse(session["completed"])
        self.assertIn(
            "complete_loop_efficiency_below_minimum",
            session["audit"]["rejection_reasons"],
        )

    def test_airport_transfer_gate_rejects_unknown_transfer_when_configured(self) -> None:
        session = ENGINE.init_session(
            config(airport_transit={"unknown_policy": "reject", "airside_only": False}),
            AIRPORTS,
        )
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="transfer-outbound",
                observation_kind="payable",
                offers=[
                    offer(
                        "AAA",
                        "BBB",
                        departure="2026-01-01T08:00:00+00:00",
                        arrival="2026-01-01T10:00:00+00:00",
                        price=20.0,
                        flight_no="AB400",
                    )
                ],
            ),
        )
        closure = request(
            session,
            parent_id=session["ordered_dfs"]["next_candidate_id"],
            kind="closure",
        )
        session = expand(
            session,
            group(
                closure,
                query_id="transfer-return",
                observation_kind="payable",
                offers=[
                    offer(
                        "BBB",
                        "AAA",
                        departure="2026-01-01T12:00:00+00:00",
                        arrival="2026-01-01T14:00:00+00:00",
                        price=20.0,
                        flight_no="BA400",
                    )
                ],
                sequence=2,
            ),
        )
        self.assertFalse(session["completed"])
        self.assertIn(
            "airport_transit_unknown",
            session["audit"]["rejection_reasons"],
        )

    def test_query_replay_requires_identical_content(self) -> None:
        session = ENGINE.init_session(config(), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        original = group(
            root,
            query_id="immutable-query",
            observation_kind="payable",
            offers=[
                offer(
                    "AAA",
                    "BBB",
                    departure="2026-01-01T08:00:00+00:00",
                    arrival="2026-01-01T10:00:00+00:00",
                    price=20.0,
                    flight_no="AB500",
                )
            ],
        )
        admitted = expand(session, original)
        replayed = expand(admitted, copy.deepcopy(original))
        self.assertEqual(admitted, replayed)

        changed = copy.deepcopy(original)
        changed["offers"][0]["price"] = 21.0
        with self.assertRaisesRegex(ENGINE.RouteEngineError, "different content"):
            expand(admitted, changed)

    def test_session_stores_each_route_segment_once(self) -> None:
        session = ENGINE.init_session(config(max_legs=3), AIRPORTS)
        root = request(session, parent_id="root", kind="initial")
        session = expand(
            session,
            group(
                root,
                query_id="compact-routes",
                observation_kind="payable",
                offers=[
                    offer(
                        "AAA",
                        "BBB",
                        departure="2026-01-01T08:00:00+00:00",
                        arrival="2026-01-01T10:00:00+00:00",
                        price=10.0,
                        flight_no="AB600",
                    ),
                    offer(
                        "AAA",
                        "CCC",
                        departure="2026-01-01T09:00:00+00:00",
                        arrival="2026-01-01T12:00:00+00:00",
                        price=30.0,
                        flight_no="AC600",
                    ),
                ],
            ),
        )

        self.assertTrue(all(isinstance(value, str) for value in session["frontier"]))
        self.assertEqual(3, len(session["route_nodes"]))
        for identifier, node in session["route_nodes"].items():
            self.assertNotIn("segments", node)
            self.assertNotIn("total_price", node)
            self.assertEqual(identifier, node["route_id"])
        for node in session["ordered_dfs"]["nodes"].values():
            self.assertNotIn("total_price", node)
            self.assertNotIn("arrival", node)
            self.assertNotIn(
                "cumulative_efficiency_km_per_base_currency_unit", node
            )


if __name__ == "__main__":
    unittest.main()
