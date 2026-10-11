"""Product Adapter for the existing fixed one-way planner."""

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
    one_way_baggage,
)
from ..suite_layout import SuiteLayout


TERMINAL_HYPOTHESIS_STATUSES = {"complete", "hard_dead", "deferred_low_value"}


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


class OneWayProductAdapter:
    product_id = "one_way"
    journey_shape = "one_way"

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
        self.live = _load_module("flight_search_harness_one_way_live", scripts / "live_provider_session.py")
        self.materializer = _load_module(
            "flight_search_harness_one_way_materializer", scripts / "scenario_materializer.py"
        )
        self.briefs = _load_module(
            "flight_search_harness_one_way_briefs", scripts / "browser_task_brief.py"
        )
        self.evidence_adapter = _load_module(
            "flight_search_harness_one_way_evidence", scripts / "ai_evidence_adapter.py"
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
                f"cannot identify one-way Session state: {exc}"
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
        for event in reversed(state.get("finalization_history", [])):
            reason = event.get("reason") if isinstance(event, dict) else None
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
        target = self._finalization_intent_path(product_directory)
        if target.exists():
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot read one-way finalization intent: {exc}"
                ) from exc
            reason = value.get("reason") if isinstance(value, dict) else None
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        return "Recovered finalized one-way Product Session artifact."

    def _materialize_final_result(
        self,
        product_directory: Path,
        state: dict[str, Any],
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        reason = self._finalization_reason(
            product_directory, state, fallback_reason
        )
        evaluated_at = self.live.finalization_evaluated_at(state)
        try:
            result = self.live._export_result(
                state,
                search_status="final",
                finalization_reason=reason,
                evaluated_at=evaluated_at,
            )
        except self.live.LiveProviderError as exc:
            raise HarnessProtocolError(
                f"one-way final result recovery failed: {exc}"
            ) from exc
        existing = self.read_final_result(product_directory)
        if existing != result:
            _write_json_atomic(self._final_path(product_directory), result)
        return result

    def initialize(self, request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        if request.get("journey_shape") != self.journey_shape:
            raise HarnessProtocolError("OneWayProductAdapter requires journey_shape=one_way")
        intent = request.get("intent")
        if not isinstance(intent, dict):
            raise HarnessProtocolError("one-way request.intent must be an object")
        shared = canonical_shared_intent(intent)
        shared["fx_to_base"] = system_fx_context(shared["base_currency"])["rates"]
        if shared["baggage_policy"] != {
            "cabin_baggage": "none_required",
            "checked_bag_count": 0,
            "paid_baggage_allowed": True,
        }:
            raise HarnessProtocolError(
                "one-way currently supports only no minimum baggage requirement"
            )
        origin = canonical_region_side(intent, "origin")
        destination = canonical_region_side(intent, "destination")
        for field, side in (("origin", origin), ("destination", destination)):
            side["max_ground_cost"] = money_in_base_currency(
                side.get("max_ground_cost"), shared, f"intent.{field}.max_ground_cost"
            )
        product_intent = copy.deepcopy(intent)
        if "budget" in product_intent:
            product_intent["budget"] = money_in_base_currency(
                product_intent.get("budget"), shared, "intent.budget"
            )
        product_intent.update(
            {
                "intent_id": derived_intent_id(self.journey_shape, intent),
                "trip_scope": "one_way",
                "trip_mode": "fixed_destination",
                "currency": shared["base_currency"],
                "currency_decimal_places": 2,
                "traveler_count": shared["adult_count"],
                "baggage": one_way_baggage(shared["baggage_policy"]),
                "overnight_allowed": shared["overnight_allowed"],
                "self_transfer_policy": shared["self_transfer_policy"],
                "fx_to_base": shared["fx_to_base"],
                "origin": self._one_way_region_side(copy.deepcopy(origin)),
                "destination": self._one_way_region_side(copy.deepcopy(destination)),
                "date_window": canonical_window(intent, "departure_window"),
                "date_flexibility": (
                    {
                        "mode": "exact_dates",
                        "exact_departure_dates": copy.deepcopy(
                            intent["exact_departure_dates"]
                        ),
                    }
                    if "exact_departure_dates" in intent
                    else {"mode": "bounded_window"}
                ),
            }
        )
        for public_field in (
            "adult_count",
            "base_currency",
            "baggage_policy",
            "departure_window",
            "exact_departure_dates",
        ):
            product_intent.pop(public_field, None)
        artifacts = self.materializer.materialize_intent(product_intent)
        for side_name, side, endpoint_field in (
            ("origin", origin, "origin_start_nodes"),
            ("destination", destination, "destination_end_nodes"),
        ):
            if side["endpoint_policy"] != "anchor_required":
                continue
            anchor_ids = [
                str(node.get("node_id") or "").upper()
                for node in side["anchor_nodes"]
            ]
            if not all(anchor_ids):
                raise HarnessProtocolError(
                    f"one-way intent.{side_name}.anchor_nodes require node_id"
                )
            artifacts["profile"][endpoint_field] = anchor_ids
        maximum_total_duration_minutes = intent.get("maximum_total_duration_minutes")
        if maximum_total_duration_minutes is not None:
            if (
                isinstance(maximum_total_duration_minutes, bool)
                or not isinstance(maximum_total_duration_minutes, int)
                or maximum_total_duration_minutes < 1
            ):
                raise HarnessProtocolError(
                    "one-way intent.maximum_total_duration_minutes must be a "
                    "positive integer"
                )
            artifacts["config"]["search"]["max_total_travel_minutes"] = (
                maximum_total_duration_minutes
            )
        scenario_directory = product_directory / "scenario"
        scenario_directory.mkdir(parents=True, exist_ok=True)
        for name in ("profile", "nodes", "edges", "config"):
            _write_json_atomic(scenario_directory / f"{name}.json", artifacts[name])
        state = self.live.initialize_session(scenario_directory / "config.json")
        state = self.live.add_research_requirements(
            state,
            self._regional_connector_requests(
                origin,
                destination,
                state["profile"],
            ),
        )
        _write_json_atomic(self._session_path(product_directory), state)
        return state

    @staticmethod
    def _regional_connector_requests(
        origin: dict[str, Any],
        destination: dict[str, Any],
        profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Publish priced access/egress questions for anchor-required regions."""

        requests: list[dict[str, Any]] = []
        window = profile["date_window"]
        defaults = ["rail", "bus", "coach", "local_transit", "walk", "rideshare"]
        for side_name, side in (("origin", origin), ("destination", destination)):
            if side.get("endpoint_policy") != "anchor_required":
                continue
            anchors = list(side.get("anchor_nodes", []))
            gateways = list(side.get("seed_gateway_nodes", []))
            modes = list(side.get("allowed_ground_modes") or defaults)
            for anchor in anchors:
                for gateway in gateways:
                    anchor_id = str(anchor.get("node_id") or "").upper()
                    gateway_id = str(gateway.get("node_id") or "").upper()
                    if not anchor_id or not gateway_id or anchor_id == gateway_id:
                        continue
                    from_node, to_node = (
                        (anchor_id, gateway_id)
                        if side_name == "origin"
                        else (gateway_id, anchor_id)
                    )
                    requests.append(
                        {
                            "request_id": (
                                f"regional-{side_name}-{from_node.lower()}-"
                                f"{to_node.lower()}"
                            ),
                            "request_type": "unresolved_connector",
                            "origins": [from_node],
                            "destinations": [to_node],
                            "from_node": from_node,
                            "to_node": to_node,
                            "earliest_departure": window["start"],
                            "latest_arrival": window["end"],
                            "suggested_modes": modes,
                            "provider_kinds": [
                                f"{mode}_schedule_or_offer" for mode in modes
                            ],
                            "endpoint_policy": {
                                "from": "exact_node",
                                "to": "exact_node",
                            },
                            "traveler_count": profile["traveler_count"],
                            "baggage": copy.deepcopy(profile["baggage"]),
                            "overnight_allowed": profile["overnight_allowed"],
                            "self_transfer_policy": profile["self_transfer_policy"],
                            "date_search": {"mode": "bounded_window"},
                            "hypothesis_id": (
                                f"regional-{side_name}:{from_node}:{to_node}"
                            ),
                            "hypothesis_kind": "regional_connector",
                            "route_shape": f"{side_name}_surface_connector",
                        }
                    )
        return requests

    @staticmethod
    def _one_way_region_side(side: dict[str, Any]) -> dict[str, Any]:
        """Translate shared regional endpoints without weakening endpoint policy."""

        anchors = side.pop("anchor_nodes")
        gateways = side.pop("seed_gateway_nodes")
        side.pop("endpoint_policy")
        seed_nodes = []
        seen_node_ids: set[str] = set()
        for node in [*anchors, *gateways]:
            node_id = str(node.get("node_id") or "")
            if node_id not in seen_node_ids:
                seen_node_ids.add(node_id)
                seed_nodes.append(node)
        side["seed_nodes"] = seed_nodes
        return side

    def load_state(self, product_directory: Path) -> dict[str, Any]:
        try:
            raw = json.loads(self._session_path(product_directory).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessProtocolError(f"cannot load one-way Session: {exc}") from exc
        return self.live.validate_session(raw)

    def _save_state(self, product_directory: Path, state: dict[str, Any]) -> None:
        _write_json_atomic(self._session_path(product_directory), state)

    def inspect(self, state: dict[str, Any]) -> dict[str, Any]:
        state = self.live.validate_session(state)
        audit = self.live._finalization_audit(state)
        no_result_stop = state.get("no_result_stop_assessment")
        if (
            isinstance(no_result_stop, dict)
            and no_result_stop.get("search_round") == int(state.get("search_round", 0))
            and not state.get("result", {}).get("routes")
        ):
            resume_actions = []
            for task in state.get("research_tasks", []):
                if task.get("request_type") != "conventional_reference":
                    continue
                if task.get("status") not in {"rejected", "no_result"}:
                    continue
                attempted = provider_foundation.attempted_source_ids(
                    state, str(task.get("task_id"))
                )
                untried = [
                    str(item.get("source_id"))
                    for item in task.get("source_suggestions", [])
                    if isinstance(item, dict)
                    and item.get("source_id")
                    and str(item.get("source_id")) not in attempted
                ]
                if attempted and untried:
                    resume_actions.append(
                        {
                            "kind": "resume_no_result_source_challenger",
                            "action_key": f"resume_no_result_source_challenger:{task['task_id']}",
                            "hypothesis_id": str(task.get("hypothesis_id")),
                            "task_id": str(task["task_id"]),
                            "request_type": str(task.get("request_type")),
                            "priority": 0,
                            "attempted_source_ids": sorted(attempted),
                            "untried_source_ids": untried,
                        }
                    )
            actions = [
                *resume_actions,
                {
                    "kind": "draft",
                    "action_key": "keep_no_result_draft",
                    "reason": f"Search stopped without a complete route: {no_result_stop['reason']}",
                    "priority": 1000,
                },
            ]
            return {
                "control_version": "1.0",
                "product_id": self.product_id,
                "session_id": state["session_id"],
                "active_hypothesis_id": None,
                "next_candidate_id": None,
                "has_complete_result": False,
                "can_finalize": False,
                "finalization_issues": copy.deepcopy(audit["issues"]),
                "investigation_actions": [],
                "disposition_actions": [],
                "recovery_actions": [],
                "source_challenger_actions": [],
                "actions": actions,
                "draft_reason": f"Search stopped without a complete route: {no_result_stop['reason']}",
            }
        catalog = self.briefs.build_catalog(state)
        view = self.briefs.catalog_view(catalog, show_all=True)
        outcomes = state.get("hypothesis_outcomes", {})
        baseline_outcome = outcomes.get("conventional-baseline", {})
        complete_trip_lane_open = bool(
            not state.get("result", {}).get("routes")
            and baseline_outcome.get("status") not in TERMINAL_HYPOTHESIS_STATUSES
            and any(
                task.get("request_type") == "conventional_reference"
                and task.get("status") in {"candidate", "pending"}
                for task in state.get("research_tasks", [])
                if isinstance(task, dict)
            )
        )
        investigation_actions: list[dict[str, Any]] = []
        for question in view.get("questions", []):
            if complete_trip_lane_open and question.get("request_type") != "conventional_reference":
                continue
            for option in question.get("options", []):
                investigation_actions.append(
                    {
                        "kind": "investigate",
                        "task_id": option.get("task_id"),
                        "status": option.get("status"),
                        "hypothesis_id": question.get("hypothesis_id"),
                        "request_type": question.get("request_type"),
                        "sequence": next(
                            (
                                int(task.get("sequence", 0))
                                for task in state["research_tasks"]
                                if task.get("task_id") == option.get("task_id")
                            ),
                            0,
                        ),
                    }
                )
        active = state.get("active_hypothesis")
        active_id = active.get("hypothesis_id") if isinstance(active, dict) else None
        pending_hypotheses = {
            str(task.get("hypothesis_id"))
            for task in state.get("research_tasks", [])
            if task.get("status") == "pending" and task.get("hypothesis_id")
        }
        if active_id:
            pending_hypotheses = {str(active_id)}
        elif complete_trip_lane_open:
            pending_hypotheses = {"conventional-baseline"}
        else:
            pending_hypotheses.update(
                str(hypothesis_id)
                for hypothesis_id, node in state.get("hypothesis_tree", {}).get("nodes", {}).items()
                if hypothesis_id != self.live.HYPOTHESIS_ROOT_ID
                and isinstance(node, dict)
                and node.get("status") == "selected_open"
            )
        disposition_actions = []
        recovery_actions = []
        source_challenger_actions = []
        frontier_actions = []
        tree_nodes = state.get("hypothesis_tree", {}).get("nodes", {})
        tasks_by_hypothesis: dict[str, list[dict[str, Any]]] = {}
        for task in state.get("research_tasks", []):
            if isinstance(task, dict) and task.get("hypothesis_id"):
                tasks_by_hypothesis.setdefault(str(task["hypothesis_id"]), []).append(task)
        for hypothesis_id, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue
            if outcome.get("status") != "deferred_low_value":
                continue
            reason = str(outcome.get("reason") or "")
            if reason != "External browser policy selected the next published product action.":
                continue
            candidates = [
                task
                for task in tasks_by_hypothesis.get(str(hypothesis_id), [])
                if task.get("status") in {"candidate", "pending"}
                and int(task.get("attempt_count", 0)) == 0
            ]
            candidates.sort(
                key=lambda task: (int(task.get("sequence", 0)), str(task.get("task_id")))
            )
            if candidates:
                recovery_actions.append(
                    {
                        "kind": "reopen",
                        "hypothesis_id": str(hypothesis_id),
                        "task_id": str(candidates[0]["task_id"]),
                        "action_key": f"reopen:{hypothesis_id}:{candidates[0]['task_id']}",
                        "recovery_reason": "legacy_external_disposition",
                    }
                )
        for hypothesis_id, outcome in outcomes.items():
            if not isinstance(outcome, dict) or outcome.get("status") not in TERMINAL_HYPOTHESIS_STATUSES:
                continue
            for task in tasks_by_hypothesis.get(str(hypothesis_id), []):
                if task.get("status") not in {"rejected", "no_result"}:
                    continue
                attempted = provider_foundation.attempted_source_ids(state, str(task.get("task_id")))
                suggested = [
                    str(item.get("source_id"))
                    for item in task.get("source_suggestions", [])
                    if isinstance(item, dict) and item.get("source_id")
                ]
                untried = [source_id for source_id in suggested if source_id not in attempted]
                if attempted and untried:
                    priorities = {
                        "conventional_reference": 0,
                        "direct_closure": 1,
                        "origin_market_scan": 2,
                        "destination_market_scan": 2,
                        "budgeted_predecessor_scan": 3,
                        "unresolved_connector": 3,
                    }
                    source_challenger_actions.append(
                        {
                            "kind": "reopen_source_challenger",
                            "hypothesis_id": str(hypothesis_id),
                            "task_id": str(task["task_id"]),
                            "attempted_source_ids": sorted(attempted),
                            "untried_source_ids": untried,
                            "request_type": str(task.get("request_type")),
                            "priority": priorities.get(str(task.get("request_type")), 10),
                            "action_key": f"reopen_source_challenger:{task['task_id']}",
                        }
                    )
        for hypothesis_id in sorted(pending_hypotheses):
            outcome = outcomes.get(hypothesis_id)
            if isinstance(outcome, dict) and outcome.get("status") in TERMINAL_HYPOTHESIS_STATUSES:
                continue
            node = tree_nodes.get(hypothesis_id, {})
            disposition_actions.append(
                {
                    "kind": "dispose",
                    "hypothesis_id": hypothesis_id,
                    "sibling_order": int(node.get("sibling_order", 10**9)),
                }
            )
        actions: list[dict[str, Any]] = []
        for item in investigation_actions:
            item["action_key"] = f"investigate:{item['task_id']}"
            actions.append(item)
        actions.extend(recovery_actions)
        source_challenger_actions.sort(
            key=lambda item: (
                int(item.get("priority", 10)),
                str(item.get("task_id")),
            )
        )
        actions.extend(source_challenger_actions)
        if active_id is None:
            for item in state.get("frontier_research_view", {}).get(
                "frontier_actions", []
            ):
                if not isinstance(item, dict) or not item.get("frontier_id"):
                    continue
                action = {
                    **copy.deepcopy(item),
                    "kind": str(item.get("kind") or "explore_frontier"),
                    "action_key": str(
                        item.get("action_key")
                        or f"explore_frontier:{item['frontier_id']}"
                    ),
                }
                frontier_actions.append(action)
                actions.append(action)
        for item in disposition_actions:
            if (
                item.get("hypothesis_id") == active_id
                and isinstance(active, dict)
                and active.get("status") == "complete_candidate_found"
            ):
                completed = {**copy.deepcopy(item), "disposition": "complete"}
                completed["action_key"] = (
                    f"dispose:{completed['hypothesis_id']}:complete"
                )
                actions.append(completed)
                continue
            for disposition in ("hard_dead", "deferred_low_value"):
                option = {**copy.deepcopy(item), "disposition": disposition}
                option["action_key"] = (
                    f"dispose:{option['hypothesis_id']}:{disposition}"
                )
                actions.append(option)
        if audit["can_finalize"]:
            actions.append({"kind": "finalize", "action_key": "finalize"})
        issue_codes = {
            issue.get("code")
            for issue in audit.get("issues", [])
            if isinstance(issue, dict)
        }
        if (
            state.get("result", {}).get("routes")
            and active_id is None
            and issue_codes == {"fresh_challenger_stop_assessment_missing"}
        ):
            actions.append(
                {
                    "kind": "assess_stop",
                    "action_key": "assess_stop",
                    "challenger_hypothesis_ids": [],
                }
            )
        selected_ids = {
            str(item.get("hypothesis_id"))
            for item in state.get("hypothesis_history", [])
            if item.get("action") == "select" and item.get("hypothesis_id")
        }
        selected_terminal = bool(selected_ids) and all(
            state.get("hypothesis_tree", {}).get("nodes", {}).get(hypothesis_id, {}).get("status")
            in TERMINAL_HYPOTHESIS_STATUSES
            for hypothesis_id in selected_ids
        )
        if (
            not state.get("result", {}).get("routes")
            and active_id is None
            and selected_terminal
        ):
            actions.append(
                {
                    "kind": "stop_without_result",
                    "action_key": "stop_without_result",
                }
            )
        return {
            "control_version": "1.0",
            "product_id": self.product_id,
            "session_id": state["session_id"],
            "active_hypothesis_id": active_id,
            "next_candidate_id": state.get("hypothesis_tree", {}).get("next_candidate_id"),
            "has_complete_result": bool(state.get("result", {}).get("routes")),
            "can_finalize": bool(audit["can_finalize"]),
            "finalization_issues": copy.deepcopy(audit["issues"]),
            "investigation_actions": investigation_actions,
            "disposition_actions": disposition_actions,
            "recovery_actions": recovery_actions,
            "source_challenger_actions": source_challenger_actions,
            "frontier_actions": frontier_actions,
            "frontier_research_view": copy.deepcopy(
                state.get("frontier_research_view", {})
            ),
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
        ordered = sorted(
            investigations,
            key=lambda action: (
                0 if action.get("status") == "pending" else 1,
                int(action.get("sequence", 10**9)),
                str(action.get("task_id") or ""),
            ),
        )
        return _plan_adapter_transition(
            control,
            investigations,
            ordered[:1],
            choose_when_mixed=True,
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
                raise HarnessProtocolError("one-way investigation action has no task_id")
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
            task = next(
                (
                    entry
                    for entry in state.get("research_tasks", [])
                    if isinstance(entry, dict) and str(entry.get("task_id")) == task_id
                ),
                None,
            )
            if task is None:
                raise HarnessProtocolError(f"one-way task does not exist: {task_id}")
            attempt_number = int(task.get("attempt_count", 0)) + 1
            research_kind = str(task.get("research_kind") or "")
            item_kind = (
                "semantic_judgment"
                if research_kind
                in {
                    "region_scope_definition",
                    "region_scope_challenge",
                    "structural_challenger_definition",
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

    def prepare_investigation(
        self,
        product_directory: Path,
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        state = self.load_state(product_directory)
        task = next(
            (item for item in state["research_tasks"] if item.get("task_id") == task_id),
            None,
        )
        if task is None or task.get("status") not in {"candidate", "pending"}:
            raise HarnessProtocolError(f"one-way task is not actionable: {task_id}")
        brief = self.briefs.task_to_brief(task, state, 1)
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
            "hypothesis_id": task.get("hypothesis_id"),
            "brief": brief,
            "attempt_schema": {
                "type": "object",
                "required": ["kind", "source", "observations"],
                "properties": {
                    "kind": {"const": "observations"},
                    "bundle_id": {"type": "string", "minLength": 1},
                    "source": {"type": "object"},
                    "discovered_nodes": {"type": "array"},
                    "observations": {"type": "array", "minItems": 1},
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
        if attempt.get("kind") != "observations":
            raise HarnessProtocolError("one-way ingest requires an observations attempt")
        state = self.load_state(product_directory)
        task_id = str(action["task_id"])
        task = next(
            (item for item in state["research_tasks"] if item.get("task_id") == task_id),
            None,
        )
        if task is None:
            raise HarnessProtocolError(f"unknown one-way task: {task_id}")
        active = state.get("active_hypothesis")
        if not (
            isinstance(active, dict)
            and active.get("hypothesis_id") == task.get("hypothesis_id")
            and active.get("selected_task_id") == task_id
        ):
            try:
                state = self.live.record_hypothesis_decision(
                    state,
                    "select",
                    "Admit the persisted Work Attempt for this research question.",
                    task_id=task_id,
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(
                    f"one-way Work Attempt selection failed: {exc}"
                ) from exc
            task = next(
                item for item in state["research_tasks"] if item["task_id"] == task_id
            )
        current_brief = provider_foundation.apply_foundation(
            self.briefs.task_to_brief(task, state, 1),
            attempted_sources=provider_foundation.attempted_source_ids(
                state, task_id
            ),
        )
        provider_foundation.validate_first_attempt(
            attempt,
            required_source=current_brief["provider_foundation"][
                "required_source"
            ],
        )
        provider_foundation.validate_price_ordering(attempt)
        task_contract = copy.deepcopy(task)
        task_contract["status"] = "pending"
        raw_payload = {
            "adapter_version": "0.7.1",
            "bundle_id": f"bundle-{action.get('_receipt_id') or action['action_id']}",
            "task": task_contract,
            "source": attempt.get("source"),
            "discovered_nodes": copy.deepcopy(attempt.get("discovered_nodes", [])),
            "observations": copy.deepcopy(attempt.get("observations")),
        }
        try:
            evidence = self.evidence_adapter.adapt_payload(raw_payload)
            state = self.live.ingest_evidence(state, evidence)
        except (self.evidence_adapter.EvidenceAdapterError, self.live.LiveProviderError) as exc:
            raise HarnessProtocolError(f"one-way evidence transaction failed: {exc}") from exc
        self._save_state(product_directory, state)
        return state

    def attempt_committed(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> bool:
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
        task_id = str(action["task_id"])
        task = next(
            item for item in state["research_tasks"] if item.get("task_id") == task_id
        )
        task_contract = copy.deepcopy(task)
        task_contract["status"] = "pending"
        try:
            candidate = self.evidence_adapter.adapt_payload(
                {
                    "adapter_version": "0.7.1",
                    "bundle_id": bundle_id,
                    "task": task_contract,
                    "source": attempt.get("source"),
                    "discovered_nodes": copy.deepcopy(
                        attempt.get("discovered_nodes", [])
                    ),
                    "observations": copy.deepcopy(attempt.get("observations")),
                }
            )
        except self.evidence_adapter.EvidenceAdapterError as exc:
            raise HarnessProtocolError(
                f"one-way replay Evidence is invalid: {exc}"
            ) from exc
        comparable_existing = copy.deepcopy(existing)
        comparable_candidate = copy.deepcopy(candidate)
        comparable_existing.pop("assigned_source_fulfillment", None)
        comparable_candidate.pop("assigned_source_fulfillment", None)
        if comparable_existing != comparable_candidate:
            raise HarnessProtocolError(
                "one-way Receipt identity was replayed with conflicting Evidence"
            )
        return True

    def apply_control_action(
        self,
        product_directory: Path,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        if action.get("kind") in {"explore_frontier", "reopen_frontier"}:
            state = self.load_state(product_directory)
            frontier_id = str(action.get("frontier_id") or "")
            if not frontier_id:
                raise HarnessProtocolError("one-way frontier exploration requires frontier_id")
            try:
                if action.get("kind") == "reopen_frontier":
                    task_id = str(action.get("task_id") or "")
                    if not task_id:
                        raise HarnessProtocolError(
                            "one-way frontier reopen requires task_id"
                        )
                    state = self.live.record_hypothesis_decision(
                        state,
                        "select",
                        str(
                            action.get("reason")
                            or "Reopen this evidence-backed frontier."
                        ),
                        task_id=task_id,
                    )
                else:
                    state = self.live.publish_frontier_research_task(
                        state,
                        frontier_id,
                        str(
                            action.get("reason")
                            or "Explore this evidence-backed frontier."
                        ),
                    )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(f"one-way frontier exploration failed: {exc}") from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") == "resume_no_result_source_challenger":
            state = self.load_state(product_directory)
            task_id = str(action.get("task_id") or "")
            if not task_id:
                raise HarnessProtocolError("one-way draft resume requires task_id")
            try:
                state = self.live.reopen_task_for_source_challenger(
                    state,
                    task_id,
                    str(action.get("reason") or "Resume the complete-trip Provider fallback."),
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(f"one-way draft resume failed: {exc}") from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") == "stop_without_result":
            state = self.load_state(product_directory)
            try:
                state = self.live.record_no_result_stop(
                    state,
                    str(action.get("reason") or "No complete route was verified."),
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(f"one-way no-result stop failed: {exc}") from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") == "reopen_source_challenger":
            state = self.load_state(product_directory)
            task_id = str(action.get("task_id") or "")
            if not task_id:
                raise HarnessProtocolError("one-way source challenger requires task_id")
            try:
                state = self.live.reopen_task_for_source_challenger(
                    state,
                    task_id,
                    str(action.get("reason") or "Try an unattempted capability-matched source."),
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(f"one-way source challenger reopen failed: {exc}") from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") == "reopen":
            state = self.load_state(product_directory)
            task_id = str(action.get("task_id") or "")
            if not task_id:
                raise HarnessProtocolError("one-way reopen requires task_id")
            try:
                state = self.live.record_hypothesis_decision(
                    state,
                    "select",
                    str(action.get("reason") or "Resume unfinished hypothesis work."),
                    task_id=task_id,
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(f"one-way hypothesis reopen failed: {exc}") from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") == "assess_stop":
            state = self.load_state(product_directory)
            assessment = state.get("stop_assessment")
            if isinstance(assessment, dict) and assessment.get("search_round") == int(
                state.get("search_round", 0)
            ):
                return state
            try:
                state = self.live.record_stop_assessment(
                    state,
                    str(action.get("reason") or "not recorded"),
                    list(action.get("challenger_hypothesis_ids", [])),
                )
            except self.live.LiveProviderError as exc:
                raise HarnessProtocolError(
                    f"one-way stop assessment failed: {exc}"
                ) from exc
            self._save_state(product_directory, state)
            return state
        if action.get("kind") != "dispose":
            raise HarnessProtocolError(
                f"unsupported one-way control action: {action.get('kind')}"
            )
        hypothesis_id = str(action.get("hypothesis_id") or "")
        disposition = str(action.get("disposition") or "")
        reason = str(action.get("reason") or "")
        if disposition not in TERMINAL_HYPOTHESIS_STATUSES:
            raise HarnessProtocolError(f"unsupported one-way disposition: {disposition}")
        state = self.load_state(product_directory)
        existing = state.get("hypothesis_outcomes", {}).get(hypothesis_id)
        if isinstance(existing, dict) and existing.get("status") == disposition:
            return state
        try:
            state = self.live.record_hypothesis_decision(
                state,
                disposition,
                reason,
                hypothesis_id=hypothesis_id,
            )
        except self.live.LiveProviderError as exc:
            raise HarnessProtocolError(f"one-way hypothesis disposition failed: {exc}") from exc
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
            state, result = self.live.finalize_result(state, reason)
        except self.live.LiveProviderError as exc:
            raise HarnessProtocolError(f"one-way finalization failed: {exc}") from exc
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
