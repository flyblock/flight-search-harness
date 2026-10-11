"""Contract tests for the Product Adapter Seam.

These tests keep the shared Product Kernel deliberately ignorant of Product
Session shapes.  Each concrete Product Adapter is exercised through the public
``start``/``advance`` Interface while its product runtime is replaced by a
small in-memory fixture.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flight_search_harness.adapters.one_way import OneWayProductAdapter
from flight_search_harness.adapters.random_loop import RandomLoopProductAdapter
from flight_search_harness.adapters.round_trip import RoundTripProductAdapter
from flight_search_harness.contracts import HarnessProtocolError
from flight_search_harness.product_kernel import AdapterProductKernel


FINAL_RESULT = {
    "status": "complete",
    "outcome": "route_found",
    "finalization_audit": {"passed": True},
}


class ProductAdapterSeamTestCase(unittest.TestCase):
    def test_shared_kernel_has_no_product_name_or_session_shape_branching(self) -> None:
        source = inspect.getsource(AdapterProductKernel)
        tree = ast.parse(source)

        for product_id in (
            "one_way",
            "random_loop",
            "round_trip",
        ):
            self.assertNotIn(product_id, source)

        forbidden_session_fields = {
            "research_tasks",
            "next_search_requests",
            "browser_query_ledger",
            "evidence_bundles",
            "finalization_status",
            "search_round",
            "current_round",
            "task_id",
            "hypothesis_id",
            "research_kind",
            "journey_scope",
            "attempt_count",
            "product_payload",
        }
        string_values = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(forbidden_session_fields & string_values)

        product_id_comparisons = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and "product_id" in ast.unparse(node)
        ]
        self.assertEqual([], product_id_comparisons)

    def test_mutating_choice_receipt_replay_does_not_repeat_product_admission(
        self,
    ) -> None:
        adapter, state = self._fixture_adapter(
            OneWayProductAdapter,
            self._fixed_route_state("choice-replay-session"),
        )
        calls = {"mutations": 0}

        def inspect(current: dict[str, Any]) -> dict[str, Any]:
            actions = []
            if not current.get("choice_applied"):
                actions.append(
                    {
                        "kind": "test_mutation",
                        "action_key": "test_mutation:once",
                    }
                )
            return {
                "product_id": adapter.product_id,
                "actions": actions,
                "investigation_actions": [],
                "disposition_actions": [],
                "has_complete_result": False,
                "can_finalize": False,
            }

        def apply_control_action(
            product_directory: Path, action: dict[str, Any]
        ) -> dict[str, Any]:
            calls["mutations"] += 1
            state["choice_applied"] = True
            raise RuntimeError("injected crash after Product admission")

        adapter.inspect = inspect
        adapter.apply_control_action = apply_control_action
        adapter.admission_state_identity = lambda product_directory: hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        kernel = AdapterProductKernel(adapter)

        with tempfile.TemporaryDirectory() as temporary_directory:
            product_directory = Path(temporary_directory)
            choice = kernel.start(
                {"journey_shape": adapter.journey_shape}, product_directory
            )["effect"]["items"][0]
            receipt = {
                "receipt_id": "receipt-choice-replay",
                "status": "complete",
                "payload": {
                    "action_key": "test_mutation:once",
                    "reason": "Apply this choice exactly once.",
                },
            }
            inbound = {
                "kind": "receipt_batch",
                "entries": [{"item": choice, "receipt": receipt}],
            }

            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                kernel.advance(product_directory, inbound)

            replayed = kernel.advance(product_directory, inbound)

            markers = list((product_directory / "choice-admissions").glob("*.json"))
            self.assertEqual(1, len(markers))
            marker = json.loads(markers[0].read_text(encoding="utf-8"))
            self.assertEqual("committed", marker["status"])

        self.assertEqual(1, calls["mutations"])
        self.assertEqual("committed", replayed["receipt_results"][0]["status"])
        self.assertEqual("blocked", replayed["effect"]["kind"])

    def test_receipt_admission_survives_transient_next_effect_failure(self) -> None:
        adapter, state = self._fixture_adapter(
            OneWayProductAdapter,
            self._fixed_route_state("planning-recovery-session"),
        )
        calls = {"ingestions": 0, "post_admission_plans": 0}
        investigation = {
            "kind": "investigate",
            "action_key": "investigate:research-1",
            "task_id": "research-1",
        }

        adapter.completed_result = lambda product_directory, current: None
        adapter.normalize_attempt = lambda attempt: copy.deepcopy(attempt)
        adapter.attempt_committed = (
            lambda current, action, attempt: bool(current.get("receipt_committed"))
        )

        def ingest_attempt(
            product_directory: Path,
            action: dict[str, Any],
            attempt: dict[str, Any],
        ) -> dict[str, Any]:
            calls["ingestions"] += 1
            state["receipt_committed"] = True
            return copy.deepcopy(state)

        def inspect(current: dict[str, Any]) -> dict[str, Any]:
            if current.get("receipt_committed"):
                calls["post_admission_plans"] += 1
                if calls["post_admission_plans"] == 1:
                    raise RuntimeError("transient planning failure")
                actions = []
            else:
                actions = [investigation]
            return {
                "product_id": adapter.product_id,
                "actions": copy.deepcopy(actions),
                "investigation_actions": copy.deepcopy(actions),
                "disposition_actions": [],
                "has_complete_result": False,
                "can_finalize": False,
            }

        def plan_transition(
            current: dict[str, Any], control: dict[str, Any]
        ) -> dict[str, Any]:
            if current.get("receipt_committed"):
                return {
                    "kind": "blocked",
                    "reason": "No further work is available.",
                    "action_required": {"type": "supply_more_evidence"},
                }
            return {
                "kind": "investigate",
                "actions": [copy.deepcopy(investigation)],
                "reason": "Collect Evidence.",
            }

        adapter.ingest_attempt = ingest_attempt
        adapter.inspect = inspect
        adapter.plan_transition = plan_transition
        adapter.build_research_items = (
            lambda product_directory, investigations, reason, identify: [
                {
                    "item_id": "research-1",
                    "kind": "browser_research",
                    "payload": {"action": copy.deepcopy(investigations[0])},
                }
            ]
        )
        kernel = AdapterProductKernel(adapter)

        with tempfile.TemporaryDirectory() as temporary_directory:
            product_directory = Path(temporary_directory)
            item = kernel.start(
                {"journey_shape": adapter.journey_shape}, product_directory
            )["effect"]["items"][0]
            transition = kernel.advance(
                product_directory,
                {
                    "kind": "receipt_batch",
                    "entries": [
                        {
                            "item": item,
                            "receipt": {
                                "receipt_id": "receipt-planning-recovery",
                                "status": "complete",
                                "payload": {"kind": "observations"},
                            },
                        }
                    ],
                },
            )

            self.assertEqual(
                [{"item_id": "research-1", "status": "committed"}],
                transition["receipt_results"],
            )
            self.assertEqual("draft", transition["effect"]["kind"])
            self.assertEqual(
                "transient planning failure",
                transition["effect"]["action_required"]["error"],
            )

            resumed = kernel.advance(product_directory, None)

        self.assertEqual(1, calls["ingestions"])
        self.assertEqual(2, calls["post_admission_plans"])
        self.assertEqual([], resumed["receipt_results"])
        self.assertEqual("blocked", resumed["effect"]["kind"])

    def test_random_loop_replay_uses_persisted_query_identity_after_request_closes(
        self,
    ) -> None:
        published = [
            {
                "request_id": "route:request",
                "parent_id": "route-parent",
                "source_strategy": {"selection_mode": "ordered"},
            }
        ]

        class ReplayEngine:
            @staticmethod
            def build_search_requests(state: dict[str, Any]) -> list[dict[str, Any]]:
                return copy.deepcopy(published)

            @staticmethod
            def normalize_ordered_observation_kind(value: Any) -> str:
                return str(value)

            @staticmethod
            def browser_query_identity(group: dict[str, Any]) -> tuple[str, str]:
                canonical = json.dumps(
                    group,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                return str(group["query_id"]), hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()

        adapter = object.__new__(RandomLoopProductAdapter)
        adapter.engine = ReplayEngine()
        action = {
            "action_id": "mission-action-query",
            "task_id": "route:request",
        }
        attempt = {
            "kind": "observations",
            "product_payload": {
                "search_results": [
                    {
                        "source_id": "provider-a",
                        "observation_kind": "payable",
                        "source": {
                            "name": "provider-a",
                            "url": "https://provider.test/search",
                            "observed_at": "2027-01-01T00:00:00+00:00",
                        },
                        "offers": [{"offer_id": "offer-1", "price": 10}],
                        "submitted_scope": {"origins": ["AAA"]},
                        "coverage_complete": True,
                        "scope_partitions": [],
                        "acquisition": {"method": "browser"},
                    }
                ]
            },
        }
        state = {"browser_query_ledger": {"entries": []}}
        admitted_group = adapter._canonical_replay_groups(state, action, attempt)[0]
        query_id, content_sha256 = adapter.engine.browser_query_identity(admitted_group)
        state["browser_query_ledger"]["entries"] = [
            {
                "query_id": query_id,
                "content_sha256": content_sha256,
                "request_id": "route:request",
                "parent_id": "route-parent",
                "source_id": "provider-a",
            }
        ]
        published.clear()

        self.assertTrue(adapter.attempt_committed(state, action, attempt))

        conflicting = copy.deepcopy(attempt)
        conflicting["product_payload"]["search_results"][0]["offers"][0][
            "price"
        ] = 11
        with self.assertRaisesRegex(
            HarnessProtocolError, "replayed with conflicting Evidence"
        ):
            adapter.attempt_committed(state, action, conflicting)

    def test_all_adapters_repair_forged_or_missing_final_from_product_session(
        self,
    ) -> None:
        cases = (
            (OneWayProductAdapter, "live", "_export_result", "finalization_reason"),
            (RandomLoopProductAdapter, "engine", "export_result", "reason"),
            (RoundTripProductAdapter, "session", "export_result", "reason"),
        )
        for adapter_class, module_name, export_name, reason_name in cases:
            with self.subTest(adapter=adapter_class.__name__):
                adapter = object.__new__(adapter_class)
                calls = {"exports": 0, "mutations": 0}

                expected = {
                    "status": "complete",
                    "reason": "original reason",
                    "revision": 7,
                    "history": [
                        {"event": "finalized", "reason": "original reason"}
                    ],
                }

                def export_result(
                    current: dict[str, Any], **keywords: Any
                ) -> dict[str, Any]:
                    calls["exports"] += 1
                    return {
                        "status": "complete",
                        "reason": keywords[reason_name],
                        "revision": current.get("product_revision"),
                        "history": copy.deepcopy(
                            current.get("finalization_history", [])
                        ),
                    }

                module = SimpleNamespace()
                setattr(module, export_name, export_result)
                module.LiveProviderError = RuntimeError
                module.RouteEngineError = RuntimeError
                module.RoundTripSessionError = RuntimeError
                setattr(adapter, module_name, module)
                state = {
                    "finalization_status": "final",
                    "product_revision": 7,
                    "finalization_history": [
                        {"event": "finalized", "reason": "original reason"}
                    ],
                }
                if adapter_class is OneWayProductAdapter:
                    state["finalization"] = {
                        "status": "final",
                        "history": copy.deepcopy(state["finalization_history"]),
                        "evaluated_at": "2027-01-01T00:00:00Z",
                    }
                    adapter.live.finalization_evaluated_at = (
                        lambda current: None
                    )
                elif adapter_class is RoundTripProductAdapter:
                    state["finalization_reason"] = "original reason"
                before = copy.deepcopy(state)
                adapter.load_state = lambda product_directory: copy.deepcopy(state)

                def fail_mutation(*args: Any, **kwargs: Any) -> Any:
                    calls["mutations"] += 1
                    raise AssertionError("finalization mutation must not repeat")

                if adapter_class is OneWayProductAdapter:
                    adapter.live.finalize_result = fail_mutation
                elif adapter_class is RandomLoopProductAdapter:
                    adapter.engine.finalize_result = fail_mutation
                else:
                    adapter.session.finalize_session = fail_mutation

                with tempfile.TemporaryDirectory() as temporary_directory:
                    product_directory = Path(temporary_directory)
                    if adapter_class is RoundTripProductAdapter:
                        adapter._remember_finalization_reason(
                            product_directory, "original reason"
                        )
                    (product_directory / "final.json").write_text(
                        json.dumps({"status": "forged", "revision": -1}),
                        encoding="utf-8",
                    )
                    recovered = adapter.finalize(
                        product_directory, "replacement reason"
                    )
                    (product_directory / "final.json").unlink()
                    read_again = adapter.completed_result(
                        product_directory, copy.deepcopy(state)
                    )
                    persisted = json.loads(
                        (product_directory / "final.json").read_text(
                            encoding="utf-8"
                        )
                    )

                self.assertEqual(before, state)
                self.assertEqual(0, calls["mutations"])
                self.assertEqual(2, calls["exports"])
                self.assertEqual("original reason", recovered["reason"])
                self.assertEqual(expected, recovered)
                self.assertEqual(recovered, read_again)
                self.assertEqual(recovered, persisted)
                self.assertNotIn("final_result_projection", state)
                self.assertNotIn(
                    "result_projection", state.get("finalization", {})
                )

    @staticmethod
    def _fixed_route_state(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "search_round": 1,
            "finalization_status": "open",
            "evidence_bundles": [],
            "research_tasks": [
                {
                    "task_id": "research-1",
                    "hypothesis_id": "hypothesis-1",
                    "research_kind": "priced_offer",
                    "journey_scope": "one_way",
                    "status": "pending",
                    "attempt_count": 0,
                }
            ],
        }

    @staticmethod
    def _random_loop_state() -> dict[str, Any]:
        return {
            "engine_version": "test",
            "finalization_status": "open",
            "next_search_requests": [
                {
                    "request_id": "research-1",
                    "parent_id": "route-root",
                }
            ],
            "browser_query_ledger": {"entries": []},
        }

    def _fixture_adapter(
        self,
        adapter_class: type,
        initial_state: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        adapter = object.__new__(adapter_class)
        state = copy.deepcopy(initial_state)
        if adapter_class is RandomLoopProductAdapter:
            adapter.engine = SimpleNamespace(
                build_search_requests=lambda current: copy.deepcopy(
                    current.get("next_search_requests", [])
                ),
                export_result=lambda current, **keywords: copy.deepcopy(FINAL_RESULT),
                RouteEngineError=RuntimeError,
            )
        elif adapter_class is OneWayProductAdapter:
            adapter.live = SimpleNamespace(
                finalization_evaluated_at=lambda current: None,
                _export_result=lambda current, **keywords: copy.deepcopy(FINAL_RESULT),
                LiveProviderError=RuntimeError,
            )
        else:
            adapter.session = SimpleNamespace(
                export_result=lambda current, **keywords: copy.deepcopy(FINAL_RESULT),
                RoundTripSessionError=RuntimeError,
            )

        def initialize(request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
            product_directory.mkdir(parents=True, exist_ok=True)
            return copy.deepcopy(state)

        def load_state(product_directory: Path) -> dict[str, Any]:
            return copy.deepcopy(state)

        def inspect(current: dict[str, Any]) -> dict[str, Any]:
            action = {
                "kind": "investigate",
                "action_key": "investigate:research-1",
                "task_id": "research-1",
                "status": "pending",
                "sequence": 1,
            }
            return {
                "product_id": adapter.product_id,
                "actions": [action],
                "investigation_actions": [action],
                "disposition_actions": [],
                "has_complete_result": False,
                "can_finalize": False,
            }

        def prepare_investigation(
            product_directory: Path,
            task_id: str,
            reason: str,
        ) -> dict[str, Any]:
            return {
                "work_version": "test",
                "product_id": adapter.product_id,
                "task_id": task_id,
                "brief": {"research_kind": "contract_test", "reason": reason},
                "attempt_schema": {
                    "type": "object",
                    "required": ["kind", "product_payload"],
                    "properties": {
                        "kind": {"const": "observations"},
                        "product_payload": {"type": "object"},
                    },
                },
            }

        def ingest_attempt(
            product_directory: Path,
            action: dict[str, Any],
            attempt: dict[str, Any],
        ) -> dict[str, Any]:
            state["finalization_status"] = "final"
            if adapter_class is OneWayProductAdapter:
                state["finalization"] = {
                    "status": "final",
                    "history": [],
                }
            return copy.deepcopy(state)

        adapter.initialize = initialize
        adapter.load_state = load_state
        adapter.inspect = inspect
        adapter.prepare_investigation = prepare_investigation
        adapter.attempt_committed = lambda current, action, attempt: False
        adapter.ingest_attempt = ingest_attempt
        adapter.read_final_result = lambda product_directory: (
            copy.deepcopy(FINAL_RESULT)
            if state.get("finalization_status") == "final"
            else None
        )
        adapter.apply_control_action = lambda product_directory, action: copy.deepcopy(state)
        adapter.finalize = lambda product_directory, reason: copy.deepcopy(FINAL_RESULT)
        return adapter, state


if __name__ == "__main__":
    unittest.main()
