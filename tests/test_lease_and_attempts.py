"""Worker identity, Lease fencing, and Work Attempt specifications."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from flight_search_harness.contracts import HarnessProtocolError
from flight_search_harness.mission_runner import SearchMissionHarness
from flight_search_harness.mission_store import MissionStore
from flight_search_harness.mission_workers import (
    BrowserMissionWorker,
    ExternalMissionWorker,
)
from flight_search_harness.run_store import RunBusyError, RunStore


PRODUCT_ID = "random_loop"


class _BrowserAdapter:
    adapter_id = "contract-browser"


class _NeverAdmissionKernel:
    product_id = PRODUCT_ID
    journey_shape = "random_loop"

    def advance(self, product_directory: Any, inbound: Any = None) -> dict[str, Any]:
        raise AssertionError("stale owner reached Product Kernel admission")


def _work_item(identifier: str = "research-a") -> dict[str, Any]:
    return {
        "item_id": identifier,
        "kind": "browser_research",
        "payload": {
            "task_id": identifier,
            "research_kind": "contract_test",
            "action": {"action_id": f"action-{identifier}"},
            "work": {
                "work_version": "test",
                "product_id": PRODUCT_ID,
                "task_id": identifier,
                "brief": {
                    "research_kind": "contract_test",
                    "question": f"Research {identifier}",
                },
            },
        },
    }


def _receipt(
    lease: dict[str, Any], *, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    item = lease["items"][0]
    return {
        "receipt_version": "2.0",
        "item_id": item["item_id"],
        "attempt_number": item["attempt_number"],
        "status": "complete",
        "payload": copy.deepcopy(payload or {"observation": item["item_id"]}),
    }


class WorkerIdentityTestCase(unittest.TestCase):
    def test_default_workers_have_unique_instance_ids(self) -> None:
        for first, second in (
            (BrowserMissionWorker(_BrowserAdapter()), BrowserMissionWorker(_BrowserAdapter())),
            (ExternalMissionWorker(), ExternalMissionWorker()),
        ):
            self.assertNotEqual(first.worker_instance_id, second.worker_instance_id)

    def test_external_handoff_exposes_the_active_claim_fence(self) -> None:
        worker = ExternalMissionWorker(
            worker_instance_id="external:test-instance"
        )
        lease = {
            "lease_id": "lease-000001",
            "mission_revision": 0,
            "claim": {
                "claim_id": "claim-a",
                "worker_instance_id": worker.worker_instance_id,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "items": [],
        }
        outcome = worker.run(
            lease,
            lambda _: self.fail("empty external handoff must not checkpoint"),
            {"lease_ref": "mission/leases/lease-000001.json"},
        )
        required = outcome["action_required"]
        self.assertEqual("claim-a", required["claim_id"])
        self.assertEqual(worker.worker_instance_id, required["worker_instance_id"])

    def test_external_worker_binds_compact_receipts_to_current_lease(self) -> None:
        worker = ExternalMissionWorker(
            {
                "receipts": [
                    {
                        "item_id": "research-a",
                        "status": "complete",
                        "payload": {"observation": "current"},
                    }
                ],
                "metrics": {"provider_query_count": 1},
            },
            worker_instance_id="external:test-instance",
        )
        lease = {
            "lease_id": "lease-000007",
            "mission_revision": 4,
            "claim": {
                "claim_id": "claim-current",
                "worker_instance_id": worker.worker_instance_id,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "items": [{"item_id": "research-a", "attempt_number": 3, "payload": {}}],
        }
        captured: list[dict[str, Any]] = []

        result = worker.run(
            lease,
            lambda batch: captured.append(copy.deepcopy(batch)) or {"kind": "draft"},
            {},
        )

        self.assertEqual({"kind": "draft"}, result)
        batch = captured[0]
        self.assertEqual("lease-000007", batch["lease_id"])
        self.assertEqual(4, batch["mission_revision"])
        self.assertEqual("claim-current", batch["claim_id"])
        self.assertEqual("external:test-instance", batch["worker_instance_id"])
        self.assertEqual("2.0", batch["receipts"][0]["receipt_version"])
        self.assertEqual(3, batch["receipts"][0]["attempt_number"])
        self.assertEqual(1, batch["metrics"]["provider_query_count"])

    def test_external_worker_preserves_matching_full_batch(self) -> None:
        lease = {
            "lease_id": "lease-000002",
            "mission_revision": 1,
            "claim": {
                "claim_id": "claim-a",
                "worker_instance_id": "external:test-instance",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "items": [{"item_id": "research-a", "attempt_number": 2, "payload": {}}],
        }
        full = {
            "lease_id": "lease-000002",
            "mission_revision": 1,
            "claim_id": "claim-a",
            "worker_instance_id": "external:test-instance",
            "receipts": [
                {
                    "receipt_version": "2.0",
                    "item_id": "research-a",
                    "attempt_number": 2,
                    "status": "complete",
                    "payload": {"observation": "legacy-full-batch"},
                }
            ],
        }
        captured: list[dict[str, Any]] = []
        worker = ExternalMissionWorker(full)

        worker.run(
            lease,
            lambda batch: captured.append(copy.deepcopy(batch)) or {"kind": "draft"},
            {},
        )

        for field in ("lease_id", "mission_revision", "claim_id", "worker_instance_id"):
            self.assertEqual(full[field], captured[0][field])
        self.assertEqual(full["receipts"], captured[0]["receipts"])

    def test_external_worker_rejects_unknown_duplicate_and_conflicting_receipts(self) -> None:
        lease = {
            "lease_id": "lease-000003",
            "mission_revision": 2,
            "claim": {
                "claim_id": "claim-a",
                "worker_instance_id": "external:test-instance",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "items": [{"item_id": "research-a", "attempt_number": 2, "payload": {}}],
        }
        cases = {
            "unknown pending Work Item": {
                "receipts": [
                    {"item_id": "research-b", "status": "complete", "payload": {}}
                ]
            },
            "duplicate Work Item": {
                "receipts": [
                    {"item_id": "research-a", "status": "complete", "payload": {}},
                    {"item_id": "research-a", "status": "complete", "payload": {}},
                ]
            },
            "conflicting mission_revision": {
                "mission_revision": 99,
                "receipts": [
                    {"item_id": "research-a", "status": "complete", "payload": {}}
                ],
            },
            "conflicting attempt_number": {
                "receipts": [
                    {
                        "item_id": "research-a",
                        "attempt_number": 1,
                        "status": "complete",
                        "payload": {},
                    }
                ]
            },
        }
        for message, submission in cases.items():
            with self.subTest(message=message):
                worker = ExternalMissionWorker(
                    submission, worker_instance_id="external:test-instance"
                )
                with self.assertRaisesRegex(HarnessProtocolError, message):
                    worker.run(lease, lambda _: {"kind": "draft"}, {})


class LeaseAndAttemptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.run_store = RunStore(self.temporary_directory.name)
        manifest = self.run_store.create(
            {"journey_shape": "random_loop"}, PRODUCT_ID, run_id="lease-contract"
        )
        self.missions = MissionStore(self.run_store)
        self.mission = self.missions.create(manifest)
        self.item = _work_item()
        self.mission, self.lease = self.missions.publish_lease(
            self.mission, [self.item]
        )

    def _expire(self, lease: dict[str, Any]) -> None:
        lease["claim"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat(timespec="seconds")
        self.missions._write_lease(self.mission, lease)

    def test_live_owner_blocks_takeover_but_expired_owner_can_be_replaced(self) -> None:
        claimed = self.missions.claim_lease(
            self.mission, "worker-instance-a", ttl_seconds=60
        )
        with self.assertRaises(RunBusyError):
            self.missions.claim_lease(
                self.missions.load("lease-contract"),
                "worker-instance-b",
                ttl_seconds=60,
            )
        first_claim_id = claimed["claim"]["claim_id"]
        self._expire(claimed)
        taken_over = self.missions.claim_lease(
            self.missions.load("lease-contract"),
            "worker-instance-b",
            ttl_seconds=60,
        )
        self.assertEqual(
            "worker-instance-b", taken_over["claim"]["worker_instance_id"]
        )
        with self.assertRaisesRegex(HarnessProtocolError, "claim mismatch"):
            self.missions.heartbeat_lease(
                self.missions.load("lease-contract"),
                "worker-instance-a",
                first_claim_id,
                ttl_seconds=60,
            )
        with self.assertRaisesRegex(HarnessProtocolError, "claim mismatch"):
            self.missions.release_lease(
                self.missions.load("lease-contract"),
                "worker-instance-a",
                first_claim_id,
            )

        harness = SearchMissionHarness(
            self.run_store,
            {"random_loop": _NeverAdmissionKernel()},
            lease_ttl_seconds=60,
        )
        stale_batch = {
            "lease_id": self.lease["lease_id"],
            "mission_revision": self.lease["mission_revision"],
            "claim_id": first_claim_id,
            "worker_instance_id": "worker-instance-a",
            "receipts": [_receipt(self.lease)],
        }
        with self.assertRaisesRegex(HarnessProtocolError, "claim.*own"):
            harness._checkpoint(
                self.run_store.load_manifest("lease-contract"),
                self.missions.load("lease-contract"),
                _NeverAdmissionKernel(),
                stale_batch,
                worker_instance_id="worker-instance-a",
            )

    def test_takeover_rotates_claim_fence(self) -> None:
        claimed = self.missions.claim_lease(
            self.mission, "worker-instance-a", ttl_seconds=60
        )
        first_claim_id = claimed["claim"]["claim_id"]
        self._expire(claimed)
        taken_over = self.missions.claim_lease(
            self.missions.load("lease-contract"),
            "worker-instance-b",
            ttl_seconds=60,
        )
        self.assertNotEqual(first_claim_id, taken_over["claim"]["claim_id"])

    def test_same_worker_instance_cannot_use_an_expired_claim_fence(self) -> None:
        first = self.missions.claim_lease(
            self.mission, "worker-instance-a", ttl_seconds=60
        )
        first_claim_id = first["claim"]["claim_id"]
        self._expire(first)
        replacement = self.missions.claim_lease(
            self.missions.load("lease-contract"),
            "worker-instance-a",
            ttl_seconds=60,
        )
        self.assertNotEqual(first_claim_id, replacement["claim"]["claim_id"])
        with self.assertRaisesRegex(HarnessProtocolError, "claim mismatch"):
            self.missions.release_lease(
                self.missions.load("lease-contract"),
                "worker-instance-a",
                first_claim_id,
            )

    def test_compact_receipt_binds_replacement_claim_after_expiry(self) -> None:
        first = self.missions.claim_lease(
            self.mission, "external:test-instance", ttl_seconds=60
        )
        first_claim_id = first["claim"]["claim_id"]
        self._expire(first)
        replacement = self.missions.claim_lease(
            self.missions.load("lease-contract"),
            "external:test-instance",
            ttl_seconds=60,
        )
        captured: list[dict[str, Any]] = []
        worker = ExternalMissionWorker(
            {
                "receipts": [
                    {
                        "item_id": replacement["items"][0]["item_id"],
                        "status": "complete",
                        "payload": {"observation": "after-expiry"},
                    }
                ]
            },
            worker_instance_id="external:test-instance",
        )

        worker.run(
            replacement,
            lambda batch: captured.append(copy.deepcopy(batch)) or {"kind": "draft"},
            {},
        )

        self.assertNotEqual(first_claim_id, replacement["claim"]["claim_id"])
        self.assertEqual(replacement["claim"]["claim_id"], captured[0]["claim_id"])

    def test_expired_owner_cannot_revive_lease_with_heartbeat(self) -> None:
        claimed = self.missions.claim_lease(
            self.mission, "worker-instance-a", ttl_seconds=60
        )
        claim_id = claimed["claim"]["claim_id"]
        self._expire(claimed)
        with self.assertRaisesRegex(HarnessProtocolError, "expired"):
            self.missions.heartbeat_lease(
                self.missions.load("lease-contract"),
                "worker-instance-a",
                claim_id,
                ttl_seconds=60,
            )

    def test_full_work_item_integrity_is_verified_before_admission(self) -> None:
        index = self.mission["work_items"]["research-a"]
        target = self.run_store.run_directory("lease-contract") / index["item_ref"]
        tampered = json.loads(target.read_text(encoding="utf-8"))
        tampered["payload"]["research_kind"] = "tampered"
        target.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaisesRegex(HarnessProtocolError, "integrity mismatch"):
            self.missions.read_item(self.mission, "research-a")


if __name__ == "__main__":
    unittest.main()
