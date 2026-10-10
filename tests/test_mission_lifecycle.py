"""Executable lifecycle specification for :class:`SearchMissionHarness`.

The test doubles intentionally implement only the public Product Kernel and
Worker Interfaces.  This keeps the specification focused on lifecycle
ownership, Receipt admission, recovery, and Outcome materialization instead of
depending on any one travel product's search algorithm.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from flight_search_harness.contracts import OUTCOME_VERSION, REQUEST_VERSION
from flight_search_harness.mission_contracts import blocked, complete, draft, need_work
from flight_search_harness.mission_runner import SearchMissionHarness
from flight_search_harness.mission_workers import ExternalMissionWorker
from flight_search_harness.run_store import RunStore


PRODUCT_ID = "random_loop"
JOURNEY_SHAPE = "random_loop"


def request() -> dict[str, Any]:
    return {
        "request_version": REQUEST_VERSION,
        "journey_shape": JOURNEY_SHAPE,
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


def work_item(
    identifier: str,
    *,
    kind: str = "browser_research",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    }
    return {
        "item_id": identifier,
        "kind": kind,
        "payload": payload,
    }


def final_result(identifier: str = "route-1") -> dict[str, Any]:
    return {
        "status": "complete",
        "outcome": "route_found",
        "selected_route": {
            "route_id": identifier,
            "segments": [],
        },
        "finalization_audit": {"passed": True},
    }


def receipt_for(
    item: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identifier = str(item["item_id"])
    return {
        "receipt_version": "2.0",
        "item_id": identifier,
        "attempt_number": int(item.get("attempt_number", 1)),
        "status": "complete",
        "payload": copy.deepcopy(payload or {"observation": identifier}),
    }


class ScriptedProductKernel:
    """Small Product Kernel double with explicit effects and admission results."""

    product_id = PRODUCT_ID
    journey_shape = JOURNEY_SHAPE

    def __init__(
        self,
        start_effect: dict[str, Any],
        advances: list[dict[str, Any]] | None = None,
    ) -> None:
        self.start_effect = copy.deepcopy(start_effect)
        self.advances = copy.deepcopy(advances or [])
        self.start_calls = 0
        self.advance_calls = 0
        self.receipt_batch_calls = 0

    def start(
        self, requested: dict[str, Any], product_directory: Path
    ) -> dict[str, Any]:
        self.start_calls += 1
        product_directory.mkdir(parents=True, exist_ok=True)
        (product_directory / "session.json").write_text(
            json.dumps({"initialized": True}), encoding="utf-8"
        )
        return {"effect": copy.deepcopy(self.start_effect)}

    def advance(
        self,
        product_directory: Path,
        inbound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.advance_calls += 1
        if not self.advances:
            raise AssertionError("unexpected Product Kernel advance")
        specification = self.advances.pop(0)
        if inbound is None:
            if specification.get("requires_receipts"):
                raise AssertionError("expected a Receipt batch")
            return {"effect": copy.deepcopy(specification["effect"])}
        if inbound.get("kind") != "receipt_batch":
            raise AssertionError(f"unexpected inbound value: {inbound!r}")
        self.receipt_batch_calls += 1
        entries = inbound["entries"]
        statuses = specification.get("statuses", {})
        results = []
        for entry in entries:
            identifier = entry["receipt"]["item_id"]
            configured = statuses.get(identifier, "committed")
            if isinstance(configured, str):
                result = {"item_id": identifier, "status": configured}
            else:
                result = {"item_id": identifier, **copy.deepcopy(configured)}
            results.append(result)
        return {
            "receipt_results": results,
            "effect": copy.deepcopy(specification["effect"]),
        }


class NoopWorker:
    worker_instance_id = "noop-worker-instance"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, lease: dict[str, Any], checkpoint: Callable[..., Any], context: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("Worker must not run for this transition")


class ReceiptWorker:
    """Submit one Receipt per leased Work Item until the Kernel stops asking."""

    worker_instance_id = "receipt-worker-instance"

    def __init__(
        self,
        *,
        payload_factory: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        self.payload_factory = payload_factory
        self.metrics = copy.deepcopy(metrics)
        self.calls = 0
        self.checkpoints = 0

    def run(self, lease: dict[str, Any], checkpoint: Callable[..., Any], context: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        current = copy.deepcopy(lease)
        for _ in range(16):
            revision = int(current["mission_revision"])
            receipts = []
            for item in current["items"]:
                payload = (
                    self.payload_factory(item, self.checkpoints)
                    if self.payload_factory is not None
                    else {"observation": item["item_id"]}
                )
                receipts.append(receipt_for(item, payload=payload))
            batch = {
                "lease_id": current["lease_id"],
                "mission_revision": revision,
                "claim_id": current["claim"]["claim_id"],
                "worker_instance_id": self.worker_instance_id,
                "receipts": receipts,
            }
            if self.metrics is not None:
                batch["metrics"] = copy.deepcopy(self.metrics)
            self.checkpoints += 1
            step = checkpoint(batch)
            if step.get("kind") == "lease":
                current = copy.deepcopy(step["lease"])
                continue
            return step
        raise AssertionError("Product Kernel requested too many Work Item batches")


class ExitWorker:
    worker_instance_id = "exit-worker-instance"

    def __init__(self, exit_value: dict[str, Any]) -> None:
        self.exit_value = copy.deepcopy(exit_value)
        self.calls = 0

    def run(self, lease: dict[str, Any], checkpoint: Callable[..., Any], context: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return copy.deepcopy(self.exit_value)


class InvalidCheckpointWorker:
    worker_instance_id = "invalid-checkpoint-worker-instance"

    def __init__(self, mutation: str) -> None:
        self.mutation = mutation

    def run(self, lease: dict[str, Any], checkpoint: Callable[..., Any], context: dict[str, Any]) -> dict[str, Any]:
        batch = {
            "lease_id": lease["lease_id"],
            "mission_revision": lease["mission_revision"],
            "claim_id": lease["claim"]["claim_id"],
            "worker_instance_id": self.worker_instance_id,
            "receipts": [
                receipt_for(lease["items"][0])
            ],
        }
        if self.mutation == "lease":
            batch["lease_id"] = "lease-stale"
        elif self.mutation == "revision":
            batch["mission_revision"] += 1
        else:
            raise AssertionError(f"unsupported mutation: {self.mutation}")
        return checkpoint(batch)


class FailOnceAdmissionKernel:
    """Durable idempotent Product Kernel double for Receipt crash windows."""

    product_id = PRODUCT_ID
    journey_shape = JOURNEY_SHAPE

    def __init__(self, *, fail_before_admission: bool) -> None:
        self.fail_before_admission = fail_before_admission
        self.has_failed = False
        self.advance_calls = 0

    @staticmethod
    def _state_path(product_directory: Path) -> Path:
        return product_directory / "fake-admission-state.json"

    def _read_state(self, product_directory: Path) -> dict[str, Any]:
        target = self._state_path(product_directory)
        if not target.exists():
            return {"admission_count": 0, "receipts": {}}
        return json.loads(target.read_text(encoding="utf-8"))

    def _write_state(self, product_directory: Path, value: dict[str, Any]) -> None:
        self._state_path(product_directory).write_text(
            json.dumps(value, sort_keys=True), encoding="utf-8"
        )

    def start(self, requested: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        product_directory.mkdir(parents=True, exist_ok=True)
        self._write_state(product_directory, {"admission_count": 0, "receipts": {}})
        return {"effect": need_work([work_item("research-1")], reason="Need Evidence")}

    def advance(
        self,
        product_directory: Path,
        inbound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.advance_calls += 1
        if inbound is None:
            state = self._read_state(product_directory)
            if state["receipts"]:
                # An active Lease remains authoritative until its Harness commit.
                return {"effect": need_work([work_item("research-1")], reason="Recover commit")}
            return {"effect": need_work([work_item("research-1")], reason="Retry admission")}

        entry = inbound["entries"][0]
        received = entry["receipt"]
        if self.fail_before_admission and not self.has_failed:
            self.has_failed = True
            raise RuntimeError("injected failure after raw Receipt write")

        state = self._read_state(product_directory)
        key = received["receipt_id"]
        canonical = json.dumps(received, sort_keys=True, separators=(",", ":"))
        existing = state["receipts"].get(key)
        if existing is not None and existing != canonical:
            raise RuntimeError("Product Kernel received conflicting Receipt content")
        if existing is None:
            state["receipts"][key] = canonical
            state["admission_count"] += 1
            self._write_state(product_directory, state)
        if not self.fail_before_admission and not self.has_failed:
            self.has_failed = True
            raise RuntimeError("injected failure after Product Kernel admission")
        return {
            "receipt_results": [
                {"item_id": received["item_id"], "status": "committed"}
            ],
            "effect": complete(final_result()),
        }

    def admission_count(self, product_directory: Path) -> int:
        return int(self._read_state(product_directory)["admission_count"])


class GateDriveHarness(SearchMissionHarness):
    """Pause the first selected drive after a resume transaction commits."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate_enabled = False
        self.gate_entered = threading.Event()
        self.gate_release = threading.Event()
        self._gate_used = False

    def _drive(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.gate_enabled and not self._gate_used:
            self._gate_used = True
            self.gate_entered.set()
            if not self.gate_release.wait(timeout=5):
                raise AssertionError("resume race gate timed out")
        return super()._drive(*args, **kwargs)


class SearchMissionHarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = RunStore(self.temporary_directory.name)

    def harness(self, kernel: Any) -> SearchMissionHarness:
        return SearchMissionHarness(
            self.store,
            {JOURNEY_SHAPE: kernel},
            lease_ttl_seconds=60,
        )

    @staticmethod
    def start_command(run_id: str) -> dict[str, Any]:
        return {"kind": "start", "run_id": run_id, "request": request()}

    @staticmethod
    def resume_command(run_id: str) -> dict[str, Any]:
        return {"kind": "resume", "run_id": run_id}

    def assert_outcome(self, value: dict[str, Any], kind: str, run_id: str) -> None:
        self.assertEqual(OUTCOME_VERSION, value.get("outcome_version"))
        self.assertEqual(kind, value.get("kind"))
        self.assertEqual(run_id, value.get("run_id"))
        self.assertEqual(PRODUCT_ID, value.get("product_id"))

    def test_start_need_work_receipt_complete(self) -> None:
        run_id = "lifecycle-complete"
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence"),
            [
                {
                    "requires_receipts": True,
                    "effect": complete(final_result()),
                }
            ],
        )

        outcome = self.harness(kernel).run(
            self.start_command(run_id), ReceiptWorker()
        )

        self.assert_outcome(outcome, "completed", run_id)
        self.assertEqual("completed", self.store.load_manifest(run_id)["status"])
        mission = self.harness(kernel).missions.load(run_id)
        self.assertIsNone(mission["active_lease_ref"])
        self.assertEqual(
            "committed", mission["work_items"]["research-1"]["last_decision"]
        )
        self.assertEqual(1, kernel.receipt_batch_calls)

    def test_product_kernel_draft_is_materialized_as_public_outcome(self) -> None:
        run_id = "kernel-draft"
        kernel = ScriptedProductKernel(
            draft("Useful work exists but the search is unfinished."),
        )

        outcome = self.harness(kernel).run(self.start_command(run_id), NoopWorker())

        self.assert_outcome(outcome, "draft", run_id)
        self.assertNotIn("outcome", outcome, "Outcome must not be wrapped in a Runner step")
        self.assertEqual("draft", self.store.load_manifest(run_id)["status"])

    def test_blocked_run_can_resume_to_product_kernel_completion(self) -> None:
        run_id = "blocked-resume"
        kernel = ScriptedProductKernel(
            blocked("A user decision is required.", {"type": "choose_scope"}),
            [{"effect": complete(final_result())}],
        )
        harness = self.harness(kernel)

        first = harness.run(self.start_command(run_id), NoopWorker())
        second = harness.run(self.resume_command(run_id), NoopWorker())

        self.assert_outcome(first, "blocked", run_id)
        self.assert_outcome(second, "completed", run_id)
        self.assertEqual("completed", self.store.load_manifest(run_id)["status"])
        self.assertIsNone(harness.missions.load(run_id)["active_lease_ref"])

    def test_failed_outcome_is_terminal_for_ordinary_resume(self) -> None:
        run_id = "failed-terminal"
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence"),
        )
        harness = self.harness(kernel)
        worker = ExitWorker(
            {
                "kind": "failed",
                "retryable": False,
                "error": {"code": "provider_contract_broken", "message": "No recovery"},
            }
        )

        first = harness.run(self.start_command(run_id), worker)
        advance_calls = kernel.advance_calls
        event_count = len(self.store.read_events(run_id))
        second = harness.run(self.resume_command(run_id), NoopWorker())

        self.assert_outcome(first, "failed", run_id)
        self.assertNotIn(
            "resume_token",
            first,
            "A terminal failed Outcome must not advertise ordinary resume.",
        )
        self.assertNotIn("retryable", first)
        self.assertEqual(first, second)
        self.assertEqual(advance_calls, kernel.advance_calls)
        self.assertEqual(event_count, len(self.store.read_events(run_id)))
        self.assertEqual("failed", self.store.load_manifest(run_id)["status"])

    def test_completed_resume_is_read_only_and_idempotent(self) -> None:
        run_id = "completed-idempotent"
        kernel = ScriptedProductKernel(complete(final_result()))
        harness = self.harness(kernel)

        first = harness.run(self.start_command(run_id), NoopWorker())
        advance_calls = kernel.advance_calls
        events = self.store.read_events(run_id)
        second = harness.run(self.resume_command(run_id), NoopWorker())

        self.assert_outcome(first, "completed", run_id)
        self.assertEqual(first, second)
        self.assertEqual(advance_calls, kernel.advance_calls)
        self.assertEqual(events, self.store.read_events(run_id))
        self.assertEqual("completed", self.store.load_manifest(run_id)["status"])

    def test_stale_lease_and_revision_are_rejected_before_product_admission(self) -> None:
        for mutation in ("lease", "revision"):
            with self.subTest(mutation=mutation):
                run_id = f"stale-{mutation}"
                kernel = ScriptedProductKernel(
                    need_work([work_item("research-1")], reason="Need Evidence"),
                )

                outcome = self.harness(kernel).run(
                    self.start_command(run_id), InvalidCheckpointWorker(mutation)
                )

                self.assertEqual("blocked", outcome.get("kind"))
                self.assertEqual(
                    "correct_mission_command",
                    outcome["action_required"]["type"],
                )
                self.assertEqual(0, kernel.receipt_batch_calls)
                mission = self.harness(kernel).missions.load(run_id)
                self.assertNotIn(
                    "last_admission_ref", mission["work_items"]["research-1"]
                )
                self.assertIsNotNone(mission["active_lease_ref"])
                self.assertEqual("running", self.store.load_manifest(run_id)["status"])
                self.assertIsNone(
                    self.harness(kernel).missions.active_lease(mission)["claim"]
                )

    def test_replayed_receipt_after_product_admission_commits_once(self) -> None:
        run_id = "admission-replay"
        kernel = FailOnceAdmissionKernel(fail_before_admission=False)
        harness = self.harness(kernel)
        metrics = {"worker_calls": 1, "provider_query_count": 1}

        first = harness.run(
            self.start_command(run_id), ReceiptWorker(metrics=metrics)
        )
        second = harness.run(
            self.resume_command(run_id), ReceiptWorker(metrics=metrics)
        )

        self.assertIn(first["kind"], {"draft", "blocked", "failed"})
        self.assert_outcome(second, "completed", run_id)
        product_directory = self.store.product_directory(run_id)
        self.assertEqual(1, kernel.admission_count(product_directory))
        mission = harness.missions.load(run_id)
        self.assertEqual(
            "committed", mission["work_items"]["research-1"]["last_decision"]
        )
        metrics_value = self.store.metrics(run_id)["mission_metrics"]
        self.assertEqual(1, metrics_value["worker_calls"])
        self.assertEqual(1, metrics_value["provider_query_count"])

    def test_replayed_receipt_identity_with_different_content_is_rejected(self) -> None:
        run_id = "receipt-content-mismatch"
        kernel = FailOnceAdmissionKernel(fail_before_admission=True)
        harness = self.harness(kernel)

        first = harness.run(
            self.start_command(run_id),
            ReceiptWorker(payload_factory=lambda item, _: {"value": "first"}),
        )
        second = harness.run(
            self.resume_command(run_id),
            ReceiptWorker(payload_factory=lambda item, _: {"value": "different"}),
        )

        self.assertIn(first["kind"], {"draft", "blocked", "failed"})
        self.assertEqual("blocked", second["kind"])
        self.assertEqual(
            "correct_mission_command", second["action_required"]["type"]
        )
        self.assertEqual("running", self.store.load_manifest(run_id)["status"])
        self.assertEqual(0, kernel.admission_count(self.store.product_directory(run_id)))
        self.assertNotIn(
            "last_admission_ref",
            harness.missions.load(run_id)["work_items"]["research-1"],
        )
        raw_receipts = list(
            (self.store.run_directory(run_id) / "mission/receipts/raw").glob("*.json")
        )
        self.assertEqual(1, len(raw_receipts))
        persisted = json.loads(raw_receipts[0].read_text(encoding="utf-8"))
        self.assertEqual({"value": "first"}, persisted["payload"])
        mission = harness.missions.load(run_id)
        self.assertIsNone(harness.missions.active_lease(mission)["claim"])

    def test_partial_correction_preserves_committed_sibling_and_resolves_original(self) -> None:
        run_id = "partial-correction"
        original_a = work_item("research-a")
        original_b = work_item("research-b")
        correction = work_item("research-b")
        kernel = ScriptedProductKernel(
            need_work([original_a, original_b], reason="Need Evidence"),
            [
                {
                    "requires_receipts": True,
                    "statuses": {
                        "research-a": "committed",
                        "research-b": {
                            "status": "correction_required",
                            "error": "Missing required field",
                        },
                    },
                    "effect": need_work([correction], reason="Correct rejected Receipt"),
                },
                {
                    "requires_receipts": True,
                    "effect": complete(final_result()),
                },
            ],
        )

        outcome = self.harness(kernel).run(
            self.start_command(run_id), ReceiptWorker()
        )

        self.assert_outcome(outcome, "completed", run_id)
        mission = self.harness(kernel).missions.load(run_id)
        self.assertEqual(
            "committed", mission["work_items"]["research-a"]["last_decision"]
        )
        self.assertEqual(
            "committed", mission["work_items"]["research-b"]["last_decision"]
        )
        self.assertEqual(2, mission["work_items"]["research-b"]["attempt_count"])
        self.assertNotIn("resolved_by", mission["work_items"]["research-b"])
        self.assertNotIn("correction_of", mission["work_items"]["research-b"])

    def test_worker_cannot_declare_product_completion(self) -> None:
        run_id = "worker-forged-completion"
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence"),
        )
        forged = {
            "outcome_version": OUTCOME_VERSION,
            "kind": "completed",
            "run_id": run_id,
            "product_id": PRODUCT_ID,
            "result_ref": "result/forged.json",
        }
        harness = self.harness(kernel)

        outcome = harness.run(
            self.start_command(run_id),
            ExitWorker({"kind": "complete", "outcome": forged}),
        )

        self.assertNotEqual("completed", outcome.get("kind"))
        self.assertNotEqual("completed", self.store.load_manifest(run_id)["status"])
        self.assertFalse((self.store.run_directory(run_id) / "product/final.json").exists())
        self.assertEqual(0, kernel.receipt_batch_calls)
        mission = harness.missions.load(run_id)
        self.assertIsNone(harness.missions.active_lease(mission)["claim"])

    def test_resume_advance_and_effect_materialization_are_one_transaction(self) -> None:
        run_id = "resume-single-advance"
        kernel = ScriptedProductKernel(
            blocked("Wait before resuming", {"type": "resume"}),
            [
                {
                    "effect": need_work(
                        [work_item("research-1")], reason="One advance only"
                    )
                }
            ],
        )
        harness = GateDriveHarness(
            self.store,
            {JOURNEY_SHAPE: kernel},
            lease_ttl_seconds=60,
        )
        first = harness.run(self.start_command(run_id), NoopWorker())
        self.assertEqual("blocked", first["kind"])
        harness.gate_enabled = True
        thread_result: list[dict[str, Any]] = []

        def first_resume() -> None:
            thread_result.append(
                harness.run(
                    self.resume_command(run_id),
                    ExternalMissionWorker(worker_instance_id="external:first"),
                )
            )

        thread = threading.Thread(target=first_resume)
        thread.start()
        self.assertTrue(harness.gate_entered.wait(timeout=5))
        competing = harness.run(
            self.resume_command(run_id),
            ExternalMissionWorker(worker_instance_id="external:second"),
        )
        harness.gate_release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, kernel.advance_calls)
        self.assertEqual("blocked", competing["kind"])
        self.assertEqual(1, len(thread_result))

    def test_stale_worker_exception_cannot_overwrite_replacement_outcome(self) -> None:
        run_id = "stale-worker-exception"
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence")
        )
        harness = self.harness(kernel)
        replacement_outcome = {
            "outcome_version": OUTCOME_VERSION,
            "kind": "blocked",
            "run_id": run_id,
            "product_id": PRODUCT_ID,
            "reason": "Replacement Worker owns the Lease.",
            "action_required": {"type": "wait_for_replacement"},
        }

        class StaleCrashWorker:
            worker_instance_id = "worker:stale"

            def run(
                worker_self,
                lease: dict[str, Any],
                checkpoint: Callable[..., Any],
                context: dict[str, Any],
            ) -> dict[str, Any]:
                with self.store.run_lock(run_id):
                    mission = harness.missions.load(run_id)
                    current = harness.missions.active_lease(mission)
                    current["claim"]["expires_at"] = (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat(timespec="seconds")
                    harness.missions._write_lease(mission, current)
                    harness.missions.claim_lease(
                        mission, "worker:replacement", ttl_seconds=60
                    )
                    manifest = self.store.load_manifest(run_id)
                    manifest["status"] = "blocked"
                    manifest["last_outcome"] = copy.deepcopy(replacement_outcome)
                    self.store.save_manifest(manifest)
                raise RuntimeError("stale Worker crashed late")

        outcome = harness.run(self.start_command(run_id), StaleCrashWorker())

        self.assertEqual(replacement_outcome, outcome)
        manifest = self.store.load_manifest(run_id)
        self.assertEqual(replacement_outcome, manifest["last_outcome"])
        mission = harness.missions.load(run_id)
        self.assertEqual(
            "worker:replacement",
            harness.missions.active_lease(mission)["claim"]["worker_instance_id"],
        )

    def test_resume_recovers_product_session_before_first_effect_commit(self) -> None:
        run_id = "recover-session-before-effect"
        manifest = self.store.create(request(), PRODUCT_ID, run_id=run_id)
        harness = self.harness(
            ScriptedProductKernel(
                blocked("discarded start effect"),
                [{"effect": complete(final_result())}],
            )
        )
        harness.missions.create(manifest)
        kernel = harness.kernels[JOURNEY_SHAPE]
        kernel.start(request(), self.store.product_directory(run_id))

        outcome = harness.run(self.resume_command(run_id), NoopWorker())

        self.assert_outcome(outcome, "completed", run_id)
        self.assertEqual(1, kernel.start_calls)
        self.assertEqual(1, kernel.advance_calls)

    def test_mismatched_duplicate_start_does_not_terminalize_initializing_run(self) -> None:
        run_id = "duplicate-start-mismatch"
        self.store.create(request(), PRODUCT_ID, run_id=run_id)
        different = request()
        different["intent"]["max_legs"] = 4

        outcome = self.harness(
            ScriptedProductKernel(complete(final_result()))
        ).run(
            {"kind": "start", "run_id": run_id, "request": different},
            NoopWorker(),
        )

        self.assertEqual("blocked", outcome["kind"])
        self.assertEqual("initializing", self.store.load_manifest(run_id)["status"])
        self.assertIsNone(self.store.load_manifest(run_id)["last_outcome"])

    def test_invalid_worker_exit_releases_original_claim(self) -> None:
        run_id = "invalid-worker-exit"
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence")
        )
        harness = self.harness(kernel)

        outcome = harness.run(
            self.start_command(run_id), ExitWorker({"kind": "invented"})
        )

        self.assertEqual("blocked", outcome["kind"])
        self.assertEqual("correct_mission_command", outcome["action_required"]["type"])
        mission = harness.missions.load(run_id)
        self.assertIsNone(harness.missions.active_lease(mission)["claim"])

    def test_run_staging_temp_residue_is_removed_and_recovered(self) -> None:
        run_id = "recover-run-staging-temp"
        run_directory = self.store.run_directory(run_id)
        run_directory.mkdir(parents=True)
        residue = run_directory / "..initializing-run.json.deadbeef.tmp"
        residue.write_text('{"partial":', encoding="utf-8")
        kernel = ScriptedProductKernel(complete(final_result()))

        outcome = self.harness(kernel).run(
            self.start_command(run_id), NoopWorker()
        )

        self.assert_outcome(outcome, "completed", run_id)
        self.assertFalse(residue.exists())
        self.assertTrue((run_directory / "run.json").is_file())

    def test_mission_staging_temp_residue_is_removed_and_recovered(self) -> None:
        run_id = "recover-mission-staging-temp"
        self.store.create(request(), PRODUCT_ID, run_id=run_id)
        mission_directory = self.store.run_directory(run_id) / "mission"
        mission_directory.mkdir()
        residue = mission_directory / "..initializing-mission.json.deadbeef.tmp"
        residue.write_text('{"partial":', encoding="utf-8")
        kernel = ScriptedProductKernel(complete(final_result()))

        outcome = self.harness(kernel).run(
            self.resume_command(run_id), NoopWorker()
        )

        self.assert_outcome(outcome, "completed", run_id)
        self.assertFalse(residue.exists())
        self.assertTrue((mission_directory / "mission.json").is_file())

    def test_pending_product_pause_survives_post_admission_crash(self) -> None:
        run_id = "pending-effect-replay"
        exact_effect = draft(
            "Exact Product draft survived the crash.",
            {"type": "continue_exact_product_draft"},
        )
        kernel = ScriptedProductKernel(
            need_work([work_item("research-1")], reason="Need Evidence"),
            [{"requires_receipts": True, "effect": exact_effect}],
        )
        harness = self.harness(kernel)
        original = harness._materialize_effect
        failed = False

        def fail_pause_once(
            manifest: dict[str, Any],
            mission: dict[str, Any],
            effect: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal failed
            if effect.get("kind") == "draft" and not failed:
                failed = True
                raise RuntimeError("injected post-close Outcome crash")
            return original(manifest, mission, effect)

        with patch.object(harness, "_materialize_effect", side_effect=fail_pause_once):
            first = harness.run(self.start_command(run_id), ReceiptWorker())

        self.assertEqual("blocked", first["kind"])
        advance_calls = kernel.advance_calls
        pending = harness.missions.pending_effect(harness.missions.load(run_id))
        self.assertEqual(exact_effect, pending)

        recovered = harness.run(self.resume_command(run_id), NoopWorker())

        self.assert_outcome(recovered, "draft", run_id)
        self.assertEqual(exact_effect["reason"], recovered["reason"])
        self.assertEqual(
            exact_effect["action_required"], recovered["action_required"]
        )
        self.assertEqual(advance_calls, kernel.advance_calls)
        self.assertIsNone(
            harness.missions.pending_effect(harness.missions.load(run_id))
        )

    def test_event_append_delimits_final_record_without_newline(self) -> None:
        run_id = "event-missing-newline"
        manifest = self.store.create(request(), PRODUCT_ID, run_id=run_id)
        events_path = self.store.run_directory(run_id) / "events.jsonl"
        first = {
            "event_version": "1.0",
            "sequence": 41,
            "type": "existing",
            "at": "2027-01-01T00:00:00+00:00",
        }
        events_path.write_text(
            json.dumps(first, separators=(",", ":")), encoding="utf-8"
        )

        self.store.append_event(manifest, "appended")

        raw = events_path.read_text(encoding="utf-8")
        self.assertIn("}\n{", raw)
        events = self.store.read_events(run_id)
        self.assertEqual([41, 42], [event["sequence"] for event in events])

    def test_duplicate_start_cannot_overwrite_completed_run(self) -> None:
        run_id = "duplicate-start-completed"
        kernel = ScriptedProductKernel(complete(final_result()))
        harness = self.harness(kernel)
        completed = harness.run(self.start_command(run_id), NoopWorker())

        replay = harness.run(self.start_command(run_id), NoopWorker())

        self.assertEqual(completed, replay)
        self.assertEqual("completed", self.store.load_manifest(run_id)["status"])


if __name__ == "__main__":
    unittest.main()
