"""Product Adapter for the airport-only random-loop planner."""

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
    HarnessBlockedError,
    HarnessProtocolError,
    _build_adapter_choice_item,
    _build_adapter_research_item,
    _plan_adapter_transition,
)
from ..observation_bundle import normalize_attempt
from ..fx_rates import system_fx_context
from ..request_values import (
    canonical_shared_intent,
    canonical_window,
)
from ..suite_layout import SuiteLayout


ORDERED_SOURCE_BATCH_SIZE = 3


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


class RandomLoopProductAdapter:
    product_id = "random_loop"
    journey_shape = "random_loop"

    def __init__(
        self,
        *,
        product_root: str | Path | None = None,
        airport_catalog_ref: str | Path | None = None,
        airport_transit_catalog_ref: str | Path | None = None,
    ):
        self.product_root = (
            Path(product_root).resolve()
            if product_root is not None
            else SuiteLayout.discover(anchor=__file__).runtime_root(self.product_id)
        )
        self.engine = _load_module(
            "flight_search_harness_random_loop",
            self.product_root / "scripts" / "route_engine.py",
        )
        configured_airport_catalog = airport_catalog_ref or os.environ.get(
            "FLIGHT_SEARCH_HARNESS_AIRPORT_CATALOG"
        )
        configured_transit_catalog = airport_transit_catalog_ref or os.environ.get(
            "FLIGHT_SEARCH_HARNESS_AIRPORT_TRANSIT_CATALOG"
        )
        self.airport_catalog_ref = (
            Path(configured_airport_catalog).resolve()
            if configured_airport_catalog
            else None
        )
        self.airport_transit_catalog_ref = (
            Path(configured_transit_catalog).resolve()
            if configured_transit_catalog
            else None
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
                f"cannot identify random-loop Session state: {exc}"
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
        target = self._finalization_intent_path(product_directory)
        if target.exists():
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot read random-loop finalization intent: {exc}"
                ) from exc
            reason = value.get("reason") if isinstance(value, dict) else None
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
        for event in reversed(state.get("finalization_history", [])):
            reason = event.get("reason") if isinstance(event, dict) else None
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        return "Recovered finalized random-loop Product Session artifact."

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
            result = self.engine.export_result(
                state, search_status="final", reason=reason
            )
        except self.engine.RouteEngineError as exc:
            raise HarnessProtocolError(
                f"random-loop final result recovery failed: {exc}"
            ) from exc
        existing = self.read_final_result(product_directory)
        if existing != result:
            _write_json_atomic(self._final_path(product_directory), result)
        return result

    def initialize(self, request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        if request.get("journey_shape") != self.journey_shape:
            raise HarnessProtocolError(
                "RandomLoopProductAdapter requires journey_shape=random_loop"
            )
        intent = request.get("intent")
        if not isinstance(intent, dict):
            raise HarnessProtocolError("random-loop request.intent must be an object")
        shared = canonical_shared_intent(intent)
        shared["fx_to_base"] = system_fx_context(shared["base_currency"])["rates"]
        if not shared["overnight_allowed"]:
            raise HarnessProtocolError(
                "random-loop currently supports only overnight_allowed=true; "
                "its Product Kernel cannot enforce an overnight prohibition"
            )
        if shared["self_transfer_policy"] != "allow":
            raise HarnessProtocolError(
                "random-loop currently supports only self_transfer_policy=allow; "
                "its airport-only independent-leg search cannot enforce never"
            )
        if shared["baggage_policy"] != {
            "cabin_baggage": "none_required",
            "checked_bag_count": 0,
            "paid_baggage_allowed": True,
        }:
            raise HarnessProtocolError(
                "random-loop currently supports only no minimum baggage requirement"
            )
        config = copy.deepcopy(intent)
        first_departure_window = canonical_window(intent, "first_departure_window")
        maximum_total_duration_minutes = intent.get("maximum_total_duration_minutes")
        if (
            isinstance(maximum_total_duration_minutes, bool)
            or not isinstance(maximum_total_duration_minutes, int)
            or maximum_total_duration_minutes < 1
        ):
            raise HarnessProtocolError(
                "random-loop intent.maximum_total_duration_minutes must be a "
                "positive integer"
            )
        config.update(
            {
                "trip_start": first_departure_window["start"],
                "trip_end": first_departure_window["end"],
                "preferred_trip_duration_hours": (
                    maximum_total_duration_minutes / 60.0
                ),
                "traveler_count": shared["adult_count"],
                "baggage_policy": {
                    "cabin_baggage": shared["baggage_policy"]["cabin_baggage"],
                    "checked_bag_count": shared["baggage_policy"]["checked_bag_count"],
                },
                "base_currency": shared["base_currency"],
                "fx_to_base": shared["fx_to_base"],
            }
        )
        airport_catalog_ref = self.airport_catalog_ref
        if airport_catalog_ref is not None:
            config["airport_catalog"] = airport_catalog_ref
        maximum_leg_price = config.get("quality_gate", {}).pop(
            "maximum_leg_price_in_base_currency", None
        )
        if maximum_leg_price is not None:
            config["quality_gate"]["maximum_leg_price"] = maximum_leg_price
            config["quality_gate"]["maximum_leg_price_currency"] = shared[
                "base_currency"
            ]
        for public_field in (
            "adult_count",
            "first_departure_window",
            "maximum_total_duration_minutes",
            "overnight_allowed",
            "self_transfer_policy",
        ):
            config.pop(public_field, None)
        airports: dict[str, Any] = {}
        catalog_ref = config.pop("airport_catalog", None)
        if catalog_ref is not None:
            reference = Path(str(catalog_ref))
            candidates = [reference] if reference.is_absolute() else [
                Path.cwd() / reference,
                self.product_root / reference,
                self.product_root / reference.name,
            ]
            catalog_path = next(
                (path.resolve() for path in candidates if path.is_file()), None
            )
            if catalog_path is None:
                raise HarnessProtocolError(
                    f"random-loop airport_catalog does not exist: {catalog_ref}"
                )
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot load random-loop airport_catalog: {exc}"
                ) from exc
            catalog_airports = (
                catalog.get("airports", catalog) if isinstance(catalog, dict) else None
            )
            if not isinstance(catalog_airports, dict):
                raise HarnessProtocolError(
                    "random-loop airport_catalog must contain an airports object"
                )
            merged_airports = copy.deepcopy(catalog_airports)
            if isinstance(airports, dict):
                merged_airports.update(copy.deepcopy(airports))
            airports = merged_airports
        transit_catalog_ref = self.airport_transit_catalog_ref
        if transit_catalog_ref is not None:
            reference = Path(str(transit_catalog_ref))
            candidates = [reference] if reference.is_absolute() else [
                Path.cwd() / reference,
                self.product_root / reference,
                self.product_root / reference.name,
            ]
            transit_path = next(
                (path.resolve() for path in candidates if path.is_file()), None
            )
            if transit_path is None:
                raise HarnessProtocolError(
                    "random-loop airport_transit_catalog does not exist: "
                    f"{transit_catalog_ref}"
                )
            try:
                config["_airport_transit_catalog"] = (
                    self.engine.AIRPORT_TRANSIT.load_catalog(transit_path)
                )
            except (OSError, ValueError, self.engine.AIRPORT_TRANSIT.AirportTransitError) as exc:
                raise HarnessProtocolError(
                    f"cannot load random-loop airport_transit_catalog: {exc}"
                ) from exc
        quality_gate = config.setdefault("quality_gate", {})
        if not isinstance(quality_gate, dict):
            raise HarnessProtocolError("random-loop quality_gate must be an object")
        if quality_gate.get("maximum_leg_price") is not None:
            quality_gate.setdefault(
                "maximum_leg_price_currency", config.get("base_currency")
            )
        try:
            state = self.engine.init_session(config, airports)
        except self.engine.RouteEngineError as exc:
            raise HarnessProtocolError(f"random-loop initialization failed: {exc}") from exc
        _write_json_atomic(self._session_path(product_directory), state)
        return state

    def load_state(self, product_directory: Path) -> dict[str, Any]:
        try:
            value = json.loads(self._session_path(product_directory).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessProtocolError(f"cannot load random-loop Session: {exc}") from exc
        if not isinstance(value, dict) or value.get("engine_version") != self.engine.ENGINE_VERSION:
            raise HarnessProtocolError("random-loop Session version is unsupported")
        value.setdefault("finalization_status", "open")
        value.setdefault("finalization_history", [])
        value.setdefault("scope_coverage", self.engine.SCOPE_COVERAGE.new_ledger())
        return value

    def _save_state(self, product_directory: Path, state: dict[str, Any]) -> None:
        _write_json_atomic(self._session_path(product_directory), state)

    def inspect(self, state: dict[str, Any]) -> dict[str, Any]:
        audit = self.engine.finalization_audit(state)
        investigations = []
        for sequence, request in enumerate(self.engine.build_search_requests(state), 1):
            task_id = str(request.get("request_id") or f"request-{sequence}")
            investigations.append(
                {
                    "kind": "investigate",
                    "action_key": f"investigate:{task_id}",
                    "task_id": task_id,
                    "status": "pending",
                    "hypothesis_id": request.get("parent_id"),
                    "sequence": sequence,
                }
            )
        actions = list(investigations)
        if audit["can_finalize"]:
            actions.append({"kind": "finalize", "action_key": "finalize"})
        coverage_audit = audit.get("scope_coverage_audit", {})
        unresolved = list(coverage_audit.get("unresolved_request_ids", []))
        pending_coverage = list(coverage_audit.get("pending_request_ids", []))
        stalled_pending = pending_coverage if pending_coverage and not investigations else []
        return {
            "control_version": "1.0",
            "product_id": self.product_id,
            "session_id": f"random-loop-{state.get('engine_version')}",
            "active_hypothesis_id": None,
            "next_candidate_id": audit.get("next_candidate_id"),
            "has_complete_result": bool(state.get("completed")),
            "can_finalize": bool(audit["can_finalize"]),
            "finalization_issues": copy.deepcopy(audit["blockers"]),
            "finalization_outcome": audit.get("outcome"),
            "draft_reason": (
                "Random-loop search has unresolved Provider coverage partitions."
                if unresolved
                else "Random-loop search has pending coverage but no executable work under the current budget."
                if stalled_pending
                else None
            ),
            "blocked_action": {
                "type": "resolve_scope_coverage",
                "request_ids": unresolved,
            }
            if unresolved
            else {
                "type": "resume_scope_coverage_or_expand_budget",
                "request_ids": stalled_pending,
            }
            if stalled_pending
            else None,
            "investigation_actions": investigations,
            "disposition_actions": [],
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
                raise HarnessProtocolError("random-loop investigation action has no task_id")
            work = self.prepare_investigation(
                product_directory,
                task_id,
                "ProductKernel scheduled required Mission work.",
            )
            state = self.load_state(product_directory)
            request = self._request(state, task_id)
            attempt_number = 1 + sum(
                1
                for entry in state.get("browser_query_ledger", {}).get("entries", [])
                if isinstance(entry, dict) and str(entry.get("request_id")) == task_id
            )
            item_kind = "browser_research"
            items.append(
                _build_adapter_research_item(
                    task_id=task_id,
                    attempt_number=attempt_number,
                    session_identity=state.get("session_id") or state.get("run_id"),
                    item_kind=item_kind,
                    work=work,
                    hypothesis_id=request.get("parent_id"),
                    research_kind="random_loop_search",
                    journey_scope=None,
                    identify=identify,
                )
            )
        return items

    def normalize_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return normalize_attempt(self.product_id, attempt)

    def _request(self, state: dict[str, Any], task_id: str) -> dict[str, Any]:
        request = next(
            (
                item
                for item in self.engine.build_search_requests(state)
                if str(item.get("request_id")) == task_id
            ),
            None,
        )
        if request is None:
            raise HarnessProtocolError(f"random-loop task is not actionable: {task_id}")
        return request

    def prepare_investigation(
        self, product_directory: Path, task_id: str, reason: str
    ) -> dict[str, Any]:
        state = self.load_state(product_directory)
        request = self._request(state, task_id)
        ordered_sources = (
            request.get("source_strategy", {}).get("selection_mode") == "ordered"
        )
        source_batch = self._ordered_source_batch(request) if ordered_sources else []
        brief_request = copy.deepcopy(request)
        if source_batch:
            brief_request["required_source_batch"] = source_batch
        result_required = ["observation_kind", "source", "offers"]
        if ordered_sources:
            result_required.extend(
                [
                    "source_id",
                    "request_id",
                    "submitted_scope",
                    "coverage_complete",
                    "acquisition",
                ]
            )
        else:
            result_required.append("keep_parent_open")
        return {
            "work_version": "1.0",
            "product_id": self.product_id,
            "task_id": task_id,
            "hypothesis_id": request.get("parent_id"),
            "brief": {
                "research_kind": "random_loop_search",
                "request": brief_request,
                "ai_decision_reason": reason,
                "evidence_rule": (
                    "Execute request.required_source_batch in order when present. Every ordered "
                    "group must report submitted_scope, coverage_complete, and acquisition. If "
                    "one or more airports or date cells cannot be represented, stop the batch and "
                    "return exact scope_partitions: put the supported remainder first with "
                    "source_disposition=retry_current and only the unsupported remainder with "
                    "source_disposition=advance_source. Continue the whole scope to the next "
                    "source only after source_unavailable or scope_unsupported with no viable "
                    "partition. After an AZair "
                    "no_market_result, continue to Google only for root initial or closure_probe; "
                    "an ordinary continuation stops and backtracks. Stop after payable, "
                    "scope_incomplete, research_only, or a partition plan. Return one "
                    "search_results group per "
                    "executed source and every field-complete offer visible in its scope. "
                    "Preserve source_id, request_id, source URL, and offset-aware observation time."
                ),
            },
            "attempt_schema": {
                "type": "object",
                "required": ["kind", "product_payload"],
                "properties": {
                    "kind": {"const": "observations"},
                    "product_payload": {
                        "type": "object",
                        "required": ["search_results"],
                        "properties": {
                            "airports": {
                                "type": "object",
                                "description": (
                                    "Airport metadata keyed by IATA code for every airport not "
                                    "already present in the brief. Include latitude, longitude, "
                                    "timezone, name, and country_code when observed."
                                ),
                            },
                            "search_results": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": result_required,
                                    "properties": {
                                        "observation_kind": {
                                            "enum": [
                                                "payable",
                                                "source_unavailable",
                                                "scope_unsupported",
                                                "scope_incomplete",
                                                "no_market_result",
                                                "research_only",
                                            ]
                                        },
                                        "source_id": {
                                            "enum": source_batch
                                        }
                                        if ordered_sources
                                        else {"type": "string"},
                                        "request_id": {"const": task_id},
                                        "source": {
                                            "type": "object",
                                            "required": ["name", "url", "observed_at"],
                                            "properties": {
                                                "name": {"type": "string"},
                                                "url": {"type": "string"},
                                                "observed_at": {"type": "string"},
                                            },
                                        },
                                        "offers": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": [
                                                    "origin",
                                                    "destination",
                                                    "departure",
                                                    "arrival",
                                                    "price",
                                                    "currency",
                                                    "distance_km",
                                                    "flight_no",
                                                    "booking_url",
                                                    "notes",
                                                ],
                                            },
                                        },
                                        "submitted_scope": {
                                            "type": "object",
                                            "required": [
                                                "origins",
                                                "destinations",
                                                "earliest_departure",
                                                "latest_departure",
                                            ],
                                            "properties": {
                                                "origins": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "items": {"type": "string"},
                                                },
                                                "destinations": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "items": {"type": "string"},
                                                },
                                                "earliest_departure": {"type": "string"},
                                                "latest_departure": {"type": "string"},
                                            },
                                        },
                                        "coverage_complete": {"type": "boolean"},
                                        "scope_partitions": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": ["scope", "source_disposition"],
                                                "properties": {
                                                    "scope": {"type": "object"},
                                                    "source_disposition": {
                                                        "enum": [
                                                            "retry_current",
                                                            "advance_source",
                                                        ]
                                                    },
                                                    "reason": {"type": "string"},
                                                },
                                            },
                                        },
                                        "acquisition": {
                                            "type": "object",
                                            "required": ["method"],
                                            "properties": {
                                                "method": {
                                                    "enum": [
                                                        "browser",
                                                        "http_recovery",
                                                        "provider_api",
                                                        "cache",
                                                    ]
                                                },
                                                "detail": {"type": "string"},
                                            },
                                        },
                                        "keep_parent_open": {"type": "boolean"},
                                        "detail": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
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
        payload = attempt.get("product_payload")
        if attempt.get("kind") != "observations" or not isinstance(payload, dict):
            raise HarnessProtocolError("random-loop attempt requires product_payload")
        state = self.load_state(product_directory)
        request = self._request(state, str(action["task_id"]))
        ordered_sources = (
            request.get("source_strategy", {}).get("selection_mode") == "ordered"
        )
        source_batch = self._ordered_source_batch(request) if ordered_sources else []
        groups = copy.deepcopy(payload.get("search_results"))
        if not isinstance(groups, list) or not groups:
            raise HarnessProtocolError("random-loop product_payload.search_results must be non-empty")
        group_count = len(groups)
        for index, group in enumerate(groups, 1):
            if not isinstance(group, dict):
                raise HarnessProtocolError("random-loop search result group must be an object")
            group.setdefault("request_id", request["request_id"])
            if ordered_sources:
                if index > len(source_batch):
                    raise HarnessProtocolError(
                        "random-loop ordered attempt exceeds the published source batch"
                    )
                expected_source_id = source_batch[index - 1]
                actual_source_id = str(group.get("source_id", "")).strip()
                if actual_source_id != expected_source_id:
                    raise HarnessProtocolError(
                        "random-loop ordered request requires source_id="
                        f"{expected_source_id}; received {actual_source_id or '<missing>'}"
                    )
                missing_coverage = [
                    field
                    for field in ("submitted_scope", "coverage_complete", "acquisition")
                    if field not in group
                ]
                if missing_coverage:
                    raise HarnessProtocolError(
                        "random-loop ordered result is missing coverage fields: "
                        + ", ".join(missing_coverage)
                    )
                group.setdefault("keep_parent_open", False)
                observation_kind = self.engine.normalize_ordered_observation_kind(
                    group.get("observation_kind")
                )
                group["observation_kind"] = observation_kind
                if (
                    index < group_count
                    and not self._batch_can_continue(
                        request,
                        expected_source_id,
                        group,
                    )
                ):
                    raise HarnessProtocolError(
                        "random-loop source batch cannot continue after this disposition"
                    )
            if not isinstance(group.get("source"), dict):
                source_id = str(group.get("source_id", "")).strip()
                source_url = str(group.get("source_url", "")).strip()
                observed_at = str(group.get("observed_at", "")).strip()
                if source_id and source_url and observed_at:
                    group["source"] = {
                        "name": source_id,
                        "url": source_url,
                        "observed_at": observed_at,
                    }
            source_url = str(
                group.get("source", {}).get("url", "")
                if isinstance(group.get("source"), dict)
                else ""
            ).strip()
            if (
                not group.get("offers")
                and group.get("observation_kind") in {"unknown", "research_only"}
                and not source_url.lower().startswith(("http://", "https://"))
            ):
                reason = str(group.get("detail") or "Browser runtime loaded no source page")
                raise HarnessBlockedError(
                    reason,
                    {
                        "type": "retry_browser_runtime",
                        "source_url": source_url or None,
                    },
                )
            if (
                not group.get("offers")
                and group.get("observation_kind") in {"unknown", "research_only"}
            ):
                group["keep_parent_open"] = True
            group.setdefault(
                "query_id",
                self._default_query_id(str(action["action_id"]), index, group_count),
            )
            group.setdefault("parent_id", request["parent_id"])
            group.setdefault("ai_decision_reason", action.get("reason", "not recorded"))
        candidates = {"search_results": groups}
        if isinstance(payload.get("airports"), dict):
            candidates["airports"] = copy.deepcopy(payload["airports"])
        try:
            if ordered_sources:
                airport_updates = candidates.get("airports", {})
                for group in groups:
                    current_request = self._request(state, str(action["task_id"]))
                    expected_source_id = str(
                        current_request.get("required_source_id", "")
                    )
                    if group.get("source_id") != expected_source_id:
                        raise HarnessProtocolError(
                            "random-loop source batch no longer matches engine-required source "
                            f"{expected_source_id}"
                        )
                    state = self.engine.expand_session(
                        state,
                        {
                            "search_results": [group],
                            "airports": copy.deepcopy(airport_updates),
                        },
                    )
            else:
                state = self.engine.expand_session(state, candidates)
        except self.engine.RouteEngineError as exc:
            raise HarnessProtocolError(f"random-loop evidence transaction failed: {exc}") from exc
        self._save_state(product_directory, state)
        return state

    def attempt_committed(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> bool:
        groups = self._canonical_replay_groups(state, action, attempt)
        group_count = len(groups)
        expected: dict[str, str] = {}
        for index, raw_group in enumerate(groups, 1):
            if not isinstance(raw_group, dict):
                continue
            group = copy.deepcopy(raw_group)
            group.setdefault(
                "query_id",
                self._default_query_id(str(action["action_id"]), index, group_count),
            )
            query_id, content_sha256 = self.engine.browser_query_identity(group)
            expected[query_id] = content_sha256
        recorded = {
            str(entry.get("query_id")): str(entry.get("content_sha256"))
            for entry in state.get("browser_query_ledger", {}).get("entries", [])
        }
        if not expected:
            return False
        for query_id, content_sha256 in expected.items():
            recorded_sha256 = recorded.get(query_id)
            if recorded_sha256 is None:
                return False
            if recorded_sha256 != content_sha256:
                raise HarnessProtocolError(
                    "random-loop Receipt identity was replayed with conflicting Evidence"
                )
        return True

    def _canonical_replay_groups(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Rebuild the exact group content hashed by the Product Kernel."""

        payload = attempt.get("product_payload")
        raw_groups = payload.get("search_results") if isinstance(payload, dict) else None
        if not isinstance(raw_groups, list):
            return []
        try:
            request = self._request(state, str(action["task_id"]))
        except HarnessProtocolError:
            request = None
        ledger_entries = {
            str(entry.get("query_id")): entry
            for entry in state.get("browser_query_ledger", {}).get("entries", [])
            if isinstance(entry, dict) and entry.get("query_id")
        }
        groups = copy.deepcopy(raw_groups)
        group_count = len(groups)
        for index, group in enumerate(groups, 1):
            if not isinstance(group, dict):
                continue
            group.setdefault(
                "query_id",
                self._default_query_id(str(action["action_id"]), index, group_count),
            )
            persisted = ledger_entries.get(str(group.get("query_id")))
            ordered_sources = (
                request.get("source_strategy", {}).get("selection_mode") == "ordered"
                if isinstance(request, dict)
                else isinstance(persisted, dict) and "request_id" in persisted
            )
            group.setdefault(
                "request_id",
                request["request_id"]
                if isinstance(request, dict)
                else (
                    persisted.get("request_id")
                    if isinstance(persisted, dict)
                    else action.get("task_id")
                ),
            )
            if ordered_sources:
                group["observation_kind"] = (
                    self.engine.normalize_ordered_observation_kind(
                        group.get("observation_kind")
                    )
                )
                group.setdefault("keep_parent_open", False)
            if not isinstance(group.get("source"), dict):
                source_id = str(group.get("source_id", "")).strip()
                source_url = str(group.get("source_url", "")).strip()
                observed_at = str(group.get("observed_at", "")).strip()
                if source_id and source_url and observed_at:
                    group["source"] = {
                        "name": source_id,
                        "url": source_url,
                        "observed_at": observed_at,
                    }
            if (
                not group.get("offers")
                and group.get("observation_kind") in {"unknown", "research_only"}
            ):
                group["keep_parent_open"] = True
            group.setdefault(
                "parent_id",
                request["parent_id"]
                if isinstance(request, dict)
                else persisted.get("parent_id")
                if isinstance(persisted, dict)
                else None,
            )
            group.setdefault(
                "ai_decision_reason", action.get("reason", "not recorded")
            )
        return groups

    @staticmethod
    def _default_query_id(action_id: str, index: int, group_count: int) -> str:
        if group_count == 1:
            return action_id
        return f"{action_id}:group-{index:03d}"

    @staticmethod
    def _ordered_source_batch(request: dict[str, Any]) -> list[str]:
        order = request.get("required_source_order", [])
        index = request.get("required_source_index")
        if not isinstance(order, list) or not isinstance(index, int):
            raise HarnessProtocolError(
                "random-loop ordered request is missing source order metadata"
            )
        remaining = request.get("browser_queries_remaining")
        batch_size = ORDERED_SOURCE_BATCH_SIZE
        if isinstance(remaining, int):
            batch_size = min(batch_size, max(0, remaining))
        batch = [str(item) for item in order[index : index + batch_size]]
        if not batch or batch[0] != str(request.get("required_source_id", "")):
            raise HarnessProtocolError(
                "random-loop ordered request has inconsistent source batch metadata"
            )
        return batch

    def _batch_can_continue(
        self, request: dict[str, Any], source_id: str, group: dict[str, Any]
    ) -> bool:
        observation_kind = str(group.get("observation_kind") or "")
        if group.get("scope_partitions"):
            return False
        if observation_kind in {"source_unavailable", "scope_unsupported"}:
            return True
        return (
            observation_kind == "no_market_result"
            and source_id == self.engine.PRIMARY_SOURCE_ID
            and request.get("request_kind")
            in self.engine.BUSINESS_CHALLENGER_REQUEST_KINDS
        )

    def apply_control_action(
        self, product_directory: Path, action: dict[str, Any]
    ) -> dict[str, Any]:
        raise HarnessProtocolError(
            f"random-loop published unsupported control action: {action.get('kind')}"
        )

    def finalize(self, product_directory: Path, reason: str) -> dict[str, Any]:
        state = self.load_state(product_directory)
        if state.get("finalization_status") == "final":
            return self._materialize_final_result(
                product_directory, state, reason
            )
        self._remember_finalization_reason(product_directory, reason)
        try:
            state, result = self.engine.finalize_result(state, reason)
        except self.engine.RouteEngineError as exc:
            raise HarnessProtocolError(f"random-loop finalization failed: {exc}") from exc
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
