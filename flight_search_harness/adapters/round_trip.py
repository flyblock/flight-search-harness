"""Product Adapter for the independent fixed round-trip planner."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..contracts import (
    HarnessProtocolError,
    _build_adapter_choice_item,
    _build_adapter_research_item,
    _observation_bundle_schema,
    _plan_adapter_transition,
)
from ..observation_bundle import normalize_attempt
from .. import fixed_route_provider as provider_foundation
from ..fx_rates import system_fx_context
from ..request_values import (
    canonical_region_side,
    canonical_shared_intent,
    canonical_window,
    derived_intent_id,
    money_in_base_currency,
    round_trip_baggage,
)
from ..suite_layout import SuiteLayout


TERMINAL_TASK_STATUSES = {
    "ai_proposed",
    "verified_observed",
    "research_observed",
    "no_result",
    "rejected",
    "superseded",
    "covered_by_hypothesis_disposition",
}


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HarnessProtocolError(f"cannot load product module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class RoundTripProductAdapter:
    product_id = "round_trip"
    journey_shape = "round_trip"

    def __init__(
        self,
        *,
        product_root: str | Path | None = None,
    ):
        self.product_root = (
            Path(product_root).resolve()
            if product_root is not None
            else SuiteLayout.discover(anchor=__file__).runtime_root(self.product_id)
        )
        scripts = self.product_root / "scripts"
        self.materializer = _load_module(
            "flight_search_harness_round_materializer", scripts / "round_trip_intent_materializer.py"
        )
        self.session = _load_module(
            "flight_search_harness_round_session", scripts / "round_trip_session.py"
        )
        self.briefs = _load_module(
            "flight_search_harness_round_briefs", scripts / "round_trip_browser_task_brief.py"
        )
        self.evidence_adapter = _load_module(
            "flight_search_harness_round_evidence", scripts / "round_trip_evidence_adapter.py"
        )
        self.temporal = _load_module(
            "flight_search_harness_round_temporal", scripts / "round_trip_temporal_scheduler.py"
        )

    @staticmethod
    def _session_path(product_directory: Path) -> Path:
        return product_directory / "session.json"

    @staticmethod
    def _final_path(product_directory: Path) -> Path:
        return product_directory / "final.json"

    @staticmethod
    def _finalization_intent_path(product_directory: Path) -> Path:
        return product_directory / "finalization-intent.json"

    def admission_state_identity(self, product_directory: Path) -> str:
        try:
            content = self._session_path(product_directory).read_bytes()
        except OSError as exc:
            raise HarnessProtocolError(
                f"cannot identify round-trip Session state: {exc}"
            ) from exc
        return hashlib.sha256(content).hexdigest()

    def _remember_finalization_reason(
        self, product_directory: Path, reason: str
    ) -> None:
        _write_json_atomic(
            self._finalization_intent_path(product_directory),
            {"intent_version": "1.0", "reason": reason},
        )

    def _finalization_reason(
        self,
        product_directory: Path,
        state: dict[str, Any],
        fallback: str | None = None,
    ) -> str:
        reason = state.get("finalization_reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        target = self._finalization_intent_path(product_directory)
        if target.exists():
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot read round-trip finalization intent: {exc}"
                ) from exc
            reason = value.get("reason") if isinstance(value, dict) else None
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        return "Recovered finalized round-trip Product Session artifact."

    def _materialize_final_result(
        self,
        product_directory: Path,
        state: dict[str, Any],
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        reason = self._finalization_reason(
            product_directory, state, fallback_reason
        )
        try:
            result = self.session.export_result(
                state, search_status="final", reason=reason
            )
        except self.session.RoundTripSessionError as exc:
            raise HarnessProtocolError(
                f"round-trip final result recovery failed: {exc}"
            ) from exc
        existing = self.read_final_result(product_directory)
        if existing != result:
            _write_json_atomic(self._final_path(product_directory), result)
        return result

    def initialize(self, request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        if request.get("journey_shape") != self.journey_shape:
            raise HarnessProtocolError(
                "RoundTripProductAdapter requires journey_shape=round_trip"
            )
        intent = request.get("intent")
        if not isinstance(intent, dict):
            raise HarnessProtocolError("round-trip request.intent must be an object")
        shared = canonical_shared_intent(intent)
        fx_context = system_fx_context(shared["base_currency"])
        shared["fx_to_base"] = fx_context["rates"]
        product_intent = copy.deepcopy(intent)
        if fx_context["snapshot"] is not None:
            product_intent["fx_snapshot"] = copy.deepcopy(fx_context["snapshot"])
        origin = canonical_region_side(intent, "origin")
        destination = canonical_region_side(intent, "destination")
        for field, side in (("origin", origin), ("destination", destination)):
            side["max_ground_cost"] = money_in_base_currency(
                side.get("max_ground_cost"), shared, f"intent.{field}.max_ground_cost"
            )
        if "budget" in product_intent:
            product_intent["budget"] = money_in_base_currency(
                product_intent.get("budget"), shared, "intent.budget"
            )
        product_intent.update(
            {
                "intent_id": derived_intent_id(self.journey_shape, intent),
                "trip_scope": "round_trip",
                "trip_mode": "fixed_destination",
                "currency": shared["base_currency"],
                "traveler_count": shared["adult_count"],
                "baggage_policy": round_trip_baggage(shared["baggage_policy"]),
                "overnight_allowed": shared["overnight_allowed"],
                "self_transfer_policy": shared["self_transfer_policy"],
                "origin": origin,
                "destination": destination,
                "outbound_departure_window": canonical_window(
                    intent, "outbound_departure_window"
                ),
                "return_departure_window": canonical_window(
                    intent, "return_departure_window"
                ),
            }
        )
        product_intent["date_flexibility"] = self._round_trip_date_flexibility(
            intent.get("outbound_exact_departure_dates"),
            intent.get("return_exact_departure_dates"),
        )
        for public_field in ("adult_count", "base_currency", "fx_to_base"):
            product_intent.pop(public_field, None)
        product_intent.pop("outbound_exact_departure_dates", None)
        product_intent.pop("return_exact_departure_dates", None)
        scenario = self.materializer.materialize_intent(product_intent)
        state = self.session.initialize_session(scenario)
        _write_json_atomic(self._session_path(product_directory), state)
        return state

    @staticmethod
    def _round_trip_date_flexibility(
        outbound: Any,
        returning: Any,
    ) -> dict[str, Any]:
        def normalize(value: Any, field: str) -> dict[str, Any]:
            if value is None:
                return {"mode": "bounded_window"}
            if not isinstance(value, list) or not value:
                raise HarnessProtocolError(
                    f"intent.{field} must be a non-empty array"
                )
            return {
                "mode": "exact_dates",
                "exact_departure_dates": copy.deepcopy(value),
            }

        outbound_value = normalize(outbound, "outbound_exact_departure_dates")
        return_value = normalize(returning, "return_exact_departure_dates")
        result: dict[str, Any] = {
            "mode": (
                outbound_value["mode"]
                if outbound_value["mode"] == return_value["mode"]
                else "independent"
            ),
            "outbound_mode": outbound_value["mode"],
            "return_mode": return_value["mode"],
        }
        if "exact_departure_dates" in outbound_value:
            result["outbound_exact_departure_dates"] = outbound_value[
                "exact_departure_dates"
            ]
        if "exact_departure_dates" in return_value:
            result["return_exact_departure_dates"] = return_value[
                "exact_departure_dates"
            ]
        return result

    def load_state(self, product_directory: Path) -> dict[str, Any]:
        try:
            raw = json.loads(self._session_path(product_directory).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessProtocolError(f"cannot load round-trip Session: {exc}") from exc
        return self.session.validate_session(raw)

    def _save_state(self, product_directory: Path, state: dict[str, Any]) -> None:
        _write_json_atomic(self._session_path(product_directory), state)

    def inspect(self, state: dict[str, Any]) -> dict[str, Any]:
        state = self.session.validate_session(state)
        audit = self.session._finalization_audit(state)
        catalog = self.briefs.build_catalog(state)
        actions: list[dict[str, Any]] = []
        investigations: list[dict[str, Any]] = []
        active = state.get("active_hypothesis")
        active_id = active.get("hypothesis_id") if isinstance(active, dict) else None
        active_node = (
            state["hypothesis_tree"]["nodes"].get(active_id, {})
            if active_id
            else {}
        )
        mandatory_joint_task_ids = {
            str(task["task_id"])
            for task in state["research_tasks"]
            if active_id
            and active_node.get("hypothesis_kind") == "complete_journey"
            and task.get("hypothesis_id") == active_id
            and task.get("journey_scope") == "joint_round_trip"
            and task.get("status") in {"candidate", "pending"}
        }
        for hypothesis in catalog.get("hypotheses", []):
            if (
                active_id is None
                and hypothesis.get("hypothesis_kind") == "complete_journey"
            ):
                continue
            for task in hypothesis.get("tasks", []):
                if (
                    mandatory_joint_task_ids
                    and task.get("task_id") not in mandatory_joint_task_ids
                ):
                    continue
                action = {
                    "kind": "investigate",
                    "action_key": f"investigate:{task['task_id']}",
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "sequence": next(
                        (
                            index
                            for index, item in enumerate(state["research_tasks"], 1)
                            if item["task_id"] == task["task_id"]
                        ),
                        0,
                    ),
                }
                investigations.append(action)
                actions.append(action)

        disposition_actions: list[dict[str, Any]] = []
        if active_id:
            node = state["hypothesis_tree"]["nodes"][active_id]
            active_tasks = [
                task for task in state["research_tasks"] if task["hypothesis_id"] == active_id
            ]
            unfinished = [
                task
                for task in active_tasks
                if task.get("required", True)
                and task["status"] not in TERMINAL_TASK_STATUSES
            ]
            if node["hypothesis_kind"] == "gateway_discovery":
                side = "origin" if active_id == self.session.ORIGIN_GATEWAY_HYPOTHESIS else "destination"
                admitted = any(
                    candidate.get("evidence_status")
                    in {"ai_proposed", "research_observed", "exact_anchor"}
                    for candidate in state["gateway_candidates"][side]
                )
                unresolved_scope = any(
                    status == "pending"
                    for status in state.get("region_scopes", {})
                    .get(side, {})
                    .get("coverage", {})
                    .values()
                )
                if admitted and not unfinished and not unresolved_scope:
                    disposition = "complete"
                elif not investigations:
                    disposition = "hard_dead"
                else:
                    disposition = None
            elif not unfinished and self.session._supporting_portfolio_exists(state, active_id):
                disposition = "complete"
            elif not investigations:
                disposition = "hard_dead"
            else:
                disposition = None
            if disposition:
                action = {
                    "kind": "dispose",
                    "action_key": f"dispose:{active_id}:{disposition}",
                    "hypothesis_id": active_id,
                    "disposition": disposition,
                    "sibling_order": int(node.get("sibling_order", 10**9)),
                }
                if disposition == "complete":
                    action["automatic"] = True
                disposition_actions.append(action)
                actions.append(action)
            screen_backed = bool(
                (node.get("advisory_basis") or {}).get("screen_signal_id")
            )
            if (
                not mandatory_joint_task_ids
                and disposition != "complete"
                and not screen_backed
            ):
                actions.append(
                    {
                        "kind": "dispose",
                        "action_key": f"dispose:{active_id}:deferred_low_value",
                        "hypothesis_id": active_id,
                        "disposition": "deferred_low_value",
                    }
                )

        if active_id is None:
            suggestions = catalog.get("route_hypothesis_suggestions", [])
            if suggestions:
                baseline = {
                    "kind": "activate_route_hypothesis",
                    "action_key": "activate_route_hypothesis:baseline",
                    "hypothesis_spec": copy.deepcopy(suggestions[0]),
                    "automatic": True,
                    "reason": (
                    "ProductKernel registered the first complete journey from "
                    "the admitted gateway baseline."
                    ),
                }
                actions.append(baseline)

        if active_id:
            for task in state["research_tasks"]:
                if task["hypothesis_id"] != active_id:
                    continue
                temporal_state = task.get("temporal_state", {})
                if temporal_state.get("phase") != "surface_discovery" or not temporal_state.get("signal_refs"):
                    continue
                temporal_catalog = self.temporal.schedule_round_trip_date_pairs(state)
                for candidate in temporal_catalog["candidate_catalog"]["candidates"]:
                    selection = {
                        "candidate_id": candidate["candidate_id"],
                        "origin": "scheduler_candidate",
                        "basis_signal_ids": [
                            item["signal_id"] for item in candidate["research_signal_refs"]
                        ],
                    }
                    if task.get("journey_scope") == "joint_round_trip":
                        selection.update(
                            {
                                "outbound_date": candidate["outbound_date"],
                                "return_date": candidate["return_date"],
                            }
                        )
                    else:
                        selection["departure_date"] = (
                            candidate["outbound_date"]
                            if task.get("direction") == "outbound"
                            else candidate["return_date"]
                        )
                    actions.append(
                        {
                            "kind": "select_temporal",
                            "action_key": f"select_temporal:{task['task_id']}:{candidate['candidate_id']}",
                            "task_id": task["task_id"],
                            "selection": selection,
                        }
                    )

        blockers = list(audit.get("blockers", []))
        pending_route_suggestions = catalog.get("route_hypothesis_suggestions", [])
        can_finalize = bool(audit["can_finalize"] and not pending_route_suggestions)
        if audit["can_finalize"] and pending_route_suggestions:
            blockers.append(
                "unverified regional fare-screen candidates may still beat the incumbent"
            )
        if can_finalize:
            actions = [
                {
                    "kind": "finalize",
                    "action_key": "finalize",
                    "automatic": True,
                }
            ]
            investigations = []
            disposition_actions = []
        return {
            "control_version": "1.0",
            "product_id": self.product_id,
            "session_id": state["session_id"],
            "active_hypothesis_id": active_id,
            "next_candidate_id": state["hypothesis_tree"].get("next_candidate_id"),
            "has_complete_result": state["result"].get("best_portfolio") is not None,
            "can_finalize": can_finalize,
            "finalization_issues": blockers,
            "investigation_actions": investigations,
            "disposition_actions": disposition_actions,
            "actions": actions,
        }

    def completed_result(
        self,
        product_directory: Path,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        if state.get("finalization_status") != "final":
            return None
        return self._materialize_final_result(product_directory, state)

    def plan_transition(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
    ) -> dict[str, Any]:
        investigations = [
            copy.deepcopy(action)
            for action in control.get("investigation_actions", [])
            if isinstance(action, dict)
        ]
        selected = (
            self._select_investigation_batch(state, investigations)
            if investigations
            else []
        )
        return _plan_adapter_transition(
            control,
            investigations,
            selected,
            choose_when_mixed=False,
        )

    def build_choice_item(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        actions: list[dict[str, Any]],
        identify: Callable[[str, Any], str],
    ) -> dict[str, Any]:
        return _build_adapter_choice_item(
            control,
            actions,
            identify,
            session_identity=state.get("session_id") or state.get("run_id"),
            round_identity=state.get("search_round") or state.get("current_round"),
            browser_attempt_count=len(state.get("evidence_bundles", [])),
        )

    def build_research_items(
        self,
        product_directory: Path,
        investigations: list[dict[str, Any]],
        _reason: str,
        identify: Callable[[str, Any], str],
    ) -> list[dict[str, Any]]:
        items = []
        for investigation in investigations:
            task_id = str(investigation.get("task_id") or "")
            if not task_id:
                raise HarnessProtocolError("round-trip investigation action has no task_id")
            work = self.prepare_investigation(
                product_directory,
                task_id,
                "ProductKernel scheduled required Mission work.",
            )
            original_schema = copy.deepcopy(work.get("attempt_schema", {}))
            work["attempt_schema"] = {
                "oneOf": [original_schema, _observation_bundle_schema()]
            }
            state = self.load_state(product_directory)
            task = self._task_by_id(state, task_id)
            attempt_number = int(task.get("attempt_count", 0)) + 1
            research_kind = str(task.get("research_kind") or "")
            item_kind = (
                "semantic_judgment"
                if research_kind
                in {
                    "region_scope_definition",
                }
                else "browser_research"
            )
            items.append(
                _build_adapter_research_item(
                    task_id=task_id,
                    attempt_number=attempt_number,
                    session_identity=state.get("session_id") or state.get("run_id"),
                    item_kind=item_kind,
                    work=work,
                    hypothesis_id=task.get("hypothesis_id"),
                    research_kind=research_kind,
                    journey_scope=task.get("journey_scope"),
                    identify=identify,
                )
            )
        return items

    def normalize_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return normalize_attempt(self.product_id, attempt)

    def _select_investigation_batch(
        self,
        state: dict[str, Any],
        investigations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            investigations,
            key=lambda action: (
                0 if action.get("status") == "pending" else 1,
                int(action.get("sequence", 10**9)),
                str(action.get("task_id") or ""),
            ),
        )
        first = ordered[0]
        first_task = self._task_by_id(state, str(first["task_id"]))
        research_kind = first_task.get("research_kind")
        journey_scope = first_task.get("journey_scope")
        if research_kind == "priced_offer" and journey_scope == "one_way":
            limit = 2
        else:
            limit = 1
        result = []
        for action in ordered:
            task = self._task_by_id(state, str(action["task_id"]))
            if task.get("hypothesis_id") != first_task.get("hypothesis_id"):
                continue
            if task.get("research_kind") != research_kind:
                continue
            if research_kind == "priced_offer" and task.get("journey_scope") != journey_scope:
                continue
            result.append(action)
            if len(result) >= limit:
                break
        return result or [first]

    @staticmethod
    def _task_by_id(state: dict[str, Any], task_id: str) -> dict[str, Any]:
        task = next(
            (
                entry
                for entry in state.get("research_tasks", [])
                if isinstance(entry, dict) and str(entry.get("task_id")) == task_id
            ),
            None,
        )
        if task is None:
            raise HarnessProtocolError(f"round-trip task does not exist: {task_id}")
        return task

    def prepare_investigation(
        self, product_directory: Path, task_id: str, reason: str
    ) -> dict[str, Any]:
        state = self.load_state(product_directory)
        task = next(
            (item for item in state["research_tasks"] if item["task_id"] == task_id),
            None,
        )
        if task is None or task["status"] not in {"candidate", "pending"}:
            raise HarnessProtocolError(f"round-trip task is not actionable: {task_id}")
        active = state.get("active_hypothesis")
        if active is None:
            state = self.session.record_hypothesis_decision(
                state, "select", task_id=task_id, reason=reason
            )
            self._save_state(product_directory, state)
        elif active.get("hypothesis_id") != task["hypothesis_id"]:
            raise HarnessProtocolError("round-trip active hypothesis must be closed before switching")
        brief = self.briefs.task_to_brief(state, task_id)
        semantic = task.get("research_kind") in self.briefs.SEMANTIC_RESEARCH_KINDS
        pool_screen = task.get("research_kind") == "region_pool_fare_screen"
        if not semantic and not pool_screen:
            brief = provider_foundation.apply_foundation(
                brief,
                attempted_sources=provider_foundation.attempted_source_ids(
                    state, task_id
                ),
            )
        return {
            "work_version": "1.0",
            "product_id": self.product_id,
            "task_id": task_id,
            "hypothesis_id": task["hypothesis_id"],
            "brief": brief,
            "attempt_schema": {
                "type": "object",
                "required": ["kind", "product_payload"],
                "properties": {
                    "kind": {"const": "observations"},
                    "product_payload": {"type": "object"},
                },
                "additionalProperties": False,
            },
        }

    def ingest_attempt(
        self,
        product_directory: Path,
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        if attempt.get("kind") != "observations" or not isinstance(
            attempt.get("product_payload"), dict
        ):
            raise HarnessProtocolError("round-trip attempt requires product_payload")
        state = self.load_state(product_directory)
        current_task = next(
            item
            for item in state["research_tasks"]
            if item["task_id"] == str(action["task_id"])
        )
        pool_screen = current_task.get("research_kind") == "region_pool_fare_screen"
        if (
            current_task.get("research_kind")
            not in self.briefs.SEMANTIC_RESEARCH_KINDS
            and not pool_screen
        ):
            current_brief = provider_foundation.apply_foundation(
                self.briefs.task_to_brief(state, str(action["task_id"])),
                attempted_sources=provider_foundation.attempted_source_ids(
                    state, str(action["task_id"])
                ),
            )
            provider_foundation.validate_first_attempt(
                attempt,
                required_source=current_brief["provider_foundation"][
                    "required_source"
                ],
            )
            provider_foundation.validate_price_ordering(attempt)
        elif pool_screen:
            provider_foundation.validate_price_ordering(attempt)
        payload = copy.deepcopy(attempt["product_payload"])
        if pool_screen:
            # Pool completion belongs to the Product Kernel.  The Worker reports
            # Provider scopes and local minima; it never supplies the completion
            # decision or echoes the requested scope as proof.
            payload["coverage_complete"] = False
            payload.pop("submitted_scope", None)
        payload.setdefault("observation_version", self.session.SESSION_VERSION)
        payload["bundle_id"] = (
            f"bundle-{action.get('_receipt_id') or action['action_id']}"
        )
        payload["task_id"] = action["task_id"]
        try:
            evidence = self.evidence_adapter.adapt_observations(payload)
            state = self.session.ingest_evidence(
                self.load_state(product_directory), evidence
            )
        except (
            self.evidence_adapter.EvidenceAdapterError,
            self.session.RoundTripSessionError,
        ) as exc:
            raise HarnessProtocolError(f"round-trip evidence transaction failed: {exc}") from exc
        self._save_state(product_directory, state)
        return state

    def attempt_committed(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> bool:
        payload = attempt.get("product_payload", {})
        bundle_id = f"bundle-{action.get('_receipt_id') or action['action_id']}"
        existing = next(
            (
                bundle
                for bundle in state.get("evidence_bundles", [])
                if isinstance(bundle, dict) and bundle.get("bundle_id") == bundle_id
            ),
            None,
        )
        if existing is None:
            return False
        candidate_payload = copy.deepcopy(payload)
        task = self._task_by_id(state, str(action["task_id"]))
        if task.get("research_kind") == "region_pool_fare_screen":
            candidate_payload["coverage_complete"] = False
            candidate_payload.pop("submitted_scope", None)
        candidate_payload["bundle_id"] = bundle_id
        candidate_payload["task_id"] = action["task_id"]
        try:
            candidate = self.evidence_adapter.adapt_observations(candidate_payload)
        except self.evidence_adapter.EvidenceAdapterError as exc:
            raise HarnessProtocolError(
                f"round-trip replay Evidence is invalid: {exc}"
            ) from exc
        if existing != candidate:
            raise HarnessProtocolError(
                "round-trip Receipt identity was replayed with conflicting Evidence"
            )
        return True

    def apply_control_action(
        self, product_directory: Path, action: dict[str, Any]
    ) -> dict[str, Any]:
        state = self.load_state(product_directory)
        kind = action.get("kind")
        reason = str(action.get("reason") or "not recorded")
        try:
            if kind == "dispose":
                node = state["hypothesis_tree"]["nodes"].get(action.get("hypothesis_id"))
                if node and node.get("status") == action.get("disposition"):
                    return state
                state = self.session.record_hypothesis_decision(
                    state,
                    str(action.get("disposition")),
                    reason=reason,
                )
            elif kind == "activate_route_hypothesis":
                state = self.session.activate_route_hypothesis(
                    state,
                    copy.deepcopy(action.get("hypothesis_spec")),
                    reason,
                )
            elif kind == "select_temporal":
                task = next(
                    item
                    for item in state["research_tasks"]
                    if item["task_id"] == action.get("task_id")
                )
                active_selection = task.get("temporal_state", {}).get("active_selection")
                selection = copy.deepcopy(action.get("selection"))
                if active_selection and active_selection.get("candidate_id") == selection.get("candidate_id"):
                    return state
                state = self.session.record_temporal_selection(
                    state,
                    str(action.get("task_id")),
                    selection,
                    reason,
                )
            else:
                raise HarnessProtocolError(f"unsupported round-trip control action: {kind}")
        except self.session.RoundTripSessionError as exc:
            raise HarnessProtocolError(f"round-trip control action failed: {exc}") from exc
        self._save_state(product_directory, state)
        return state

    def finalize(self, product_directory: Path, reason: str) -> dict[str, Any]:
        state = self.load_state(product_directory)
        if state.get("finalization_status") == "final":
            return self._materialize_final_result(
                product_directory, state, reason
            )
        self._remember_finalization_reason(product_directory, reason)
        try:
            state, result = self.session.finalize_session(state, reason)
        except self.session.RoundTripSessionError as exc:
            raise HarnessProtocolError(f"round-trip finalization failed: {exc}") from exc
        state["finalization_reason"] = reason
        self._save_state(product_directory, state)
        return self._materialize_final_result(product_directory, state, reason)

    def read_final_result(self, product_directory: Path) -> dict[str, Any] | None:
        target = self._final_path(product_directory)
        if not target.exists():
            return None
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
