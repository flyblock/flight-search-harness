from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = ROOT / "product" / "one-way"
SCRIPTS = PRODUCT_ROOT / "scripts"


def load_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_module("test_one_way_materializer", "scenario_materializer.py")
LIVE = load_module("test_one_way_live", "live_provider_session.py")


def one_way_intent(*, date_mode: str = "bounded_window") -> dict[str, Any]:
    intent: dict[str, Any] = {
        "intent_id": f"test-{date_mode.replace('_', '-')}",
        "trip_scope": "one_way",
        "trip_mode": "fixed_destination",
        "currency": "EUR",
        "currency_decimal_places": 2,
        "traveler_count": 1,
        "baggage": {"mode": "personal_item", "paid_baggage": False},
        "overnight_allowed": True,
        "self_transfer_policy": "allow",
        "allowed_modes": ["flight", "rail", "bus"],
        "mode_transition_buffers_minutes": {
            "surface_to_flight": 100,
            "self_transfer_to_flight": 110,
            "flight_to_flight": 70,
            "flight_to_surface": 20,
            "surface_to_surface": 15,
        },
        "origin": {
            "region_id": "berlin-region",
            "label": "Berlin",
            "country_codes": ["DE"],
            "policy": "exact",
            "allowed_ground_modes": ["rail", "bus"],
            "seed_nodes": [
                {
                    "node_id": "BER",
                    "node_type": "airport",
                    "location": "Berlin Brandenburg Airport",
                    "country_or_region": "Germany",
                    "country_code": "DE",
                    "timezone": "Europe/Berlin",
                    "coordinates": {"latitude": 52.3667, "longitude": 13.5033},
                }
            ],
        },
        "destination": {
            "region_id": "lisbon-region",
            "label": "Lisbon",
            "country_codes": ["PT"],
            "policy": "exact",
            "allowed_ground_modes": ["rail", "bus"],
            "seed_nodes": [
                {
                    "node_id": "LIS",
                    "node_type": "airport",
                    "location": "Lisbon Airport",
                    "country_or_region": "Portugal",
                    "country_code": "PT",
                    "timezone": "Europe/Lisbon",
                    "coordinates": {"latitude": 38.7742, "longitude": -9.1342},
                }
            ],
        },
        "date_window": {"start": "2027-03-10", "end": "2027-03-20"},
        "date_flexibility": {"mode": date_mode},
    }
    if date_mode == "exact_dates":
        intent["date_flexibility"]["exact_departure_dates"] = [
            "2027-03-12",
            "2027-03-16",
        ]
    return intent


def public_one_way_intent() -> dict[str, Any]:
    internal = one_way_intent()
    return {
        "base_currency": internal["currency"],
        "traveler_count": internal["traveler_count"],
        "baggage_policy": {
            "cabin_baggage": "personal_item",
            "checked_bag_count": 0,
            "paid_baggage_allowed": False,
        },
        "overnight_allowed": internal["overnight_allowed"],
        "self_transfer_policy": internal["self_transfer_policy"],
        "fx_to_base": {"EUR": 1.0},
        "allowed_modes": internal["allowed_modes"],
        "origin": {
            "region_id": internal["origin"]["region_id"],
            "label": internal["origin"]["label"],
            "country_codes": internal["origin"]["country_codes"],
            "policy": "exact",
            "endpoint_policy": "anchor_required",
            "anchor_nodes": copy.deepcopy(internal["origin"]["seed_nodes"]),
            "seed_gateway_nodes": [],
            "allowed_ground_modes": ["rail", "bus"],
        },
        "destination": {
            "region_id": internal["destination"]["region_id"],
            "label": internal["destination"]["label"],
            "country_codes": internal["destination"]["country_codes"],
            "policy": "exact",
            "endpoint_policy": "anchor_required",
            "anchor_nodes": copy.deepcopy(internal["destination"]["seed_nodes"]),
            "seed_gateway_nodes": [],
            "allowed_ground_modes": ["rail", "bus"],
        },
        "departure_window": copy.deepcopy(internal["date_window"]),
    }


def new_compact_session(*, date_mode: str = "bounded_window") -> dict[str, Any]:
    artifacts = MATERIALIZER.materialize_intent(one_way_intent(date_mode=date_mode))
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        for name in ("profile", "nodes", "edges", "config"):
            (directory / f"{name}.json").write_text(
                json.dumps(artifacts[name], ensure_ascii=False),
                encoding="utf-8",
            )
        return LIVE.initialize_session(directory / "config.json")


def new_session(*, date_mode: str = "bounded_window") -> dict[str, Any]:
    return LIVE.validate_session(new_compact_session(date_mode=date_mode))


def baseline_task(state: dict[str, Any]) -> dict[str, Any]:
    return next(
        task
        for task in state["research_tasks"]
        if task.get("request_type") == "conventional_reference"
        and task.get("source_suggestions")
    )


def no_result_bundle(
    task: dict[str, Any], bundle_id: str, source: dict[str, Any]
) -> dict[str, Any]:
    return {
        "bundle_version": "0.7.1",
        "bundle_id": bundle_id,
        "request_id": task["task_id"],
        "discovered_nodes": [],
        "observations": [
            {
                "observation_id": f"observation-{bundle_id}",
                "evidence_status": "no_result",
                "provider": source["name"],
                "url": source["url"],
                "observed_at": "2027-01-10T12:00:00Z",
                "reason": "No payable result was visible for the issued scope.",
            }
        ],
    }


def priced_alternative_bundle(
    task: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    def iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    common = {
        "evidence_status": "verified_observed",
        "from_node": "BER",
        "to_node": "LIS",
        "mode": "flight",
        "departure": "2027-03-12T09:00:00+01:00",
        "arrival": "2027-03-12T11:30:00+00:00",
        "baggage": {
            "status": "confirmed",
            "included_personal_item": True,
            "mandatory_cost": {"amount": 0, "currency": "EUR"},
        },
        "operator": "Example Air",
        "flight_identity": {
            "marketing_carrier": "Example Air",
            "flight_numbers": ["EX123"],
        },
    }
    return {
        "bundle_version": "0.7.1",
        "bundle_id": "bundle-priced-alternatives",
        "request_id": task["task_id"],
        "discovered_nodes": [],
        "observations": [
            {
                **copy.deepcopy(common),
                "observation_id": "offer-cheap",
                "provider": "Cheap Seller",
                "provider_offer_id": "cheap-offer",
                "url": "https://cheap.example/offer",
                "observed_at": iso(now - timedelta(hours=2)),
                "expires_at": iso(now + timedelta(hours=1)),
                "price": {"amount": 50, "currency": "EUR"},
            },
            {
                **copy.deepcopy(common),
                "observation_id": "offer-fresh",
                "provider": "Fresh Seller",
                "provider_offer_id": "fresh-offer",
                "url": "https://fresh.example/offer",
                "observed_at": iso(now - timedelta(hours=1)),
                "expires_at": iso(now + timedelta(hours=3)),
                "price": {"amount": 150, "currency": "EUR"},
            },
        ],
    }


class OneWayKernelTests(unittest.TestCase):
    def test_question_attempts_store_references_not_observation_copies(self) -> None:
        state = new_session()
        task = baseline_task(state)
        bundle = no_result_bundle(task, "bundle-reference-only", task["source_suggestions"][0])
        compact = LIVE.ingest_evidence(state, bundle)
        question = next(
            item
            for item in compact["research_questions"]
            if item["task_id"] == task["task_id"]
        )
        self.assertEqual(question["question_status"], "closed")
        self.assertEqual(question["last_attempt_status"], "no_result")
        self.assertEqual(question["attempt_bundle_ids"], ["bundle-reference-only"])
        self.assertNotIn("attempts", question)
        self.assertNotIn("attempt_count", question)
        self.assertNotIn("status", question)
        self.assertEqual(
            compact["evidence_bundles"][-1]["observations"][0]["evidence_status"],
            "no_result",
        )
        self.assertEqual(LIVE.compact_session(compact), compact)
        expanded = LIVE.validate_session(compact)
        expanded_question = next(
            item for item in expanded["research_tasks"] if item["task_id"] == task["task_id"]
        )
        self.assertEqual(expanded_question["status"], "no_result")
        self.assertEqual(expanded["budget_ledger"]["attempted_count"], 1)

    def test_task_local_attempt_identity_and_source_challenger(self) -> None:
        state = new_session()
        task = baseline_task(state)
        first_source, second_source = task["source_suggestions"][:2]
        first = no_result_bundle(task, "bundle-first", first_source)

        state = LIVE.ingest_evidence(state, first)
        state = LIVE.validate_session(state)
        task = next(item for item in state["research_tasks"] if item["task_id"] == task["task_id"])
        self.assertEqual(task["attempt_count"], 1)
        self.assertEqual(task["attempt_bundle_ids"], ["bundle-first"])

        state = LIVE.record_hypothesis_decision(
            state,
            "hard_dead",
            "The first source returned no result for the issued scope.",
            hypothesis_id=task["hypothesis_id"],
        )
        state = LIVE.reopen_task_for_source_challenger(
            state,
            task["task_id"],
            "Try the next unattempted capability-matched source.",
        )
        state = LIVE.validate_session(state)
        task = next(item for item in state["research_tasks"] if item["task_id"] == task["task_id"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["attempt_count"], 1)
        self.assertEqual(task["assigned_source"]["source_id"], second_source["source_id"])
        self.assertEqual(task["last_reopen"]["next_attempt_number"], 2)

        second = no_result_bundle(task, "bundle-second", second_source)
        state = LIVE.ingest_evidence(state, second)
        state = LIVE.validate_session(state)
        task = next(item for item in state["research_tasks"] if item["task_id"] == task["task_id"])
        self.assertEqual(task["attempt_count"], 2)
        self.assertEqual(task["attempt_bundle_ids"], ["bundle-first", "bundle-second"])

    def test_hard_dead_and_deferred_are_distinct_product_dispositions(self) -> None:
        for disposition in ("hard_dead", "deferred_low_value"):
            with self.subTest(disposition=disposition):
                state = new_session()
                task = baseline_task(state)
                for item in state["research_tasks"]:
                    item["status"] = "candidate"
                task["status"] = "pending"
                state = LIVE.record_hypothesis_decision(
                    state,
                    "select",
                    "Inspect this hypothesis.",
                    task_id=task["task_id"],
                )
                state = LIVE.validate_session(state)
                current = next(
                    item
                    for item in state["research_tasks"]
                    if item["task_id"] == task["task_id"]
                )
                current["status"] = "no_result"
                LIVE._update_active_hypothesis_after_ingest(state, current, [])
                self.assertEqual(
                    state["active_hypothesis"]["available_dispositions"],
                    ["hard_dead", "deferred_low_value"],
                )
                state = LIVE.record_hypothesis_decision(
                    state,
                    disposition,
                    f"Record {disposition} explicitly.",
                    hypothesis_id=task["hypothesis_id"],
                )
                state = LIVE.validate_session(state)
                self.assertEqual(
                    state["hypothesis_outcomes"][task["hypothesis_id"]]["status"],
                    disposition,
                )

    def test_duplicate_evidence_bundle_replay_is_idempotent(self) -> None:
        state = new_session()
        task = baseline_task(state)
        bundle = no_result_bundle(task, "bundle-idempotent", task["source_suggestions"][0])
        state = LIVE.ingest_evidence(state, bundle)
        replayed = LIVE.ingest_evidence(state, copy.deepcopy(bundle))
        replayed_view = LIVE.validate_session(replayed)
        state_view = LIVE.validate_session(state)
        self.assertEqual(replayed_view["search_round"], state_view["search_round"])
        self.assertEqual(
            len(replayed_view["evidence_bundles"]),
            len(state_view["evidence_bundles"]),
        )
        self.assertEqual(
            sum(task["attempt_count"] for task in replayed_view["research_tasks"]),
            sum(task["attempt_count"] for task in state_view["research_tasks"]),
        )

        conflicting = copy.deepcopy(state["evidence_bundles"][-1])
        conflicting["observations"][0]["reason"] = "Different content"
        with self.assertRaisesRegex(LIVE.LiveProviderError, "different content"):
            LIVE.ingest_evidence(state, conflicting)

    def test_export_recomputes_offer_freshness_and_selects_fresh_alternative(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        state = new_session()
        task = baseline_task(state)
        state = LIVE.validate_session(
            LIVE.ingest_evidence(
                state,
                priced_alternative_bundle(task, now=now),
            )
        )
        self.assertTrue(state["result"]["routes"])
        self.assertEqual(state["result"]["routes"][0]["total_trip_cost"], 50)

        edge = state["edges"][0]
        snapshots = {
            snapshot["observation_id"]: snapshot
            for offer in edge["offer_set"]["offers"]
            for snapshot in offer["snapshots"]
        }
        snapshots["offer-cheap"]["expires_at"] = (
            now - timedelta(minutes=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        before_evaluation = copy.deepcopy(state)
        first_evaluation = LIVE._evaluate_solver_result(state, now)
        second_evaluation = LIVE._evaluate_solver_result(state, now)
        self.assertEqual(first_evaluation["result"], second_evaluation["result"])
        self.assertEqual(state, before_evaluation)
        fresh_audit = LIVE._finalization_audit(
            state,
            evaluated_at=now,
            evaluation=first_evaluation,
        )
        self.assertEqual(fresh_audit["route_count"], 1)
        self.assertNotIn(
            "complete_candidate_offer_expired",
            {issue["code"] for issue in fresh_audit["issues"]},
        )

        exported = LIVE._export_result(state, search_status="draft")
        self.assertTrue(exported["routes"])
        self.assertEqual(exported["routes"][0]["total_trip_cost"], 150)
        self.assertEqual(
            exported["itinerary_offer_sets"][0]["offer_set"]["selected_provider"],
            "Fresh Seller",
        )
        self.assertEqual(exported["active_incumbent"]["total_trip_cost"], 150)
        self.assertEqual(state["result"]["routes"][0]["total_trip_cost"], 50)

        snapshots["offer-fresh"]["expires_at"] = (
            now - timedelta(seconds=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        audit = LIVE._finalization_audit(state)
        issue_codes = {issue["code"] for issue in audit["issues"]}
        self.assertIn("complete_candidate_offer_expired", issue_codes)
        self.assertIn("no_complete_candidate", issue_codes)
        self.assertEqual(audit["route_count"], 0)

    def test_baseline_challenger_and_fresh_stop_allow_completion(self) -> None:
        state = new_session()
        for item in state["research_tasks"]:
            item["status"] = "candidate"
        baseline = baseline_task(state)
        baseline["status"] = "pending"
        state = LIVE.record_hypothesis_decision(
            state, "select", "Establish the complete-trip baseline.", task_id=baseline["task_id"]
        )
        state = LIVE.validate_session(state)
        baseline = next(
            item for item in state["research_tasks"] if item["task_id"] == baseline["task_id"]
        )
        state = LIVE.validate_session(
            LIVE.ingest_evidence(
                state,
                priced_alternative_bundle(
                    baseline,
                    now=datetime.now(timezone.utc).replace(microsecond=0),
                ),
            )
        )
        baseline = next(
            item for item in state["research_tasks"] if item["task_id"] == baseline["task_id"]
        )
        self.assertEqual(baseline["status"], "verified_observed")
        self.assertTrue(state["result"]["routes"])
        state = LIVE.record_hypothesis_decision(
            state,
            "complete",
            "The complete-trip baseline produced a verified route.",
            hypothesis_id=baseline["hypothesis_id"],
        )
        state = LIVE.validate_session(state)

        challenger = next(
            item
            for item in state["research_tasks"]
            if item.get("hypothesis_id")
            and item.get("hypothesis_id") != baseline.get("hypothesis_id")
        )
        challenger["status"] = "candidate"
        state = LIVE.record_hypothesis_decision(
            state,
            "select",
            "Evaluate one materially different challenger.",
            task_id=challenger["task_id"],
        )
        state = LIVE.validate_session(state)
        state = LIVE.record_hypothesis_decision(
            state,
            "deferred_low_value",
            "The challenger is valid to defer after comparison.",
            hypothesis_id=challenger["hypothesis_id"],
        )
        state = LIVE.validate_session(state)
        self.assertFalse(LIVE._finalization_audit(state)["can_finalize"])
        state = LIVE.record_stop_assessment(
            state,
            "The verified baseline remains best after the selected challenger.",
            [challenger["hypothesis_id"]],
        )
        self.assertTrue(LIVE._finalization_audit(state)["can_finalize"])
        state, result = LIVE.finalize_result(state, "Product completion gate passed.")
        self.assertEqual(state["finalization"]["status"], "final")
        self.assertEqual(result["search_status"], "final")

    def test_final_artifact_recovery_uses_frozen_projection_after_offer_expiry(
        self,
    ) -> None:
        from flight_search_harness.adapters.one_way import OneWayProductAdapter

        state = new_session()
        for item in state["research_tasks"]:
            item["status"] = "candidate"
        baseline = baseline_task(state)
        baseline["status"] = "pending"
        state = LIVE.record_hypothesis_decision(
            state,
            "select",
            "Establish the complete-trip baseline.",
            task_id=baseline["task_id"],
        )
        state = LIVE.validate_session(state)
        baseline = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == baseline["task_id"]
        )
        state = LIVE.validate_session(
            LIVE.ingest_evidence(
                state,
                priced_alternative_bundle(
                    baseline,
                    now=datetime.now(timezone.utc).replace(microsecond=0),
                ),
            )
        )
        baseline = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == baseline["task_id"]
        )
        state = LIVE.record_hypothesis_decision(
            state,
            "complete",
            "The complete-trip baseline produced a verified route.",
            hypothesis_id=baseline["hypothesis_id"],
        )
        state = LIVE.validate_session(state)
        challenger = next(
            item
            for item in state["research_tasks"]
            if item.get("hypothesis_id")
            and item.get("hypothesis_id") != baseline.get("hypothesis_id")
        )
        challenger["status"] = "candidate"
        state = LIVE.record_hypothesis_decision(
            state,
            "select",
            "Evaluate one materially different challenger.",
            task_id=challenger["task_id"],
        )
        state = LIVE.validate_session(state)
        state = LIVE.record_hypothesis_decision(
            state,
            "deferred_low_value",
            "The challenger is valid to defer after comparison.",
            hypothesis_id=challenger["hypothesis_id"],
        )
        state = LIVE.validate_session(state)
        state = LIVE.record_stop_assessment(
            state,
            "The verified baseline remains best after the selected challenger.",
            [challenger["hypothesis_id"]],
        )
        state, finalized = LIVE.finalize_result(
            state, "Product completion gate passed."
        )
        self.assertNotIn("result_projection", state["finalization"])
        self.assertIsInstance(state["finalization"].get("evaluated_at"), str)

        expired_state = copy.deepcopy(state)
        recovery_now = datetime.now(timezone.utc) + timedelta(days=1)

        with tempfile.TemporaryDirectory() as raw_directory:
            product_directory = Path(raw_directory)
            (product_directory / "session.json").write_text(
                json.dumps(expired_state, ensure_ascii=False),
                encoding="utf-8",
            )
            (product_directory / "final.json").write_bytes(
                (json.dumps(finalized, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                )
            )
            original_final_bytes = (product_directory / "final.json").read_bytes()
            (product_directory / "final.json").unlink()
            adapter = OneWayProductAdapter(product_root=PRODUCT_ROOT)

            class RecoveryClock(datetime):
                @classmethod
                def now(cls, tz: timezone | None = None) -> datetime:
                    return recovery_now if tz is not None else recovery_now.replace(tzinfo=None)

            adapter.live.datetime = RecoveryClock
            loaded = adapter.load_state(product_directory)

            recovered = adapter.completed_result(product_directory, loaded)

            self.assertEqual(finalized, recovered)
            self.assertEqual(
                finalized,
                json.loads(
                    (product_directory / "final.json").read_text(encoding="utf-8")
                ),
            )
            self.assertEqual(
                original_final_bytes,
                (product_directory / "final.json").read_bytes(),
            )
            self.assertEqual(
                state["finalization"]["history"],
                json.loads(
                    (product_directory / "session.json").read_text(encoding="utf-8")
                )["finalization"]["history"],
            )
            persisted_session = json.loads(
                (product_directory / "session.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "result_projection", persisted_session["finalization"]
            )
            self.assertEqual(
                state["finalization"]["evaluated_at"],
                persisted_session["finalization"]["evaluated_at"],
            )

    def test_temporal_and_multimodal_constraints_survive_materialization(self) -> None:
        state = new_session(date_mode="exact_dates")
        self.assertEqual(state["profile"]["date_flexibility"]["mode"], "exact_dates")
        self.assertEqual(
            state["profile"]["date_flexibility"]["exact_departure_dates"],
            ["2027-03-12", "2027-03-16"],
        )
        self.assertEqual(state["profile"]["allowed_modes"], ["flight", "rail", "bus"])
        self.assertEqual(
            state["config"]["search"]["mode_transition_buffers_minutes"][
                "surface_to_flight"
            ],
            100,
        )
        actionable = [
            task
            for task in state["research_tasks"]
            if task.get("status") in {"candidate", "pending"}
        ]
        self.assertTrue(actionable)
        self.assertTrue(
            all(task.get("date_search", {}).get("mode") == "exact_dates" for task in actionable)
        )
        self.assertTrue(
            all(set(task.get("suggested_modes", [])) <= {"flight", "rail", "bus"} for task in actionable)
        )


if __name__ == "__main__":
    unittest.main()
