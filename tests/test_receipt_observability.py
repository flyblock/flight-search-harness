"""Receipt identity and optional-observability transaction specifications."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from flight_search_harness.contracts import HarnessProtocolError, REQUEST_VERSION
from flight_search_harness.mission_contracts import complete, need_work
from flight_search_harness.mission_runner import SearchMissionHarness
from flight_search_harness.mission_store import MissionStore
from flight_search_harness.run_store import RunStore


PRODUCT_ID = "random_loop"


def _public_request() -> dict[str, Any]:
    return {
        "request_version": REQUEST_VERSION,
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
                "start": "2027-01-01T00:00:00+00:00",
                "end": "2027-01-03T00:00:00+00:00",
            },
            "maximum_total_duration_minutes": 2880,
            "min_legs": 2,
            "max_legs": 3,
            "connection_buffer_minutes": 30,
            "max_connection_wait_minutes": 1440,
            "quality_gate": {
                "minimum_complete_loop_efficiency_km_per_base_currency_unit": 1.0,
                "target_complete_loop_efficiency_km_per_base_currency_unit": 2.0,
            },
        },
    }


def _work_item() -> dict[str, Any]:
    return {
        "item_id": "research-a",
        "kind": "browser_research",
        "payload": {
            "task_id": "research-a",
            "research_kind": "contract_test",
            "action": {"action_id": "action-research-a"},
            "work": {
                "work_version": "test",
                "product_id": PRODUCT_ID,
                "task_id": "research-a",
                "brief": {
                    "research_kind": "contract_test",
                    "question": "Research a fare",
                    "request": {
                        "origins": ["AAA"],
                        "destinations": ["BBB"],
                        "traveler_count": 1,
                        "baggage_policy": {"checked_bag_count": 0},
                    },
                    "provider_foundation": {
                        "required_source": {"source_id": "provider-a"}
                    },
                },
            },
        },
    }


def _receipt(lease: dict[str, Any], *, amount: int = 10) -> dict[str, Any]:
    item = lease["items"][0]
    return {
        "receipt_version": "2.0",
        "item_id": item["item_id"],
        "attempt_number": item["attempt_number"],
        "status": "complete",
        "payload": {
            "kind": "observations",
            "product_payload": {"offers": [{"amount": amount}]},
        },
    }


class _AdmissionKernel:
    product_id = PRODUCT_ID
    journey_shape = "random_loop"

    def __init__(self) -> None:
        self.admission_count = 0

    def start(
        self, requested: dict[str, Any], product_directory: Path
    ) -> dict[str, Any]:
        product_directory.mkdir(parents=True, exist_ok=True)
        return {"effect": need_work([_work_item()], reason="Need Evidence")}

    def advance(
        self,
        product_directory: Path,
        inbound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if inbound is None:
            return {"effect": need_work([_work_item()], reason="Need Evidence")}
        self.admission_count += 1
        identifier = inbound["entries"][0]["receipt"]["item_id"]
        return {
            "receipt_results": [{"item_id": identifier, "status": "committed"}],
            "effect": complete(
                {
                    "status": "complete",
                    "outcome": "route_found",
                    "selected_route": {"route_id": "route-a", "segments": []},
                    "finalization_audit": {"passed": True},
                }
            ),
        }


class _ReceiptWorker:
    worker_instance_id = "receipt-worker-instance"

    def run(
        self,
        lease: dict[str, Any],
        checkpoint: Callable[[dict[str, Any]], dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return checkpoint(
            {
                "lease_id": lease["lease_id"],
                "mission_revision": lease["mission_revision"],
                "claim_id": lease["claim"]["claim_id"],
                "worker_instance_id": self.worker_instance_id,
                "receipts": [_receipt(lease)],
                "metrics": {
                    "worker_calls": 1,
                    "provider_query_count": 1,
                    "items": {"research-a": {"worker_duration_ms": 5}},
                },
            }
        )


class ReceiptAndMetricsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.run_store = RunStore(self.temporary_directory.name)
        manifest = self.run_store.create(
            {"journey_shape": "random_loop"}, PRODUCT_ID, run_id="receipt-contract"
        )
        self.missions = MissionStore(self.run_store)
        self.mission = self.missions.create(manifest)
        self.mission, self.lease = self.missions.publish_lease(
            self.mission, [_work_item()]
        )

    def test_metric_checkpoint_replay_is_idempotent_and_conflicts_are_rejected(self) -> None:
        metrics = {
            "worker_calls": 1,
            "provider_query_count": 2,
            "items": {"research-a": {"worker_duration_ms": 5}},
        }
        self.missions.record_metrics(self.mission, metrics, checkpoint_id="checkpoint-a")
        self.missions.record_metrics(
            self.mission, copy.deepcopy(metrics), checkpoint_id="checkpoint-a"
        )
        aggregate = self.run_store.metrics("receipt-contract")["mission_metrics"]
        self.assertEqual(1, aggregate["worker_calls"])
        self.assertEqual(2, aggregate["provider_query_count"])
        self.assertEqual(5, aggregate["items"]["research-a"]["worker_duration_ms"])

        conflict = copy.deepcopy(metrics)
        conflict["provider_query_count"] = 3
        with self.assertRaisesRegex(HarnessProtocolError, "checkpoint.*collision"):
            self.missions.record_metrics(
                self.mission, conflict, checkpoint_id="checkpoint-a"
            )

    def test_torn_final_event_is_ignored_and_repaired_on_next_append(self) -> None:
        target = self.run_store.run_directory("receipt-contract") / "events.jsonl"
        with target.open("a", encoding="utf-8") as stream:
            stream.write('{"event_version":"1.0","sequence":')

        self.assertEqual(1, len(self.run_store.read_events("receipt-contract")))
        manifest = self.run_store.load_manifest("receipt-contract")
        self.run_store.append_event(manifest, "after_torn_tail")

        events = self.run_store.read_events("receipt-contract")
        self.assertEqual([1, 2], [event["sequence"] for event in events])
        self.assertEqual("after_torn_tail", events[-1]["type"])
        self.run_store.metrics("receipt-contract")


class OptionalArtifactFailureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    def _run(
        self,
        run_id: str,
        install_failure: Callable[[SearchMissionHarness], Any],
    ) -> tuple[dict[str, Any], _AdmissionKernel]:
        store = RunStore(self.temporary_directory.name)
        kernel = _AdmissionKernel()
        harness = SearchMissionHarness(store, {"random_loop": kernel}, lease_ttl_seconds=60)
        with install_failure(harness):
            outcome = harness.run(
                {
                    "kind": "start",
                    "run_id": run_id,
                    "request": _public_request(),
                },
                _ReceiptWorker(),
            )
        return outcome, kernel

    def test_metrics_write_failure_does_not_change_receipt_admission(self) -> None:
        outcome, kernel = self._run(
            "optional-metrics",
            lambda harness: patch.object(
                harness.missions,
                "record_metrics",
                side_effect=OSError("metrics unavailable"),
            ),
        )
        self.assertEqual(1, kernel.admission_count)
        self.assertEqual("completed", outcome["kind"])

    def test_cache_write_failure_does_not_change_receipt_admission(self) -> None:
        outcome, kernel = self._run(
            "optional-cache",
            lambda harness: patch(
                "flight_search_harness.mission_runner.ResearchCache.put",
                side_effect=OSError("cache unavailable"),
            ),
        )
        self.assertEqual(1, kernel.admission_count)
        self.assertEqual("completed", outcome["kind"])

    def test_summary_write_failure_does_not_change_completion(self) -> None:
        outcome, kernel = self._run(
            "optional-summary",
            lambda harness: patch.object(
                harness.store,
                "write_result_summary",
                side_effect=OSError("summary unavailable"),
            ),
        )
        self.assertEqual(1, kernel.admission_count)
        self.assertEqual("completed", outcome["kind"])
        self.assertNotIn("result_summary_ref", outcome)

    def test_completed_resume_rebuilds_summary_from_product_final(self) -> None:
        store = RunStore(self.temporary_directory.name)
        kernel = _AdmissionKernel()
        harness = SearchMissionHarness(
            store, {"random_loop": kernel}, lease_ttl_seconds=60
        )
        first = harness.run(
            {
                "kind": "start",
                "run_id": "summary-rebuild",
                "request": _public_request(),
            },
            _ReceiptWorker(),
        )
        summary_path = store.run_directory("summary-rebuild") / "result/summary.json"
        summary_path.write_text('{"tampered": true}\n', encoding="utf-8")

        resumed = harness.run(
            {"kind": "resume", "run_id": "summary-rebuild"},
            _ReceiptWorker(),
        )

        self.assertEqual("completed", resumed["kind"])
        self.assertEqual(first["result"], resumed["result"])
        self.assertNotIn("tampered", resumed["result"])


if __name__ == "__main__":
    unittest.main()
