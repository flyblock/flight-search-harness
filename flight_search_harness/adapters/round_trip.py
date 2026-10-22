"""Product Adapter for the independent fixed round-trip planner."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..contracts import (
    HarnessProtocolError,
    _build_adapter_choice_item,
    _build_adapter_research_item,
    _plan_adapter_transition,
)
from ..observation_bundle import normalize_attempt
from .. import fixed_route_provider as provider_foundation
from ..fx_rates import system_fx_context
from ..request_values import (
    canonical_region_side,
    canonical_round_trip_travel_windows,
    canonical_shared_intent,
    derived_intent_id,
    money_in_base_currency,
    round_trip_window_envelope,
    round_trip_baggage,
    validate_inline_schema,
)
from ..suite_layout import SuiteLayout


TERMINAL_TASK_STATUSES = {
    "ai_proposed",
    "verified_observed",
    "research_observed",
    "no_result",
    "rejected",
    "infeasible",
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

    @staticmethod
    def _uses_flight_provider_foundation(task: dict[str, Any]) -> bool:
        query_shape = task.get("query_shape", {})
        return (
            task.get("research_kind")
            not in {
                "region_scope_definition",
                "region_pool_fare_screen",
                "internal_surface_transfer",
                "price_challenge_screen",
            }
            and task.get("journey_scope") != "surface"
            and not isinstance(query_shape.get("price_refresh"), dict)
        )

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
        if hasattr(self.session, "SESSION_VERSION"):
            try:
                product_root = getattr(self, "product_root", None) or (
                    SuiteLayout.discover(anchor=__file__).runtime_root(self.product_id)
                )
                result_schema = json.loads(
                    (product_root / "schemas" / "round-trip-result.schema.json")
                    .read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot load round-trip final result Schema: {exc}"
                ) from exc
            validate_inline_schema(
                result,
                result_schema,
                path="round_trip.final_result",
            )
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
        travel_windows = canonical_round_trip_travel_windows(intent)
        outbound_envelope, return_envelope = round_trip_window_envelope(
            travel_windows
        )
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
                "search_effort": shared["search_effort"],
                "origin": origin,
                "destination": destination,
                "travel_windows": travel_windows,
                "outbound_departure_window": outbound_envelope,
                "return_departure_window": return_envelope,
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
        product_intent.pop("travel_windows", None)
        product_intent["travel_windows"] = travel_windows
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
        price_refresh = False
        if active_id:
            node = state["hypothesis_tree"]["nodes"][active_id]
            frontier_probe = bool(
                (node.get("advisory_basis") or {}).get("frontier_probe")
            )
            price_challenge_probe = bool(
                (node.get("advisory_basis") or {}).get(
                    "price_challenge_probe"
                )
            )
            regional_challenger_probe = bool(
                (node.get("advisory_basis") or {}).get(
                    "regional_challenger_probe"
                )
            )
            bounded_probe = (
                frontier_probe
                or price_challenge_probe
                or regional_challenger_probe
            )
            price_refresh = bool(
                (node.get("advisory_basis") or {}).get("price_refresh")
            )
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
                if admitted and not unfinished:
                    disposition = "complete"
                elif not investigations:
                    disposition = "hard_dead"
                else:
                    disposition = None
            elif price_refresh and not unfinished:
                refresh_resolution = self.session.assess_price_refresh_resolution(
                    state, active_id
                )
                disposition = (
                    "complete"
                    if refresh_resolution.get("disposition")
                    in {"ready_to_commit", "unavailable_fallback"}
                    else None
                )
            elif (
                bounded_probe
                and not unfinished
            ):
                disposition = "deferred_low_value"
            elif not unfinished and self.session._supporting_portfolio_exists(state, active_id):
                disposition = "complete"
            elif not investigations and not price_refresh:
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
                if disposition == "complete" or bounded_probe:
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
                and not bounded_probe
                and not price_refresh
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
        remaining_credits = int(
            state.get("research_credits", {}).get("remaining", 0)
        )
        unaffordable_investigations = bool(investigations) and all(
            self.session.research_credit_cost(
                self._task_by_id(state, str(action["task_id"]))
            )
            > remaining_credits
            for action in investigations
        )
        if (
            state["result"].get("best_portfolio") is not None
            and unaffordable_investigations
            and not price_refresh
        ):
            actions = [
                {
                    "kind": "stop_bounded_research",
                    "action_key": "stop_bounded_research:budget",
                    "automatic": True,
                    "reason": (
                        "The remaining Research Credits cannot fund the next "
                        "price challenge."
                    ),
                    "stop_disposition": "budget_stop",
                }
            ]
            investigations = []
            disposition_actions = []
        elif (
            state["result"].get("best_portfolio") is not None
            and active_id is None
            and not investigations
            and not pending_route_suggestions
            and not state.get("research_credits", {}).get("stop_reason")
            and not audit["can_finalize"]
        ):
            stop_reason = (
                "No unresearched candidate clears the active Price Challenge "
                "Threshold; remaining unproved scope is disclosed."
            )
            preview = self.session.stop_bounded_research(
                state,
                stop_reason,
                "threshold_stop",
            )
            if self.session._finalization_audit(preview)["can_finalize"]:
                actions = [
                    {
                        "kind": "stop_bounded_research",
                        "action_key": "stop_bounded_research:threshold",
                        "automatic": True,
                        "reason": stop_reason,
                        "stop_disposition": "threshold_stop",
                    }
                ]
                investigations = []
                disposition_actions = []
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
        if (
            state.get("result", {}).get("best_portfolio") is None
            and int(state.get("research_credits", {}).get("remaining", 0)) <= 0
        ):
            return {
                "kind": "draft",
                "reason": (
                    "Research Credits are exhausted before a verified Journey "
                    "Alternative was found."
                ),
                "action_required": {"type": "increase_search_effort"},
            }
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
        if (
            investigations
            and not selected
            and state.get("result", {}).get("best_portfolio") is None
        ):
            return {
                "kind": "draft",
                "reason": (
                    "Research Credits are exhausted before a verified Journey "
                    "Alternative was found."
                ),
                "action_required": {"type": "increase_search_effort"},
            }
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
            items[-1]["payload"]["research_credit_cost"] = (
                self.session.research_credit_cost(task)
            )
            items[-1]["payload"]["research_credit_budget_kind"] = (
                "price_refresh"
                if isinstance(
                    task.get("query_shape", {}).get("price_refresh"), dict
                )
                else "search"
            )
        if items:
            state = self.load_state(product_directory)
            for item in items:
                state = self.session.reserve_research_credit(
                    state,
                    item_id=str(item["item_id"]),
                    task_id=str(item["payload"]["task_id"]),
                    cost=int(item["payload"].get("research_credit_cost", 0)),
                    budget_kind=str(
                        item["payload"].get(
                            "research_credit_budget_kind", "search"
                        )
                    ),
                )
            self._save_state(product_directory, state)
        return items

    def record_research_receipt(
        self,
        product_directory: Path,
        item: dict[str, Any],
        receipt: dict[str, Any],
    ) -> None:
        """Commit or release the Product-owned Research Credit reservation."""

        if item.get("kind") not in {"browser_research", "semantic_judgment"}:
            return
        state = self.session.record_research_receipt(
            self.load_state(product_directory),
            item_id=str(item.get("item_id") or ""),
            execution_origin=(
                str(receipt.get("execution_origin"))
                if receipt.get("execution_origin") is not None
                else None
            ),
            completed=receipt.get("status") == "complete",
        )
        self._save_state(product_directory, state)

    def normalize_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_attempt(self.product_id, attempt)
        if normalized.get("kind") != "observations":
            return normalized
        payload = normalized.get("product_payload")
        observations = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(observations, list):
            return normalized
        for observation in observations:
            if not isinstance(observation, dict) or observation.get(
                "evidence_status"
            ) == "ai_proposed":
                continue
            source_id = self.evidence_adapter.resolve_source_id(
                observation.get("url"), observation.get("source_id")
            )
            if source_id is not None:
                observation["source_id"] = source_id
        return normalized

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
        price_refresh = isinstance(
            first_task.get("query_shape", {}).get("price_refresh"), dict
        )
        if research_kind == "priced_offer" and price_refresh:
            limit = 3
        elif research_kind == "region_pool_fare_screen":
            # Multiple date alternatives share one Browser state while each
            # query remains an independent Work Item and Research Credit.
            limit = 3
        elif research_kind == "priced_offer" and journey_scope in {
            "one_way",
            "surface",
        }:
            limit = 2
        else:
            limit = 1
        remaining_credits = int(
            state.get("research_credits", {}).get("remaining", 30)
        )
        result = []
        for action in ordered:
            task = self._task_by_id(state, str(action["task_id"]))
            if task.get("hypothesis_id") != first_task.get("hypothesis_id"):
                continue
            if task.get("research_kind") != research_kind:
                continue
            if (
                research_kind == "priced_offer"
                and not price_refresh
                and task.get("journey_scope") != journey_scope
            ):
                continue
            if price_refresh and not isinstance(
                task.get("query_shape", {}).get("price_refresh"), dict
            ):
                continue
            credit_cost = self.session.research_credit_cost(task)
            if credit_cost > remaining_credits and not price_refresh:
                continue
            result.append(action)
            if not price_refresh:
                remaining_credits -= credit_cost
            if len(result) >= limit:
                break
        return result

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
        if self._uses_flight_provider_foundation(task):
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
                    "product_payload": copy.deepcopy(
                        brief["attempt_product_schema"]
                    ),
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
        if self._uses_flight_provider_foundation(current_task):
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
        elif current_task.get("journey_scope") == "surface":
            provider_foundation.validate_price_ordering(attempt)
        payload = copy.deepcopy(attempt["product_payload"])
        payload = self._prepare_product_payload(
            current_task,
            payload,
            trusted_at=action.get("_receipt_received_at"),
        )
        if pool_screen:
            # Pool completion belongs to the Product Kernel. The Worker reports
            # the visible local minimum and witness; the Adapter derives scope
            # from the immutable Work Item rather than asking the Worker to echo it.
            payload["coverage_complete"] = False
            payload.pop("submitted_scope", None)
        payload.setdefault("observation_version", self.session.SESSION_VERSION)
        payload["bundle_id"] = (
            f"bundle-{action.get('_receipt_id') or action['action_id']}"
        )
        payload["task_id"] = action["task_id"]
        try:
            evidence = self.evidence_adapter.adapt_observations(
                payload,
                trusted_at=action.get("_receipt_received_at"),
            )
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

    @staticmethod
    def _normalize_price_challenge_candidates(
        task: dict[str, Any], observation: dict[str, Any]
    ) -> None:
        raw_candidates = observation.get("gateway_candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise HarnessProtocolError(
                "price challenge capture requires gateway_candidates"
            )
        normalized = []
        for index, raw in enumerate(raw_candidates):
            if not isinstance(raw, dict) or not isinstance(raw.get("node"), dict):
                raise HarnessProtocolError(
                    f"price challenge candidate {index} requires node"
                )
            node = copy.deepcopy(raw["node"])
            node_id = str(node.get("node_id") or "").strip()
            destination_id = str(
                raw.get("destination_gateway_node_id") or ""
            ).strip()
            if not node_id or not destination_id:
                raise HarnessProtocolError(
                    f"price challenge candidate {index} requires Gateway endpoints"
                )
            return_origin_id = str(
                raw.get("return_origin_gateway_node_id") or destination_id
            ).strip()
            return_hub_node = raw.get("return_hub_node")
            if return_hub_node is not None and not isinstance(
                return_hub_node, dict
            ):
                raise HarnessProtocolError(
                    f"price challenge candidate {index}.return_hub_node must be an object"
                )
            return_hub_id = str(
                (return_hub_node or {}).get("node_id") or node_id
            ).strip()
            dates = copy.deepcopy(raw.get("itinerary_dates"))
            long_haul = copy.deepcopy(raw.get("long_haul_airfare"))
            positioning = copy.deepcopy(
                raw.get("positioning_airfare_lower_bound")
            )
            basis = {
                "task_id": task.get("task_id"),
                "outbound_hub_node_id": node_id,
                "return_hub_node_id": return_hub_id,
                "destination_gateway_node_id": destination_id,
                "return_origin_gateway_node_id": return_origin_id,
                "itinerary_dates": dates,
                "long_haul_airfare": long_haul,
                "positioning_airfare_lower_bound": positioning,
            }
            token = hashlib.sha256(
                json.dumps(
                    basis,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:20]
            signal = {
                "kind": "price_challenge_fare",
                "signal_id": f"price-challenge-signal-{token}",
                "dated_fare_amount": long_haul,
                "positioning_airfare_lower_bound": positioning,
                "date_coverage": "bounded_window",
                "opposite_gateway_node_id": destination_id,
                "outbound_origin_gateway_node_id": node_id,
                "outbound_destination_gateway_node_id": destination_id,
                "return_origin_gateway_node_id": return_origin_id,
                "return_destination_gateway_node_id": return_hub_id,
                "temporal_signal_version": "2.0",
                "itinerary_dates": dates,
            }
            if return_hub_node is not None:
                signal["return_hub_node"] = copy.deepcopy(return_hub_node)
            normalized.append(
                {
                    "candidate_id": f"price-challenge-candidate-{token}",
                    "side": "origin",
                    "node": node,
                    "signal": signal,
                }
            )
        observation["gateway_candidates"] = normalized

    def _prepare_product_payload(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        *,
        trusted_at: str | None = None,
    ) -> dict[str, Any]:
        """Attach Product-owned fields and derive Work Item completion intent."""

        result = copy.deepcopy(payload)
        observations = result.get("observations")
        if not isinstance(observations, list) or not observations:
            raise HarnessProtocolError("round-trip attempt requires observations")
        journey_scope = task.get("journey_scope")
        research_kind = task.get("research_kind")
        if research_kind == "region_scope_definition":
            seed_nodes = {
                str(node.get("node_id")): copy.deepcopy(node)
                for node in task.get("query_shape", {}).get(
                    "seed_gateway_hints", []
                )
                if isinstance(node, dict) and node.get("node_id")
            }
            side = str(task.get("query_shape", {}).get("side") or "gateway")
            for observation in observations:
                proposal = observation.get("proposal")
                candidates = (
                    proposal.get("candidates")
                    if isinstance(proposal, dict)
                    else None
                )
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    gateway_node_id = candidate.pop("gateway_node_id", None)
                    if gateway_node_id is not None:
                        node = seed_nodes.get(str(gateway_node_id))
                        if node is None:
                            raise HarnessProtocolError(
                                "region scope gateway_node_id is not a published seed"
                            )
                        candidate["node"] = node
                    node = candidate.get("node")
                    if not isinstance(node, dict) or not node.get("node_id"):
                        raise HarnessProtocolError(
                            "region scope candidate requires a known code or new node"
                        )
                    candidate.setdefault(
                        "candidate_id",
                        f"scope-{side}-{str(node['node_id'])}",
                    )
        if research_kind == "region_pool_fare_screen":
            query_shape = task.get("query_shape", {})
            provider_scope = {
                "coverage_semantics": "provider_ranked_window",
                "origin_gateway_node_ids": copy.deepcopy(
                    query_shape.get("origin_gateway_node_ids", [])
                ),
                "destination_gateway_node_ids": copy.deepcopy(
                    query_shape.get("destination_gateway_node_ids", [])
                ),
                "outbound_departure_window": copy.deepcopy(
                    query_shape.get("outbound_departure_window")
                ),
                "return_departure_window": copy.deepcopy(
                    query_shape.get("return_departure_window")
                ),
                "minimum_stay_nights": query_shape.get("minimum_stay_nights"),
                "maximum_stay_nights": query_shape.get("maximum_stay_nights"),
            }
            normalized_observations = []
            for raw in observations:
                if not isinstance(raw, dict):
                    raise HarnessProtocolError(
                        "round-trip observations must be objects"
                    )
                outcome = raw.get("outcome")
                if outcome is None:
                    # Persisted pre-compact Receipts remain replayable.
                    normalized_observations.append(copy.deepcopy(raw))
                    continue
                page_url = str(raw.get("page_url") or "").strip()
                if not page_url:
                    raise HarnessProtocolError(
                        "region pool Capture requires page_url"
                    )
                source_id = self.evidence_adapter.resolve_source_id(page_url)
                if source_id is None:
                    host = (urlsplit(page_url).hostname or "browser-source").lower()
                    source_id = host.removeprefix("www.") or "browser-source"
                source_registry = self.evidence_adapter._registry()
                source = source_registry.get(source_id, {})
                provider = str(source.get("name") or source_id)
                observed_at = str(
                    trusted_at
                    or raw.get("observed_at")
                    or datetime.now(timezone.utc).isoformat(timespec="seconds")
                )
                token_basis = {
                    "task_id": task.get("task_id"),
                    "page_url": page_url,
                    "outcome": outcome,
                    "fare": raw.get("fare"),
                }
                token = hashlib.sha256(
                    json.dumps(
                        token_basis,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:20]
                identity = {
                    "observation_id": f"pool-observation-{token}",
                    "source_id": source_id,
                    "provider": provider,
                    "url": page_url,
                    "observed_at": observed_at,
                }
                if outcome == "fare_found":
                    fare = raw.get("fare")
                    if not isinstance(fare, dict):
                        raise HarnessProtocolError(
                            "fare_found Capture requires fare"
                        )
                    origin_id = str(fare.get("origin_iata") or "").strip()
                    destination_id = str(
                        fare.get("destination_iata") or ""
                    ).strip()
                    normalized_observations.append(
                        {
                            **identity,
                            "price_selection_method": raw.get(
                                "price_selection_method"
                            ),
                            "provider_scope": copy.deepcopy(provider_scope),
                            "evidence_status": "research_observed",
                            "temporal_signals": [
                                {
                                    "signal_id": f"pool-signal-{token}",
                                    "signal_kind": "joint_date_pair",
                                    "origin_gateway_node_id": origin_id,
                                    "destination_gateway_node_id": destination_id,
                                    "outbound_origin_gateway_node_id": origin_id,
                                    "outbound_destination_gateway_node_id": destination_id,
                                    "return_origin_gateway_node_id": destination_id,
                                    "return_destination_gateway_node_id": origin_id,
                                    "temporal_signal_version": "2.0",
                                    "itinerary_dates": {
                                        "outbound_departure_local_date": fare.get(
                                            "outbound_departure_local_date"
                                        ),
                                        "outbound_destination_arrival_local_date": fare.get(
                                            "outbound_destination_arrival_local_date"
                                        ),
                                        "return_departure_local_date": fare.get(
                                            "return_departure_local_date"
                                        ),
                                    },
                                    "atomic_total": copy.deepcopy(
                                        fare.get("total")
                                    ),
                                }
                            ],
                        }
                    )
                elif outcome in {"no_result", "unsupported_view"}:
                    if outcome == "no_result" and raw.get(
                        "price_selection_method"
                    ) is None:
                        raise HarnessProtocolError(
                            "region pool no_result Capture requires "
                            "price_selection_method"
                        )
                    normalized_observations.append(
                        {
                            **identity,
                            **(
                                {
                                    "price_selection_method": raw.get(
                                        "price_selection_method"
                                    ),
                                    "provider_scope": copy.deepcopy(
                                        provider_scope
                                    ),
                                }
                                if outcome == "no_result"
                                else {}
                            ),
                            "evidence_status": (
                                "no_result"
                                if outcome == "no_result"
                                else "rejected"
                            ),
                            "reason": raw.get("reason"),
                        }
                    )
                else:
                    raise HarnessProtocolError(
                        "region pool Capture outcome is unsupported"
                    )
            observations = normalized_observations
            result["observations"] = observations
        statuses = {
            observation.get("evidence_status")
            for observation in observations
            if isinstance(observation, dict)
        }
        mixed_terminal_with_offer = (
            "verified_observed" in statuses
            and statuses <= {"verified_observed", "rejected", "no_result"}
        )
        if len(statuses) != 1 and not mixed_terminal_with_offer:
            raise HarnessProtocolError(
                "round-trip attempt dispositions are incompatible"
            )
        status = (
            "verified_observed"
            if mixed_terminal_with_offer
            else next(iter(statuses))
        )
        seller_listing_closed = True
        for observation in observations:
            if not isinstance(observation, dict):
                raise HarnessProtocolError("round-trip observations must be objects")
            if research_kind == "region_pool_fare_screen":
                # Coverage is derived above from the immutable Work Item and
                # the admitted minimal Capture, never from a Worker boolean.
                observation.pop("scope_witness", None)
            if (
                research_kind == "price_challenge_screen"
                and observation.get("evidence_status")
                == "research_observed"
            ):
                RoundTripProductAdapter._normalize_price_challenge_candidates(
                    task, observation
                )
            offers = observation.pop("offers", None)
            for signal in observation.get("temporal_signals", []):
                if not isinstance(signal, dict):
                    raise HarnessProtocolError(
                        "round-trip temporal signals must be objects"
                    )
                if signal.get("signal_kind") in {
                    "joint_date_pair",
                    "split_date_pair",
                }:
                    if signal.get("temporal_signal_version") != "2.0" or not isinstance(
                        signal.get("itinerary_dates"), dict
                    ):
                        raise HarnessProtocolError(
                            "joint round-trip signals require Temporal Signal v2"
                        )
                    if "outbound_date" in signal or "return_date" in signal:
                        raise HarnessProtocolError(
                            "Temporal Signal v2 forbids outbound_date and return_date"
                        )
            for candidate in observation.get("gateway_candidates", []):
                signal = candidate.get("signal") if isinstance(candidate, dict) else None
                if isinstance(signal, dict) and signal.get("kind") in {
                    "portfolio_frontier_fare",
                    "price_challenge_fare",
                }:
                    if signal.get("temporal_signal_version") != "2.0" or not isinstance(
                        signal.get("itinerary_dates"), dict
                    ):
                        raise HarnessProtocolError(
                            "portfolio frontier signals require Temporal Signal v2"
                        )
                    if "outbound_date" in signal or "return_date" in signal:
                        raise HarnessProtocolError(
                            "Temporal Signal v2 forbids outbound_date and return_date"
                        )
            if offers is not None:
                if not isinstance(offers, list) or not offers:
                    raise HarnessProtocolError(
                        "round-trip exact observation offers must be non-empty"
                    )
                listing = observation.get("seller_listing")
                if journey_scope != "surface":
                    if not isinstance(listing, dict):
                        raise HarnessProtocolError(
                            "flight observation requires seller_listing"
                        )
                    witness = listing.get("terminal_witness")
                    if not isinstance(witness, dict):
                        raise HarnessProtocolError(
                            "seller_listing requires terminal_witness"
                        )
                    witness["observed_offer_count"] = len(offers)
                    if witness.get("next_page_available") is not False:
                        raise HarnessProtocolError(
                            "seller listing remains open"
                        )
                    if witness.get("kind") == "provider_declared_count" and (
                        witness.get("provider_reported_offer_count") != len(offers)
                    ):
                        raise HarnessProtocolError(
                            "seller listing Provider count does not match offers"
                        )
                normalized_offers = []
                for raw_offer in offers:
                    if not isinstance(raw_offer, dict):
                        raise HarnessProtocolError(
                            "seller listing offers must be objects"
                        )
                    offer = copy.deepcopy(raw_offer)
                    booking_url = offer.pop("booking_url", None)
                    if booking_url is not None:
                        offer["url"] = booking_url
                    offer.update(
                        {
                            "source_id": observation.get("source_id"),
                            "provider": observation.get("provider"),
                            "observed_at": observation.get("observed_at"),
                            "pricing_scope": "atomic_offer",
                            "journey_scope": journey_scope,
                            "atomic": True,
                            "evidence_status": "verified_observed",
                        }
                    )
                    if journey_scope == "one_way":
                        offer["direction"] = task.get("direction")
                    normalized_offers.append(offer)
                observation["offers"] = normalized_offers
            elif status == "verified_observed" and journey_scope != "surface":
                # Legacy persisted Receipts remain readable, but new Work Attempt
                # Schemas never publish the singular form.
                seller_listing_closed = False

        if research_kind == "region_pool_fare_screen":
            result["coverage_complete"] = False
        elif research_kind == "internal_surface_transfer":
            result["coverage_complete"] = True
        elif status == "research_observed" and research_kind == "priced_offer":
            result["coverage_complete"] = False
        elif status in {"no_result", "rejected"} and research_kind == "priced_offer":
            result["coverage_complete"] = False
        elif status == "verified_observed" and journey_scope != "surface":
            result["coverage_complete"] = seller_listing_closed
        else:
            result["coverage_complete"] = True
        return result

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
        task = self._task_by_id(state, str(action["task_id"]))
        candidate_payload = self._prepare_product_payload(
            task,
            payload,
            trusted_at=action.get("_receipt_received_at"),
        )
        if task.get("research_kind") == "region_pool_fare_screen":
            candidate_payload["coverage_complete"] = False
            candidate_payload.pop("submitted_scope", None)
        candidate_payload["bundle_id"] = bundle_id
        candidate_payload["task_id"] = action["task_id"]
        try:
            candidate = self.evidence_adapter.adapt_observations(
                candidate_payload,
                trusted_at=action.get("_receipt_received_at"),
            )
        except self.evidence_adapter.EvidenceAdapterError as exc:
            raise HarnessProtocolError(
                f"round-trip replay Evidence is invalid: {exc}"
            ) from exc
        if "admitted_at" not in existing:
            candidate.pop("admitted_at", None)
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
            elif kind == "stop_bounded_research":
                state = self.session.stop_bounded_research(
                    state,
                    reason,
                    str(action.get("stop_disposition") or "threshold_stop"),
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

    def reopen_expired_result(self, product_directory: Path) -> bool:
        state = self.load_state(product_directory)
        if state.get("finalization_status") != "final":
            return state.get("result", {}).get("best_portfolio") is not None
        try:
            state = self.session.reopen_for_price_refresh(state)
        except self.session.RoundTripSessionError as exc:
            raise HarnessProtocolError(
                f"round-trip price refresh reopen failed: {exc}"
            ) from exc
        self._save_state(product_directory, state)
        return True

    def read_final_result(self, product_directory: Path) -> dict[str, Any] | None:
        target = self._final_path(product_directory)
        if not target.exists():
            return None
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
