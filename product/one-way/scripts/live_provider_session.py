from __future__ import annotations

"""Auditable one-way browser-provider session for the combination engine.

The browser remains an AI-controlled external collector.  This module is the
deterministic boundary: it emits bounded research tasks, validates the returned
evidence, admits only observed facts as TravelEdges, reruns the route engine,
and persists the complete JSON state.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from flight_search_harness.search_effort import search_effort_policy


SESSION_VERSION = "0.7.1"
FRONTIER_CONTROL_VERSION = "1.2"
TRIP_SCOPE = "one_way"
VERIFIED_STATUS = "verified_observed"
TERMINAL_STATUSES = {VERIFIED_STATUS, "no_result", "rejected"}
TASK_STATUSES = {"candidate", "pending", *TERMINAL_STATUSES, "superseded"}
HYPOTHESIS_ROOT_ID = "one-way-search-root"
HYPOTHESIS_TERMINAL_STATUSES = {"complete", "hard_dead", "deferred_low_value"}
HYPOTHESIS_ACTIONS = {
    "select",
    "complete",
    "hard_dead",
    "deferred_low_value",
}


class LiveProviderError(ValueError):
    pass


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


def _load_combination_engine() -> Any:
    module_path = Path(__file__).resolve().with_name("combination_engine.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_live_provider_engine", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load combination engine: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_combination_engine()


def _load_provider_execution_scheduler() -> Any:
    module_path = Path(__file__).resolve().with_name("provider_execution_scheduler.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_combination_provider_execution_scheduler", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Provider execution scheduler: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROVIDER_SCHEDULER = _load_provider_execution_scheduler()


def _load_frontier_exploration() -> Any:
    module_path = Path(__file__).resolve().with_name("frontier_exploration.py")
    spec = importlib.util.spec_from_file_location(
        "low_cost_one_way_frontier_exploration", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load frontier exploration: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FRONTIER_EXPLORATION = _load_frontier_exploration()


def _refresh_frontier_exploration(state: dict[str, Any]) -> None:
    try:
        archive = FRONTIER_EXPLORATION.build_archive(state)
        view = FRONTIER_EXPLORATION.build_research_view(state, archive)
    except FRONTIER_EXPLORATION.FrontierExplorationError as exc:
        raise LiveProviderError(f"frontier exploration failed: {exc}") from exc
    state["frontier_archive"] = archive
    state["frontier_research_view"] = view


DERIVED_SESSION_FIELDS = {
    "active_incumbent",
    "frontier_archive",
    "frontier_research_view",
    "hypothesis_tree",
    "provider_coverage_audit",
}

DERIVED_TASK_FIELDS = {
    "attempt_count",
    "status",
    "published_round",
    "publication_sequence",
    "publication_hold_reason",
    "attempt_charged",
    "attempt_number",
    "terminal_round",
    "ai_selected_outside_script_recommendation",
    "bundle_id",
    "admitted_edge_ids",
    "assigned_source_fulfillment",
}


def _restore_research_questions(state: dict[str, Any]) -> None:
    questions = state.get("research_questions")
    tasks = state.get("research_tasks")
    if not isinstance(tasks, list) and isinstance(questions, list):
        state["research_tasks"] = copy.deepcopy(questions)
    elif not isinstance(questions, list) and isinstance(tasks, list):
        state["research_questions"] = copy.deepcopy(tasks)


def _restore_finalization_view(state: dict[str, Any]) -> None:
    canonical = state.get("finalization")
    if not isinstance(canonical, dict):
        canonical = {
            "status": state.get("finalization_status", "open"),
            "history": copy.deepcopy(state.get("finalization_history", [])),
        }
        if state.get("finalization_invalidated_reason") is not None:
            canonical["invalidated_reason"] = state[
                "finalization_invalidated_reason"
            ]
        state["finalization"] = canonical
    state["finalization_status"] = str(canonical.get("status", "open"))
    state["finalization_history"] = copy.deepcopy(canonical.get("history", []))
    if canonical.get("invalidated_reason") is not None:
        state["finalization_invalidated_reason"] = canonical[
            "invalidated_reason"
        ]
    else:
        state.pop("finalization_invalidated_reason", None)


def finalization_evaluated_at(state: dict[str, Any]) -> datetime | None:
    """Return the persisted clock used to adjudicate the final result."""

    canonical = state.get("finalization")
    value = canonical.get("evaluated_at") if isinstance(canonical, dict) else None
    return _datetime(value, "state.finalization.evaluated_at") if value else None


def _restore_hypothesis_views(state: dict[str, Any]) -> None:
    legacy_active = state.get("active_hypothesis")
    legacy_outcomes = state.get("hypothesis_outcomes")
    legacy_tree = state.get("hypothesis_tree")
    hypotheses = state.get("hypotheses")
    if not isinstance(hypotheses, dict):
        hypotheses = {}
    tree_nodes = (
        legacy_tree.get("nodes", {}) if isinstance(legacy_tree, dict) else {}
    )
    for task in state.get("research_tasks", []):
        if not isinstance(task, dict) or not task.get("hypothesis_id"):
            continue
        hypothesis_id = str(task["hypothesis_id"])
        old_node = tree_nodes.get(hypothesis_id, {})
        entry = hypotheses.setdefault(
            hypothesis_id,
            {
                "hypothesis_id": hypothesis_id,
                "parent_hypothesis_id": task.get("parent_hypothesis_id"),
                "hypothesis_kind": task.get("hypothesis_kind"),
                "route_shape": task.get("route_shape"),
                "geographic_cell": task.get("geographic_cell"),
                "anchor_edge_ids": copy.deepcopy(task.get("anchor_edge_ids", [])),
            },
        )
        if isinstance(old_node, dict) and old_node.get("sibling_order") is not None:
            entry.setdefault("sibling_order", int(old_node["sibling_order"]))
    for hypothesis_id, entry in hypotheses.items():
        if not isinstance(entry, dict):
            continue
        tree_nodes.setdefault(
            str(hypothesis_id),
            {
                "hypothesis_id": str(hypothesis_id),
                "parent_hypothesis_id": entry.get("parent_hypothesis_id"),
                "sibling_order": int(entry.get("sibling_order", 0)),
            },
        )
    active_id = state.get("active_hypothesis_id")
    if not active_id and isinstance(legacy_active, dict):
        active_id = legacy_active.get("hypothesis_id")
    active_id = str(active_id) if active_id else None
    if active_id and isinstance(legacy_active, dict):
        entry = hypotheses.setdefault(
            active_id, {"hypothesis_id": active_id}
        )
        active_state = copy.deepcopy(legacy_active)
        active_state.pop("tree_path", None)
        active_state.pop("next_task_id", None)
        entry["active_state"] = active_state
    state["hypotheses"] = hypotheses
    state["active_hypothesis_id"] = active_id
    dispositions = state.get("hypothesis_dispositions")
    if not isinstance(dispositions, dict):
        dispositions = (
            copy.deepcopy(legacy_outcomes)
            if isinstance(legacy_outcomes, dict)
            else {}
        )
        state["hypothesis_dispositions"] = dispositions
    state["hypothesis_outcomes"] = copy.deepcopy(dispositions)
    if active_id:
        active_state = hypotheses.get(active_id, {}).get("active_state")
        state["active_hypothesis"] = (
            copy.deepcopy(active_state)
            if isinstance(active_state, dict)
            else {"hypothesis_id": active_id, "status": "researching"}
        )
    else:
        state["active_hypothesis"] = None
    backtracks = state.get("hypothesis_backtracks")
    if not isinstance(backtracks, list):
        backtracks = (
            copy.deepcopy(legacy_tree.get("backtrack_history", []))
            if isinstance(legacy_tree, dict)
            else []
        )
        state["hypothesis_backtracks"] = backtracks
    state["hypothesis_tree"] = {
        "nodes": copy.deepcopy(tree_nodes),
        "backtrack_count": len(backtracks),
        "backtrack_history": copy.deepcopy(backtracks),
    }


def _restore_research_question_views(state: dict[str, Any]) -> None:
    active = state.get("active_hypothesis")
    active_task_id = (
        str(active.get("selected_task_id"))
        if isinstance(active, dict) and active.get("selected_task_id")
        else None
    )
    for task in state.get("research_tasks", []):
        if not isinstance(task, dict):
            continue
        legacy_status = task.get("status")
        question_status = task.get("question_status")
        if question_status not in {"open", "closed", "superseded"}:
            if legacy_status == "superseded":
                question_status = "superseded"
            elif legacy_status in TERMINAL_STATUSES:
                question_status = "closed"
            else:
                question_status = "open"
            task["question_status"] = question_status
        if question_status == "superseded":
            task["status"] = "superseded"
        elif question_status == "closed":
            task["status"] = str(
                task.get("last_attempt_status")
                or legacy_status
                or "rejected"
            )
        else:
            task["status"] = (
                "pending"
                if str(task.get("task_id") or "") == active_task_id
                else "candidate"
            )


def _capture_hypothesis_authority(state: dict[str, Any]) -> None:
    hypotheses = state.get("hypotheses")
    if not isinstance(hypotheses, dict):
        hypotheses = {}
    tree = state.get("hypothesis_tree")
    tree_nodes = tree.get("nodes", {}) if isinstance(tree, dict) else {}
    for task in state.get("research_tasks", []):
        if not isinstance(task, dict) or not task.get("hypothesis_id"):
            continue
        hypothesis_id = str(task["hypothesis_id"])
        node = tree_nodes.get(hypothesis_id, {})
        entry = hypotheses.setdefault(hypothesis_id, {"hypothesis_id": hypothesis_id})
        entry.update(
            {
                "parent_hypothesis_id": task.get("parent_hypothesis_id"),
                "hypothesis_kind": task.get("hypothesis_kind"),
                "route_shape": task.get("route_shape"),
                "geographic_cell": task.get("geographic_cell"),
                "anchor_edge_ids": copy.deepcopy(task.get("anchor_edge_ids", [])),
            }
        )
        if isinstance(node, dict) and node.get("sibling_order") is not None:
            entry["sibling_order"] = int(node["sibling_order"])
    active = state.get("active_hypothesis")
    if "active_hypothesis" in state:
        active_id = (
            str(active.get("hypothesis_id"))
            if isinstance(active, dict) and active.get("hypothesis_id")
            else None
        )
    else:
        active_id = (
            str(state.get("active_hypothesis_id"))
            if state.get("active_hypothesis_id")
            else None
        )
    for hypothesis_id, entry in hypotheses.items():
        if not isinstance(entry, dict):
            continue
        if hypothesis_id == active_id:
            if isinstance(active, dict):
                active_state = copy.deepcopy(active)
                active_state.pop("tree_path", None)
                active_state.pop("next_task_id", None)
                entry["active_state"] = active_state
        else:
            entry.pop("active_state", None)
    state["hypotheses"] = hypotheses
    state["active_hypothesis_id"] = active_id
    outcomes = state.get("hypothesis_outcomes")
    if isinstance(outcomes, dict):
        state["hypothesis_dispositions"] = copy.deepcopy(outcomes)
    else:
        state.setdefault("hypothesis_dispositions", {})
    if isinstance(tree, dict):
        state["hypothesis_backtracks"] = copy.deepcopy(
            tree.get("backtrack_history", [])
        )
    else:
        state.setdefault("hypothesis_backtracks", [])


def compact_session(state: dict[str, Any]) -> dict[str, Any]:
    """Return the durable Product Session without reconstructable views."""

    compact = copy.deepcopy(state)
    _capture_hypothesis_authority(compact)
    tasks = compact.get("research_tasks")
    if not isinstance(tasks, list):
        tasks = copy.deepcopy(compact.get("research_questions", []))
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("status")
        if status is None and task.get("question_status") in {
            "open",
            "closed",
            "superseded",
        }:
            pass
        elif status == "superseded":
            task["question_status"] = "superseded"
        elif status in TERMINAL_STATUSES:
            task["question_status"] = "closed"
            task["last_attempt_status"] = status
        else:
            task["question_status"] = "open"
        task.pop("attempts", None)
        for field in DERIVED_TASK_FIELDS:
            task.pop(field, None)
    compact["research_questions"] = tasks
    compact.pop("research_tasks", None)
    ledger = compact.get("budget_ledger")
    if isinstance(ledger, dict):
        compact["budget_ledger"] = {
            "limit": ledger.get("limit"),
            "attempted_attempt_ids": list(
                dict.fromkeys(ledger.get("attempted_attempt_ids", []))
            ),
            "events": [
                copy.deepcopy(event)
                for event in ledger.get("events", [])
                if isinstance(event, dict)
                and event.get("event_type")
                not in {"tasks_published", "ai_selected_candidate", "ai_selected_frontier_task"}
            ],
        }
    if isinstance(compact.get("research_credits"), dict):
        compact["research_credits"] = copy.deepcopy(
            compact["research_credits"]
        )
        compact.pop("budget_ledger", None)
    existing_finalization = compact.get("finalization")
    if not isinstance(existing_finalization, dict):
        existing_finalization = {}
    has_expanded_finalization_view = (
        "finalization_status" in compact or "finalization_history" in compact
    )
    finalization = {
        "status": compact.get(
            "finalization_status", existing_finalization.get("status", "open")
        ),
        "history": copy.deepcopy(
            compact.get(
                "finalization_history", existing_finalization.get("history", [])
            )
        ),
    }
    if compact.get("finalization_invalidated_reason") is not None:
        finalization["invalidated_reason"] = compact[
            "finalization_invalidated_reason"
        ]
    elif (
        not has_expanded_finalization_view
        and existing_finalization.get("invalidated_reason") is not None
    ):
        finalization["invalidated_reason"] = existing_finalization[
            "invalidated_reason"
        ]
    evaluated_at = existing_finalization.get("evaluated_at")
    if isinstance(evaluated_at, str) and evaluated_at:
        finalization["evaluated_at"] = evaluated_at
    compact["finalization"] = finalization
    compact.pop("active_hypothesis", None)
    compact.pop("hypothesis_outcomes", None)
    compact.pop("finalization_status", None)
    compact.pop("finalization_history", None)
    compact.pop("finalization_invalidated_reason", None)
    for field in DERIVED_SESSION_FIELDS:
        compact.pop(field, None)
    return compact


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveProviderError(f"Cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveProviderError(f"{field} must be a non-empty string")
    return value.strip()


def _datetime(value: Any, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveProviderError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise LiveProviderError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise LiveProviderError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveProviderError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise LiveProviderError(f"{field} must be a finite non-negative number")
    return result


def _currency(value: Any, field: str) -> str:
    currency = _text(value, field).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise LiveProviderError(f"{field} must be a three-letter currency code")
    return currency


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field)
    if any(character.isspace() for character in url):
        raise LiveProviderError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise LiveProviderError(
            f"{field} must be an absolute HTTP(S) URL"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise LiveProviderError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise LiveProviderError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def _normalize_flight_identity(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LiveProviderError(f"{field} must be an object")
    identity: dict[str, Any] = {}
    for carrier_field in ("marketing_carrier", "operating_carrier"):
        if raw.get(carrier_field) is not None:
            identity[carrier_field] = _text(
                raw[carrier_field], f"{field}.{carrier_field}"
            )
    if not identity:
        raise LiveProviderError(
            f"{field} must include marketing_carrier or operating_carrier"
        )
    numbers = raw.get("flight_numbers")
    unknown_reason = raw.get("unknown_reason")
    if (numbers is None) == (unknown_reason is None):
        raise LiveProviderError(
            f"{field} must include exactly one of flight_numbers or unknown_reason"
        )
    if numbers is not None:
        if (
            not isinstance(numbers, list)
            or not numbers
            or not all(isinstance(item, str) and item.strip() for item in numbers)
        ):
            raise LiveProviderError(f"{field}.flight_numbers must be a non-empty string array")
        normalized_numbers = [item.strip().upper() for item in numbers]
        if len(set(normalized_numbers)) != len(normalized_numbers):
            raise LiveProviderError(f"{field}.flight_numbers must be unique")
        identity["flight_numbers"] = normalized_numbers
    else:
        if not isinstance(unknown_reason, dict):
            raise LiveProviderError(f"{field}.unknown_reason must be an object")
        code = _text(unknown_reason.get("code"), f"{field}.unknown_reason.code")
        allowed = {
            "not_disclosed_by_source",
            "not_visible_in_search_result",
            "provider_omitted",
        }
        if code not in allowed:
            raise LiveProviderError(
                f"{field}.unknown_reason.code must be one of: " + ", ".join(sorted(allowed))
            )
        reason = {"code": code}
        if unknown_reason.get("detail") is not None:
            reason["detail"] = _text(
                unknown_reason["detail"], f"{field}.unknown_reason.detail"
            )
        identity["unknown_reason"] = reason
    unknown_fields = sorted(
        set(raw)
        - {
            "marketing_carrier",
            "operating_carrier",
            "flight_numbers",
            "unknown_reason",
        }
    )
    if unknown_fields:
        raise LiveProviderError(
            f"{field} has unsupported fields: " + ", ".join(unknown_fields)
        )
    return identity


def _as_node_list(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(nodes[node_id]) for node_id in sorted(nodes)]


def _session_id(config: dict[str, Any], profile: dict[str, Any]) -> str:
    payload = {
        "trip_scope": TRIP_SCOPE,
        "profile_id": profile["profile_id"],
        "date_window": profile["date_window"],
        "origin_start_nodes": profile["origin_start_nodes"],
        "destination_end_nodes": profile["destination_end_nodes"],
        "base_currency": config["base_currency"],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"one-way-{digest}"


def _request_type(request: dict[str, Any]) -> str:
    raw = str(request.get("request_type", "unresolved_connector"))
    if raw == "discover_air_anchors":
        return (
            "destination_market_scan"
            if request.get("direction") == "arrive_by"
            else "origin_market_scan"
        )
    if raw == "discover_connector":
        return "unresolved_connector"
    if raw == "price_and_schedule_discovery":
        return (
            "direct_closure"
            if request.get("would_directly_reach_origin_pool")
            else "budgeted_predecessor_scan"
        )
    if raw == "refresh_observed_offer":
        return "refresh_quote"
    return raw


def _normalized_request(request: dict[str, Any]) -> dict[str, Any]:
    """Normalize both the v0.6 gap shape and the residual scheduler shape."""
    if not isinstance(request, dict):
        raise LiveProviderError("provider request must be an object")
    request_type = _request_type(request)
    date_window = request.get("date_window") or {}
    origins = request.get("origins")
    destinations = request.get("destinations")
    if origins is None:
        origins = [request.get("from_node")] if request.get("from_node") else []
    if destinations is None:
        destinations = [request.get("to_node")] if request.get("to_node") else []
    origins = [str(item).upper() for item in origins if item]
    destinations = [str(item).upper() for item in destinations if item]
    earliest = request.get("earliest_departure", date_window.get("start"))
    latest = request.get("latest_arrival", date_window.get("end"))
    if earliest is None or latest is None:
        raise LiveProviderError("provider request must include a bounded time window")
    earliest = _iso(_datetime(earliest, "provider_request.earliest_departure"))
    latest = _iso(_datetime(latest, "provider_request.latest_arrival"))
    if _datetime(latest, "provider_request.latest_arrival") <= _datetime(
        earliest, "provider_request.earliest_departure"
    ):
        raise LiveProviderError("provider request time window must be increasing")
    modes = request.get("suggested_modes", request.get("allowed_modes", ["flight"]))
    if not isinstance(modes, list) or not modes:
        raise LiveProviderError("provider request suggested_modes must be non-empty")
    modes = list(dict.fromkeys(str(item).lower() for item in modes))
    provider_kinds = request.get("provider_kinds") or [
        f"{mode}_schedule_or_offer" for mode in modes
    ]
    if not isinstance(provider_kinds, list) or not provider_kinds:
        raise LiveProviderError("provider request provider_kinds must be non-empty")
    endpoint_policy = request.get("endpoint_policy")
    if not isinstance(endpoint_policy, dict):
        if request_type in {"destination_market_scan", "budgeted_predecessor_scan"}:
            endpoint_policy = {"from": "discovered", "to": "allowed"}
        elif request_type == "origin_market_scan":
            endpoint_policy = {"from": "allowed", "to": "discovered"}
        elif request_type == "conventional_reference":
            endpoint_policy = {"from": "allowed", "to": "allowed"}
        else:
            endpoint_policy = {"from": "exact", "to": "exact"}
    policy_aliases = {
        "exact": "exact_node",
        "exact_node": "exact_node",
        "allowed": "node_pool",
        "pool": "node_pool",
        "node_pool": "node_pool",
        "discovered": "provider_authorized_anywhere",
        "provider_authorized_anywhere": "provider_authorized_anywhere",
    }
    normalized_from_policy = policy_aliases.get(str(endpoint_policy.get("from", "exact")))
    normalized_to_policy = policy_aliases.get(str(endpoint_policy.get("to", "exact")))
    if normalized_from_policy is None or normalized_to_policy is None:
        raise LiveProviderError("provider request endpoint_policy is unsupported")
    raw_date_search = request.get("date_search", {"mode": "bounded_window"})
    if not isinstance(raw_date_search, dict):
        raise LiveProviderError("provider request date_search must be an object")
    date_search = copy.deepcopy(raw_date_search)
    date_mode = str(date_search.get("mode", "bounded_window")).strip().lower()
    if date_mode not in {"exact_dates", "bounded_window", "fully_flexible"}:
        raise LiveProviderError("provider request date_search.mode is unsupported")
    date_search["mode"] = date_mode
    if date_mode == "exact_dates":
        target = date_search.get("target", {})
        exact_dates = date_search.get("exact_departure_dates")
        if exact_dates is None and isinstance(target, dict):
            exact_dates = target.get("dates")
        if not isinstance(exact_dates, list) or not exact_dates:
            raise LiveProviderError("exact_dates request requires exact_departure_dates")
        normalized_dates: list[str] = []
        for value in exact_dates:
            raw = str(value).strip()
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise LiveProviderError(
                    "date_search.exact_departure_dates must use YYYY-MM-DD"
                ) from exc
            if parsed.time() != datetime.min.time():
                raise LiveProviderError(
                    "date_search.exact_departure_dates must use YYYY-MM-DD"
                )
            normalized = parsed.date().isoformat()
            if normalized not in normalized_dates:
                normalized_dates.append(normalized)
        date_search["exact_departure_dates"] = normalized_dates
    raw_corridor = request.get("corridor_context")
    if raw_corridor is not None and not isinstance(raw_corridor, dict):
        raise LiveProviderError("provider request corridor_context must be an object")
    assigned_source = request.get("assigned_source")
    if assigned_source is not None:
        if not isinstance(assigned_source, dict):
            raise LiveProviderError("provider request assigned_source must be an object")
        required_source_fields = {
            "source_id",
            "name",
            "url",
            "source_type",
            "registry_priority",
            "execution_kind",
            "admission_policy",
        }
        missing = sorted(required_source_fields - set(assigned_source))
        if missing:
            raise LiveProviderError(
                "provider request assigned_source is missing: " + ", ".join(missing)
            )
        assigned_source = copy.deepcopy(assigned_source)
        assigned_source["source_id"] = _text(
            assigned_source["source_id"], "provider_request.assigned_source.source_id"
        ).lower()
        assigned_source["name"] = _text(
            assigned_source["name"], "provider_request.assigned_source.name"
        )
        assigned_source["url"] = _http_url(
            assigned_source["url"], "provider_request.assigned_source.url"
        )
    normalized = copy.deepcopy(request)
    normalized.update(
        {
            "request_type": request_type,
            "origins": origins,
            "destinations": destinations,
            "from_node": request.get("from_node")
            or (origins[0] if len(origins) == 1 else None),
            "to_node": request.get("to_node")
            or (destinations[0] if len(destinations) == 1 else None),
            "earliest_departure": earliest,
            "latest_arrival": latest,
            "suggested_modes": modes,
            "provider_kinds": list(dict.fromkeys(str(item) for item in provider_kinds)),
            "endpoint_policy": {
                "from": normalized_from_policy,
                "to": normalized_to_policy,
            },
            "budget_context": copy.deepcopy(request.get("budget_context", {})),
            "date_search": date_search,
            "corridor_context": copy.deepcopy(raw_corridor),
            "assigned_source": assigned_source,
            "parent_request_id": request.get("parent_request_id"),
            "parent_request_key": request.get("parent_request_key"),
            "optimistic_total_cost": request.get("optimistic_total_cost"),
        }
    )
    normalized.setdefault("straight_line_distance_km", None)
    return normalized


def _canonical_task_signature(request: dict[str, Any]) -> str:
    request = _normalized_request(request)
    identity = {
        "request_type": request["request_type"],
        "endpoint_policy": request["endpoint_policy"],
        "origins": request["origins"],
        "destinations": request["destinations"],
        "from_node": request.get("from_node"),
        "to_node": request.get("to_node"),
        "earliest_departure": request["earliest_departure"],
        "latest_arrival": request["latest_arrival"],
        "suggested_modes": request["suggested_modes"],
        "provider_kinds": request["provider_kinds"],
        "target_edge_id": request.get("target_edge_id"),
        "date_search": request.get("date_search"),
        "corridor_context": request.get("corridor_context"),
        "frontier_context": request.get("frontier_context"),
        "parent_hypothesis_id": request.get("parent_hypothesis_id"),
        "assigned_source_id": (
            request.get("assigned_source", {}).get("source_id")
            if isinstance(request.get("assigned_source"), dict)
            else None
        ),
        "parent_request_key": request.get("parent_request_key"),
        "optimistic_total_cost": request.get("optimistic_total_cost"),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _task_revision(request: dict[str, Any], signature: str) -> str:
    budget_sensitive = request.get("request_type") in {
        "direct_closure",
        "budgeted_predecessor_scan",
        "unresolved_connector",
    }
    payload = {
        "task_signature": signature,
        "budget_context": request.get("budget_context", {}) if budget_sensitive else {},
        "consumer_contexts": request.get("consumer_contexts", []),
        "date_search": request.get("date_search"),
        "corridor_context": request.get("corridor_context"),
        "frontier_context": request.get("frontier_context"),
        "assigned_source": request.get("assigned_source"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _research_task(
    request: dict[str, Any], sequence: int, generation: int = 1
) -> dict[str, Any]:
    request = _normalized_request(request)
    signature = _canonical_task_signature(request)
    revision = _task_revision(request, signature)
    task_id = f"task-{signature[:16]}-g{generation}"
    task = {
        "task_id": task_id,
        "task_signature": signature,
        "task_revision": revision,
        "generation": generation,
        "sequence": sequence,
        "status": "pending",
        "request_type": request["request_type"],
        "original_request_id": request.get("request_id"),
        "endpoint_policy": copy.deepcopy(request["endpoint_policy"]),
        "origins": list(request["origins"]),
        "destinations": list(request["destinations"]),
        "from_node": request.get("from_node"),
        "to_node": request.get("to_node"),
        "earliest_departure": request["earliest_departure"],
        "latest_arrival": request["latest_arrival"],
        "suggested_modes": list(request["suggested_modes"]),
        "traveler_count": request.get("traveler_count"),
        "baggage": copy.deepcopy(request.get("baggage")),
        "overnight_allowed": request.get("overnight_allowed"),
        "self_transfer_policy": request.get("self_transfer_policy"),
        "provider_kinds": list(request["provider_kinds"]),
        "straight_line_distance_km": request.get("straight_line_distance_km"),
        "budget_context": copy.deepcopy(request.get("budget_context", {})),
        "consumer_contexts": copy.deepcopy(request.get("consumer_contexts", [])),
        "date_search": copy.deepcopy(request.get("date_search", {"mode": "bounded_window"})),
        "corridor_context": copy.deepcopy(request.get("corridor_context")),
        "frontier_context": copy.deepcopy(request.get("frontier_context")),
        "selection_advice": copy.deepcopy(request.get("selection_advice")),
        "source_suggestions": copy.deepcopy(request.get("source_suggestions", [])),
        "hypothesis_id": request.get("hypothesis_id"),
        "hypothesis_kind": request.get("hypothesis_kind"),
        "parent_hypothesis_id": request.get("parent_hypothesis_id"),
        "route_shape": request.get("route_shape"),
        "geographic_cell": request.get("geographic_cell"),
        "anchor_edge_ids": copy.deepcopy(request.get("anchor_edge_ids", [])),
        "assigned_source": copy.deepcopy(request.get("assigned_source")),
        "parent_request_id": request.get("parent_request_id"),
        "parent_request_key": request.get("parent_request_key"),
        "evidence_contract_version": request.get(
            "evidence_contract_version", SESSION_VERSION
        ),
        "evidence_requirements": {
            "source_url": "absolute_http_or_https",
            "provider": "named_provider",
            "observed_at": "timezone_aware_iso_datetime",
            "endpoints": copy.deepcopy(request["endpoint_policy"]),
            "schedule": "exact_departure_and_arrival",
            "price": "exact_amount_and_currency",
            "baggage": "declared_baggage_policy_and_exact_mandatory_cost",
            "flight_identity": (
                "marketing_or_operating_carrier_plus_disclosed_flight_numbers_"
                "or_structured_unknown_reason; never_guess"
            ),
            "provider_offer": "keep_each_seller_offer_and_quote_snapshot_separate",
            "route_admission": VERIFIED_STATUS,
            "assigned_source_fulfillment": (
                "Use the assigned source. If it is research or metasearch, preserve "
                "that source in discovery_provenance and verify the payable quote "
                "with the named seller or operator."
                if request.get("assigned_source")
                else "legacy_generic_provider_task"
            ),
        },
        "attempt_count": 0,
        "attempt_bundle_ids": [],
        "question_status": "open",
    }
    if request.get("mode_transition_buffer") is not None:
        task["mode_transition_buffer"] = copy.deepcopy(
            request["mode_transition_buffer"]
        )
    task["research_credit_cost"] = research_credit_cost(task)
    return task


def price_challenge_assessment(
    state: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    """Assess one Work Item on the Mandatory Air Ticket Total basis."""

    incumbent = state.get("active_incumbent") or {}
    money_value = incumbent.get("mandatory_air_ticket_total")
    incumbent_amount = (
        money_value.get("amount")
        if isinstance(money_value, dict)
        else incumbent.get("total_trip_cost")
    )
    if not isinstance(incumbent_amount, (int, float)) or isinstance(
        incumbent_amount, bool
    ):
        return {"disposition": "baseline_required", "optimistic_total": None}
    if (
        task.get("request_type") != "internal_surface_transfer"
        and "flight" not in set(task.get("suggested_modes", []))
    ):
        return {
            "disposition": "non_air_excluded",
            "optimistic_total": None,
            "incumbent": float(incumbent_amount),
        }
    edge_map = {
        str(edge.get("edge_id")): edge
        for edge in state.get("edges", [])
        if isinstance(edge, dict) and edge.get("edge_id")
    }
    optimistic = 0.0
    for edge_id in task.get("anchor_edge_ids", []):
        edge = edge_map.get(str(edge_id))
        if not isinstance(edge, dict) or edge.get("mode") != "flight":
            continue
        effective, _ = ENGINE.edge_effective_cost(
            edge, state["profile"], state["config"]
        )
        baggage = ENGINE.to_base(
            edge["mandatory_baggage_cost"],
            state["config"],
            f"edge {edge_id}.mandatory_baggage_cost",
        )
        optimistic += effective + baggage
    raw_optimistic = task.get("optimistic_mandatory_air_ticket_total")
    if isinstance(raw_optimistic, (int, float)) and not isinstance(
        raw_optimistic, bool
    ):
        optimistic = max(optimistic, float(raw_optimistic))
    threshold = float(
        state["research_credits"].get("price_challenge_threshold", 0.05)
    )
    cutoff = float(incumbent_amount) * (1.0 - threshold)
    return {
        "disposition": "continue" if optimistic < cutoff else "threshold_stop",
        "optimistic_total": round(optimistic, 2),
        "incumbent": round(float(incumbent_amount), 2),
        "price_challenge_threshold": threshold,
        "competitive_cutoff": round(cutoff, 2),
    }


def _source_selection_mode(config: dict[str, Any]) -> str:
    live = config.get("live_provider_session", {})
    if not isinstance(live, dict):
        live = {}
    mode = str(live.get("source_selection_mode", "ai_led")).strip().lower()
    if mode != "ai_led":
        raise LiveProviderError("live_provider_session.source_selection_mode must be ai_led")
    return mode


def _ledger_event(
    ledger: dict[str, Any], event_type: str, search_round: int, **details: Any
) -> None:
    events = ledger.setdefault("events", [])
    next_sequence = max(
        (
            int(event.get("event_sequence", 0))
            for event in events
            if isinstance(event, dict)
        ),
        default=0,
    ) + 1
    events.append(
        {
            "event_sequence": next_sequence,
            "event_type": event_type,
            "search_round": int(search_round),
            **copy.deepcopy(details),
        }
    )


def _refresh_budget_ledger(ledger: dict[str, Any]) -> None:
    attempted = sorted(set(ledger.get("attempted_task_ids", [])))
    attempt_ids = list(dict.fromkeys(ledger.get("attempted_attempt_ids", [])))
    if not attempt_ids:
        attempt_ids = [
            str(event.get("bundle_id") or f"legacy-task:{event.get('task_id')}")
            for event in ledger.get("events", [])
            if event.get("event_type") == "attempt_charged" and event.get("task_id")
        ]
    if not attempt_ids:
        attempt_ids = [f"legacy-task:{task_id}" for task_id in attempted]
    terminal = sorted(set(ledger.get("terminal_task_ids", attempted)))
    ledger["attempted_task_ids"] = attempted
    ledger["attempted_attempt_ids"] = attempt_ids
    ledger["attempted_count"] = len(attempt_ids)
    ledger["terminal_task_ids"] = terminal
    ledger["terminal_count"] = len(terminal)
    ledger["remaining"] = (
        None
        if ledger.get("limit") is None
        else max(0, int(ledger["limit"]) - len(attempt_ids))
    )


def _initial_research_credits(config: dict[str, Any]) -> dict[str, Any]:
    policy = search_effort_policy(config.get("search_effort"))
    budget = int(policy["research_credit_budget"])
    return {
        "research_credit_ledger_version": "1.0",
        **policy,
        "used": 0,
        "reserved": 0,
        "remaining": budget,
        "entries": {},
        "stop_reason": None,
    }


def research_credit_cost(task: dict[str, Any]) -> int:
    explicit = task.get("research_credit_cost")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return max(0, explicit)
    request_type = str(task.get("request_type") or "")
    if request_type in {
        "conventional_reference",
        "direct_closure",
        "refresh_quote",
    }:
        return 2
    if request_type == "internal_surface_transfer":
        return 1
    return 1


def reserve_research_credit(
    raw_state: Any,
    *,
    item_id: str,
    task_id: str,
    cost: int,
) -> dict[str, Any]:
    state = validate_session(raw_state)
    ledger = state["research_credits"]
    if item_id in ledger["entries"]:
        return compact_session(state)
    if cost < 0 or cost > int(ledger["remaining"]):
        raise LiveProviderError("Research Credit budget is exhausted")
    ledger["entries"][item_id] = {
        "item_id": item_id,
        "task_id": task_id,
        "cost": int(cost),
        "status": "reserved" if cost else "committed",
        "reserved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if cost:
        ledger["reserved"] = int(ledger["reserved"]) + cost
        ledger["remaining"] = int(ledger["research_credit_budget"]) - int(
            ledger["used"]
        ) - int(ledger["reserved"])
    return compact_session(state)


def record_research_receipt(
    raw_state: Any,
    *,
    item_id: str,
    execution_origin: str | None,
    completed: bool,
) -> dict[str, Any]:
    state = validate_session(raw_state)
    ledger = state["research_credits"]
    entry = ledger["entries"].get(item_id)
    if not isinstance(entry, dict) or entry.get("status") != "reserved":
        return compact_session(state)
    cost = int(entry.get("cost", 0))
    if execution_origin == "provider_query":
        entry["status"] = "committed"
        ledger["used"] = int(ledger["used"]) + cost
    else:
        entry["status"] = "released"
    ledger["reserved"] = max(0, int(ledger["reserved"]) - cost)
    ledger["remaining"] = int(ledger["research_credit_budget"]) - int(
        ledger["used"]
    ) - int(ledger["reserved"])
    entry["execution_origin"] = execution_origin
    entry["completed"] = bool(completed)
    entry["decided_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    return compact_session(state)


def _budget_remaining(task: dict[str, Any]) -> float | None:
    value = task.get("budget_context", {}).get("remaining_budget")
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _supersede_tighter_generations(
    tasks: list[dict[str, Any]], new_task: dict[str, Any], ledger: dict[str, Any]
) -> None:
    new_remaining = _budget_remaining(new_task)
    if new_remaining is None:
        return
    superseded: list[str] = []
    for old in tasks:
        if (
            old.get("task_signature") != new_task["task_signature"]
            or old.get("task_id") == new_task["task_id"]
            or old.get("status") not in {"candidate", "pending"}
        ):
            continue
        old_remaining = _budget_remaining(old)
        if old_remaining is None or new_remaining >= old_remaining:
            continue
        old["status"] = "superseded"
        old["superseded_by_task_id"] = new_task["task_id"]
        old["superseded_round"] = int(new_task.get("candidate_round", 0))
        old["superseded_reason"] = "tighter_remaining_budget_generation"
        superseded.append(old["task_id"])
    if superseded:
        _ledger_event(
            ledger,
            "tasks_superseded",
            int(new_task.get("candidate_round", 0)),
            task_ids=superseded,
            superseded_by_task_id=new_task["task_id"],
        )


def _invalidate_finalization(state: dict[str, Any], reason: str) -> None:
    if state.get("finalization_status") == "final":
        state["finalization_invalidated_reason"] = reason
    state["finalization_status"] = "open"
    canonical = state.get("finalization")
    if isinstance(canonical, dict):
        canonical.pop("evaluated_at", None)
    state["stop_assessment"] = None
    state["no_result_stop_assessment"] = None


def _hypothesis_outcome_covers_task(
    outcome: Any, task: dict[str, Any]
) -> bool:
    if not isinstance(outcome, dict) or outcome.get("status") not in HYPOTHESIS_TERMINAL_STATUSES:
        return False
    covered = outcome.get("covered_task_ids")
    if not isinstance(covered, list):
        # Compatibility for sessions that recorded a terminal hypothesis before
        # group-scoped task ids were introduced.
        return True
    return str(task.get("task_id")) in {str(task_id) for task_id in covered}


def _sync_hypothesis_tree(state: dict[str, Any]) -> dict[str, Any]:
    """Build an ordered tree of route hypotheses, never airport nodes.

    Initial materially different route hypotheses are siblings below a virtual
    one-way-search root.  A caller may explicitly attach a refinement below an
    existing hypothesis.  The order is deterministic advice; the AI may select
    any actionable hypothesis and its reason is kept in the history.
    """
    previous = state.get("hypothesis_tree")
    if not isinstance(previous, dict):
        previous = {}
    previous_nodes = previous.get("nodes", {})
    if not isinstance(previous_nodes, dict):
        previous_nodes = {}
    tasks = [
        task
        for task in state.get("research_tasks", [])
        if isinstance(task, dict) and task.get("hypothesis_id")
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        grouped.setdefault(str(task["hypothesis_id"]), []).append(task)
    selected_counts: dict[str, int] = {}
    for event in state.get("hypothesis_history", []):
        if not isinstance(event, dict) or event.get("action") != "select":
            continue
        hypothesis_id = event.get("hypothesis_id")
        if hypothesis_id:
            key = str(hypothesis_id)
            selected_counts[key] = selected_counts.get(key, 0) + 1
    active = state.get("active_hypothesis")
    active_id = (
        str(active.get("hypothesis_id"))
        if isinstance(active, dict) and active.get("hypothesis_id")
        else None
    )
    outcomes = state.get("hypothesis_outcomes", {})
    if not isinstance(outcomes, dict):
        outcomes = {}
    nodes: dict[str, dict[str, Any]] = {
        HYPOTHESIS_ROOT_ID: {
            "hypothesis_id": HYPOTHESIS_ROOT_ID,
            "parent_hypothesis_id": None,
            "child_hypothesis_ids": [],
            "sibling_order": 0,
            "status": "root",
            "task_ids": [],
            "actionable_task_ids": [],
            "pending_task_ids": [],
            "next_task_id": None,
        }
    }
    for hypothesis_id, hypothesis_tasks in grouped.items():
        hypothesis_tasks.sort(
            key=lambda item: (int(item.get("sequence", 0)), str(item.get("task_id")))
        )
        old_node = previous_nodes.get(hypothesis_id, {})
        if not isinstance(old_node, dict):
            old_node = {}
        declared_parents = [
            str(task.get("parent_hypothesis_id"))
            for task in hypothesis_tasks
            if task.get("parent_hypothesis_id")
            and str(task.get("parent_hypothesis_id")) != hypothesis_id
        ]
        parent_id = (
            declared_parents[0]
            if declared_parents
            else old_node.get("parent_hypothesis_id") or HYPOTHESIS_ROOT_ID
        )
        actionable = [
            task
            for task in hypothesis_tasks
            if task.get("status") in {"candidate", "pending"}
        ]
        actionable.sort(
            key=lambda item: (
                0 if item.get("status") == "pending" else 1,
                int(item.get("sequence", 0)),
                str(item.get("task_id")),
            )
        )
        outcome = outcomes.get(hypothesis_id)
        outcome_status = (
            str(outcome.get("status")) if isinstance(outcome, dict) else None
        )
        open_actionable = [
            task
            for task in actionable
            if not _hypothesis_outcome_covers_task(outcome, task)
        ]
        if hypothesis_id == active_id:
            status = "active"
        elif outcome_status in HYPOTHESIS_TERMINAL_STATUSES and open_actionable:
            status = "new_work_after_disposition"
        elif outcome_status:
            status = outcome_status
        elif selected_counts.get(hypothesis_id):
            status = "selected_open"
        else:
            status = "unexplored"
        exemplar = hypothesis_tasks[0]
        nodes[hypothesis_id] = {
            "hypothesis_id": hypothesis_id,
            "parent_hypothesis_id": str(parent_id),
            "child_hypothesis_ids": [],
            "sibling_order": int(old_node.get("sibling_order", 0)),
            "status": status,
            "hypothesis_kind": exemplar.get("hypothesis_kind"),
            "route_shape": exemplar.get("route_shape"),
            "geographic_cell": exemplar.get("geographic_cell"),
            "anchor_edge_ids": copy.deepcopy(exemplar.get("anchor_edge_ids", [])),
            "first_task_sequence": int(exemplar.get("sequence", 0)),
            "task_ids": [str(task.get("task_id")) for task in hypothesis_tasks],
            "actionable_task_ids": [
                str(task.get("task_id")) for task in open_actionable
            ],
            "pending_task_ids": [
                str(task.get("task_id"))
                for task in open_actionable
                if task.get("status") == "pending"
            ],
            "next_task_id": (
                str(open_actionable[0].get("task_id"))
                if open_actionable
                else None
            ),
            "selected_count": selected_counts.get(hypothesis_id, 0),
        }
    # Unknown or cyclic parents fall back to the virtual root.  This validation
    # records structure only; it never removes a route option.
    for hypothesis_id, node in list(nodes.items()):
        if hypothesis_id == HYPOTHESIS_ROOT_ID:
            continue
        parent_id = str(node.get("parent_hypothesis_id") or HYPOTHESIS_ROOT_ID)
        if parent_id not in nodes or parent_id == hypothesis_id:
            node["parent_hypothesis_id"] = HYPOTHESIS_ROOT_ID
            continue
        seen = {hypothesis_id}
        cursor = parent_id
        cyclic = False
        while cursor != HYPOTHESIS_ROOT_ID:
            if cursor in seen or cursor not in nodes:
                cyclic = True
                break
            seen.add(cursor)
            cursor = str(
                nodes[cursor].get("parent_hypothesis_id") or HYPOTHESIS_ROOT_ID
            )
        if cyclic:
            node["parent_hypothesis_id"] = HYPOTHESIS_ROOT_ID
    children: dict[str, list[str]] = {hypothesis_id: [] for hypothesis_id in nodes}
    for hypothesis_id, node in nodes.items():
        if hypothesis_id == HYPOTHESIS_ROOT_ID:
            continue
        children[str(node["parent_hypothesis_id"])].append(hypothesis_id)
    for parent_id, child_ids in children.items():
        child_ids.sort(
            key=lambda child_id: (
                int(nodes[child_id].get("sibling_order", 0))
                if int(nodes[child_id].get("sibling_order", 0)) > 0
                else 10**9,
                int(nodes[child_id].get("first_task_sequence", 0)),
                child_id,
            )
        )
        nodes[parent_id]["child_hypothesis_ids"] = list(child_ids)
        for order, child_id in enumerate(child_ids, 1):
            nodes[child_id]["sibling_order"] = order

    def path_to(hypothesis_id: str | None) -> list[str]:
        if not hypothesis_id or hypothesis_id not in nodes:
            return [HYPOTHESIS_ROOT_ID]
        path = [hypothesis_id]
        cursor = hypothesis_id
        while cursor != HYPOTHESIS_ROOT_ID:
            cursor = str(
                nodes[cursor].get("parent_hypothesis_id") or HYPOTHESIS_ROOT_ID
            )
            path.append(cursor)
        return list(reversed(path))

    ordered_hypotheses: list[str] = []

    def walk(parent_id: str) -> None:
        for child_id in nodes[parent_id]["child_hypothesis_ids"]:
            ordered_hypotheses.append(child_id)
            walk(child_id)

    walk(HYPOTHESIS_ROOT_ID)
    next_candidate_id = None
    if active_id in nodes and nodes[active_id]["status"] == "active":
        next_candidate_id = active_id
    else:
        next_candidate_id = next(
            (
                hypothesis_id
                for hypothesis_id in ordered_hypotheses
                if nodes[hypothesis_id]["status"]
                not in HYPOTHESIS_TERMINAL_STATUSES
                and (
                    nodes[hypothesis_id]["actionable_task_ids"]
                    or nodes[hypothesis_id]["status"] == "selected_open"
                )
            ),
            None,
        )
    tree = {
        "tree_version": "1.0",
        "root_hypothesis_id": HYPOTHESIS_ROOT_ID,
        "nodes": nodes,
        "active_path": path_to(active_id),
        "next_candidate_id": next_candidate_id,
        "backtrack_count": int(previous.get("backtrack_count", 0)),
        "backtrack_history": copy.deepcopy(previous.get("backtrack_history", [])),
        "ordering_rule": (
            "deterministic registration order is advisory; AI selection remains authoritative"
        ),
    }
    state["hypothesis_tree"] = tree
    if isinstance(state.get("active_hypothesis"), dict):
        state["active_hypothesis"]["tree_path"] = copy.deepcopy(tree["active_path"])
        state["active_hypothesis"]["next_task_id"] = nodes.get(
            active_id, {}
        ).get("next_task_id")
    return tree


def _record_backtrack(
    state: dict[str, Any],
    from_hypothesis_id: str,
    reason: str,
    *,
    to_hypothesis_id: str | None = None,
) -> None:
    tree = _sync_hypothesis_tree(state)
    history = tree.setdefault("backtrack_history", [])
    history.append(
        {
            "sequence": len(history) + 1,
            "from_hypothesis_id": from_hypothesis_id,
            "to_hypothesis_id": to_hypothesis_id,
            "reason": reason,
            "search_round": int(state.get("search_round", 0)),
        }
    )
    tree["backtrack_count"] = len(history)


def _activate_hypothesis(
    state: dict[str, Any],
    task: dict[str, Any],
    reason: str,
    *,
    parent_hypothesis_id: str | None = None,
) -> None:
    hypothesis_id = task.get("hypothesis_id")
    if not hypothesis_id:
        return
    hypothesis_id = str(hypothesis_id)
    if parent_hypothesis_id:
        parent_hypothesis_id = str(parent_hypothesis_id)
        if parent_hypothesis_id == hypothesis_id:
            raise LiveProviderError("a hypothesis cannot be its own parent")
        existing_ids = {
            str(item.get("hypothesis_id"))
            for item in state.get("research_tasks", [])
            if isinstance(item, dict) and item.get("hypothesis_id")
        }
        if parent_hypothesis_id not in existing_ids:
            raise LiveProviderError("parent_hypothesis_id must identify a known hypothesis")
        for item in state.get("research_tasks", []):
            if isinstance(item, dict) and str(item.get("hypothesis_id")) == hypothesis_id:
                item["parent_hypothesis_id"] = parent_hypothesis_id
    previous = state.get("active_hypothesis")
    previous_id = (
        str(previous.get("hypothesis_id"))
        if isinstance(previous, dict) and previous.get("hypothesis_id")
        else None
    )
    history = state.setdefault("hypothesis_history", [])
    outcomes = state.setdefault("hypothesis_outcomes", {})
    if hypothesis_id in outcomes:
        outcomes.pop(hypothesis_id, None)
        history.append(
            {
                "sequence": len(history) + 1,
                "action": "reopen",
                "hypothesis_id": hypothesis_id,
                "task_id": task.get("task_id"),
                "reason": reason,
                "search_round": int(state.get("search_round", 0)),
            }
        )
    if previous_id and previous_id != hypothesis_id:
        history.append(
            {
                "sequence": len(history) + 1,
                "action": "switch",
                "hypothesis_id": previous_id,
                "next_hypothesis_id": hypothesis_id,
                "reason": reason,
                "search_round": int(state.get("search_round", 0)),
            }
        )
        tree_before_switch = _sync_hypothesis_tree(state)
        cursor = hypothesis_id
        previous_is_ancestor = False
        while cursor in tree_before_switch["nodes"] and cursor != HYPOTHESIS_ROOT_ID:
            cursor = str(
                tree_before_switch["nodes"][cursor].get("parent_hypothesis_id")
                or HYPOTHESIS_ROOT_ID
            )
            if cursor == previous_id:
                previous_is_ancestor = True
                break
        if not previous_is_ancestor:
            _record_backtrack(
                state,
                previous_id,
                reason,
                to_hypothesis_id=hypothesis_id,
            )
    state["active_hypothesis"] = {
        "hypothesis_id": hypothesis_id,
        "hypothesis_kind": task.get("hypothesis_kind"),
        "parent_hypothesis_id": task.get("parent_hypothesis_id"),
        "route_shape": task.get("route_shape"),
        "geographic_cell": task.get("geographic_cell"),
        "anchor_edge_ids": copy.deepcopy(task.get("anchor_edge_ids", [])),
        "status": "researching",
        "selected_task_id": task.get("task_id"),
        "selected_round": int(state.get("search_round", 0)),
        "reason": reason,
        "available_dispositions": [],
    }
    if previous_id != hypothesis_id:
        history.append(
            {
                "sequence": len(history) + 1,
                "action": "select",
                "hypothesis_id": hypothesis_id,
                "task_id": task.get("task_id"),
                "reason": reason,
                "search_round": int(state.get("search_round", 0)),
            }
        )
    _invalidate_finalization(state, "hypothesis selection changed")
    _sync_hypothesis_tree(state)


def record_hypothesis_decision(
    state: dict[str, Any],
    action: str,
    reason: str,
    *,
    task_id: str | None = None,
    hypothesis_id: str | None = None,
    parent_hypothesis_id: str | None = None,
) -> dict[str, Any]:
    state = validate_session(state)
    action = str(action).strip().lower()
    reason = _text(reason, "hypothesis decision reason")
    if action == "select":
        if not task_id:
            raise LiveProviderError("select requires task_id")
        task = next(
            (item for item in state["research_tasks"] if item["task_id"] == task_id),
            None,
        )
        if task is None:
            raise LiveProviderError("selected task must be AI-actionable")
        if task.get("status") in TERMINAL_STATUSES and isinstance(
            task.get("frontier_context"), dict
        ):
            task = _reopen_terminal_task(
                state,
                task,
                reason,
            )
        if task.get("status") not in {"candidate", "pending"}:
            raise LiveProviderError("selected task must be AI-actionable")
        _activate_hypothesis(
            state,
            task,
            reason,
            parent_hypothesis_id=parent_hypothesis_id,
        )
        return compact_session(state)
    if action not in HYPOTHESIS_ACTIONS - {"select"}:
        raise LiveProviderError(
            "hypothesis action must be select, complete, hard_dead, "
            "or deferred_low_value"
        )
    target = hypothesis_id or (
        state.get("active_hypothesis", {}).get("hypothesis_id")
        if isinstance(state.get("active_hypothesis"), dict)
        else None
    )
    if not target:
        raise LiveProviderError(f"{action} requires hypothesis_id or active hypothesis")
    tree = _sync_hypothesis_tree(state)
    if str(target) not in tree["nodes"]:
        raise LiveProviderError("hypothesis decision requires a known hypothesis_id")
    target = str(target)
    was_selected = bool(
        tree["nodes"].get(target, {}).get("selected_count")
        or (
            isinstance(state.get("active_hypothesis"), dict)
            and str(state["active_hypothesis"].get("hypothesis_id")) == target
        )
    )
    covered_tasks = [
        str(item.get("task_id"))
        for item in state.get("research_tasks", [])
        if isinstance(item, dict) and str(item.get("hypothesis_id")) == target
    ]
    outcomes = state.setdefault("hypothesis_outcomes", {})
    outcomes[target] = {
        "status": action,
        "reason": reason,
        "search_round": int(state.get("search_round", 0)),
        "covered_task_ids": covered_tasks,
        "disposition_scope": "whole_hypothesis",
    }
    history = state.setdefault("hypothesis_history", [])
    history.append(
        {
            "sequence": len(history) + 1,
            "action": action,
            "hypothesis_id": target,
            "reason": reason,
            "search_round": int(state.get("search_round", 0)),
        }
    )
    if (
        isinstance(state.get("active_hypothesis"), dict)
        and state["active_hypothesis"].get("hypothesis_id") == target
    ):
        state["active_hypothesis"] = None
    _invalidate_finalization(state, "hypothesis disposition changed")
    if was_selected:
        _record_backtrack(state, target, f"hypothesis {action}: {reason}")
        tree = _sync_hypothesis_tree(state)
        if tree.get("backtrack_history"):
            tree["backtrack_history"][-1]["to_hypothesis_id"] = tree.get(
                "next_candidate_id"
            )
    else:
        _sync_hypothesis_tree(state)
    return compact_session(state)


def _attempted_source_ids_for_task(
    state: dict[str, Any], task: dict[str, Any]
) -> set[str]:
    task_id = str(task.get("task_id") or "")
    suggestions = [
        item
        for item in task.get("source_suggestions", [])
        if isinstance(item, dict) and item.get("source_id")
    ]
    attempted: set[str] = set()
    for bundle in state.get("evidence_bundles", []):
        if not isinstance(bundle, dict) or str(bundle.get("request_id") or "") != task_id:
            continue
        for observation in bundle.get("observations", []):
            if not isinstance(observation, dict):
                continue
            for source in suggestions:
                if _source_identity_matches(
                    observation.get("provider"), observation.get("url"), source
                ):
                    attempted.add(str(source["source_id"]))
                    continue
                if any(
                    isinstance(provenance, dict)
                    and _source_identity_matches(
                        provenance.get("provider"), provenance.get("url"), source
                    )
                    for provenance in observation.get("discovery_provenance", [])
                ):
                    attempted.add(str(source["source_id"]))
    return attempted


def _reopen_terminal_task(
    state: dict[str, Any],
    task: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    if task.get("status") not in TERMINAL_STATUSES:
        raise LiveProviderError("task reopen requires a terminal research task")
    previous_status = str(task["status"])
    task["status"] = "pending"
    task.pop("terminal_round", None)
    task.pop("terminal_reason", None)
    task["last_reopen"] = {
        "previous_status": previous_status,
        "reason": reason,
        "search_round": int(state.get("search_round", 0)),
        "next_attempt_number": int(task.get("attempt_count", 0)) + 1,
    }
    return task


def reopen_task_for_source_challenger(
    state: dict[str, Any], task_id: str, reason: str
) -> dict[str, Any]:
    """Reopen one terminal task while preserving its prior source attempts."""

    state = validate_session(state)
    reason = _text(reason, "source challenger reason")
    task = next(
        (item for item in state.get("research_tasks", []) if item.get("task_id") == task_id),
        None,
    )
    if task is None:
        raise LiveProviderError("source challenger requires a known task_id")
    if task.get("status") not in TERMINAL_STATUSES:
        raise LiveProviderError("source challenger requires a terminal research task")
    suggestions = task.get("source_suggestions", [])
    if not isinstance(suggestions, list) or not suggestions:
        raise LiveProviderError("source challenger requires source suggestions")
    attempted_source_ids = _attempted_source_ids_for_task(state, task)
    untried_sources = [
        source
        for source in suggestions
        if isinstance(source, dict)
        and source.get("source_id")
        and str(source["source_id"]) not in attempted_source_ids
    ]
    if not untried_sources:
        raise LiveProviderError("source challenger requires an unattempted source")
    task["assigned_source"] = copy.deepcopy(untried_sources[0])
    _reopen_terminal_task(
        state,
        task,
        reason,
    )
    _activate_hypothesis(state, task, reason)
    _invalidate_finalization(state, "source challenger reopened")
    _sync_hypothesis_tree(state)
    return compact_session(state)


def publish_frontier_research_task(
    state: dict[str, Any], frontier_id: str, reason: str
) -> dict[str, Any]:
    """Turn one AI-selected archived frontier into one persisted research task."""

    state = validate_session(state)
    reason = _text(reason, "frontier exploration reason")
    if state.get("active_hypothesis") is not None:
        raise LiveProviderError("frontier exploration requires no active hypothesis")
    view = state.get("frontier_research_view", {})
    action = next(
        (
            item
            for item in view.get("frontier_actions", [])
            if isinstance(item, dict) and item.get("frontier_id") == frontier_id
        ),
        None,
    )
    if action is None:
        raise LiveProviderError("frontier_id is not currently AI-actionable")
    try:
        request = FRONTIER_EXPLORATION.compile_frontier_request(state, frontier_id)
        provider_plan = PROVIDER_SCHEDULER.schedule_provider_requests(
            [request], copy.deepcopy(state["nodes"]), state["config"]
        )
    except (
        FRONTIER_EXPLORATION.FrontierExplorationError,
        PROVIDER_SCHEDULER.ProviderExecutionError,
    ) as exc:
        raise LiveProviderError(f"frontier task compilation failed: {exc}") from exc
    existing_ids = {str(task.get("task_id")) for task in state["research_tasks"]}
    state["research_tasks"] = _merge_tasks(
        state["research_tasks"],
        provider_plan["requests"],
        None,
        int(state.get("search_round", 0)),
    )
    new_tasks = [
        task
        for task in state["research_tasks"]
        if str(task.get("task_id")) not in existing_ids
        and task.get("corridor_context", {}).get("corridor_id")
        == request["corridor_context"]["corridor_id"]
    ]
    if not new_tasks:
        raise LiveProviderError("frontier task compilation produced no new task")
    target = min(
        new_tasks,
        key=lambda task: (int(task.get("sequence", 0)), str(task.get("task_id"))),
    )
    state["provider_coverage_audit"] = _merge_provider_coverage(
        state.get("provider_coverage_audit"),
        provider_plan,
        state["research_tasks"],
        int(state.get("search_round", 0)),
    )
    _activate_hypothesis(state, target, reason)
    state.setdefault("frontier_history", []).append(
        {
            "sequence": len(state.get("frontier_history", [])) + 1,
            "action": "select_frontier",
            "frontier_id": frontier_id,
            "task_id": target["task_id"],
            "reason": reason,
            "search_round": int(state.get("search_round", 0)),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _invalidate_finalization(state, "frontier research selected")
    _sync_hypothesis_tree(state)
    _refresh_frontier_exploration(state)
    return compact_session(state)


def add_research_requirements(
    state: dict[str, Any], raw_requests: list[dict[str, Any]]
) -> dict[str, Any]:
    """Add explicit product-owned research requirements to an initialized Session."""

    state = validate_session(state)
    if not raw_requests:
        return compact_session(state)
    try:
        provider_plan = PROVIDER_SCHEDULER.schedule_provider_requests(
            [_normalized_request(request) for request in raw_requests],
            copy.deepcopy(state["nodes"]),
            state["config"],
        )
    except PROVIDER_SCHEDULER.ProviderExecutionError as exc:
        raise LiveProviderError(
            f"explicit research requirement planning failed: {exc}"
        ) from exc
    state["research_tasks"] = _merge_tasks(
        state["research_tasks"],
        provider_plan["requests"],
        None,
        int(state.get("search_round", 0)),
    )
    state["provider_coverage_audit"] = _merge_provider_coverage(
        state.get("provider_coverage_audit"),
        provider_plan,
        state["research_tasks"],
        int(state.get("search_round", 0)),
    )
    _invalidate_finalization(state, "explicit research requirements added")
    _sync_hypothesis_tree(state)
    _refresh_frontier_exploration(state)
    return compact_session(state)


def _update_active_hypothesis_after_ingest(
    state: dict[str, Any],
    task: dict[str, Any],
    admitted_edge_ids: list[str],
) -> None:
    active = state.get("active_hypothesis")
    if not isinstance(active, dict) or active.get("hypothesis_id") != task.get(
        "hypothesis_id"
    ):
        return
    active["last_task_id"] = task.get("task_id")
    active["last_task_status"] = task.get("status")
    active["last_round"] = int(state.get("search_round", 0))
    active["last_admitted_edge_ids"] = list(admitted_edge_ids)
    admitted = set(admitted_edge_ids)
    supporting_routes = [
        route.get("id") or route.get("route_id")
        for route in state.get("result", {}).get("routes", [])
        if admitted and admitted.intersection(route.get("edge_ids", []))
    ]
    actionable = [
        item
        for item in state.get("research_tasks", [])
        if item.get("hypothesis_id") == active.get("hypothesis_id")
        and item.get("status") in {"candidate", "pending"}
    ]
    if supporting_routes:
        active["status"] = "complete_candidate_found"
        active["supporting_route_ids"] = supporting_routes
        active["available_dispositions"] = ["complete", "deferred_low_value"]
    elif task.get("status") in {"no_result", "rejected"} and not actionable:
        active["status"] = "awaiting_ai_hard_dead_decision"
        active["available_dispositions"] = ["hard_dead", "deferred_low_value"]
    else:
        active["status"] = "researching"
        active["available_dispositions"] = []
    active["actionable_followup_count"] = len(actionable)
    _sync_hypothesis_tree(state)


def _merge_tasks(
    old_tasks: list[dict[str, Any]],
    provider_requests: list[dict[str, Any]],
    budget_ledger: dict[str, Any] | None = None,
    search_round: int = 0,
) -> list[dict[str, Any]]:
    """Append immutable task candidates without spending or publishing budget."""
    tasks = [copy.deepcopy(task) for task in old_tasks]
    ledger = budget_ledger if budget_ledger is not None else {
        "limit": len(tasks) + len(provider_requests),
        "attempted_task_ids": [
            task.get("task_id")
            for task in tasks
            if task.get("status") in TERMINAL_STATUSES
        ],
        "terminal_task_ids": [
            task.get("task_id")
            for task in tasks
            if task.get("status") in TERMINAL_STATUSES
        ],
        "events": [],
    }
    _refresh_budget_ledger(ledger)
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        signature = task.get("task_signature")
        if signature:
            by_signature.setdefault(signature, []).append(task)
    next_sequence = max((int(task.get("sequence", 0)) for task in tasks), default=0)
    for raw_request in provider_requests:
        request = _normalized_request(raw_request)
        signature = _canonical_task_signature(request)
        revision = _task_revision(request, signature)
        same = by_signature.get(signature, [])
        if any(task.get("task_revision") == revision for task in same):
            continue
        generation = max(
            (int(task.get("generation", 0)) for task in same), default=0
        ) + 1
        next_sequence += 1
        task = _research_task(request, next_sequence, generation)
        task["status"] = "candidate"
        task["candidate_round"] = int(search_round)
        tasks.append(task)
        _supersede_tighter_generations(tasks, task, ledger)
        by_signature.setdefault(signature, []).append(task)
    _refresh_budget_ledger(ledger)
    return sorted(tasks, key=lambda item: (int(item.get("sequence", 0)), item["task_id"]))


def _requests_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    provider_requests = copy.deepcopy(result.get("provider_requests", []))
    # Current engines expose one unified list.  The legacy initial-discovery
    # fallback is used only when that list is absent; adding both would publish
    # the same market question twice once corridor/date context is attached.
    requests = provider_requests or copy.deepcopy(
        result.get("initial_discovery_requests", [])
    )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for request in requests:
        normalized = _normalized_request(request)
        key = _canonical_task_signature(normalized)
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return unique


def _provider_plan_from_result(
    result: dict[str, Any], nodes: dict[str, dict[str, Any]] | list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    node_list = _as_node_list(nodes) if isinstance(nodes, dict) else copy.deepcopy(nodes)
    try:
        return PROVIDER_SCHEDULER.schedule_provider_requests(
            _requests_from_result(result), node_list, config
        )
    except PROVIDER_SCHEDULER.ProviderExecutionError as exc:
        raise LiveProviderError(f"Provider execution planning failed: {exc}") from exc


def _merge_provider_coverage(
    old: dict[str, Any] | None,
    current: dict[str, Any],
    tasks: list[dict[str, Any]],
    search_round: int,
) -> dict[str, Any]:
    previous_entries = (
        old.get("coverage", []) if isinstance(old, dict) else []
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in previous_entries:
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("parent_request_key")), str(entry.get("source_id")))
        merged[key] = copy.deepcopy(entry)
    for entry in current.get("coverage", []):
        key = (str(entry.get("parent_request_key")), str(entry.get("source_id")))
        value = copy.deepcopy(entry)
        value["first_seen_round"] = int(
            merged.get(key, {}).get("first_seen_round", search_round)
        )
        value["last_seen_round"] = int(search_round)
        merged[key] = value
    task_by_request = {
        str(task.get("original_request_id")): task
        for task in tasks
        if task.get("original_request_id")
    }
    for entry in merged.values():
        provider_request_id = entry.get("provider_request_id")
        task = task_by_request.get(str(provider_request_id))
        if task is not None:
            entry["task_id"] = task.get("task_id")
            entry["task_status"] = task.get("status")
            entry["task_generation"] = task.get("generation")
    counts: dict[str, int] = {}
    task_counts: dict[str, int] = {}
    for entry in merged.values():
        disposition = str(entry.get("disposition", "unknown"))
        counts[disposition] = counts.get(disposition, 0) + 1
        if entry.get("task_status"):
            status = str(entry["task_status"])
            task_counts[status] = task_counts.get(status, 0) + 1
    return {
        "scheduler_version": current.get("scheduler_version"),
        "registry_schema_version": current.get("registry_schema_version"),
        "registry_last_verified": current.get("registry_last_verified"),
        "registry_is_whitelist": current.get("registry_is_whitelist", False),
        "static_source_count": current.get("static_source_count", 0),
        "dynamic_rule_count": current.get("dynamic_rule_count", 0),
        "parent_request_count_current": current.get("parent_request_count", 0),
        "scheduled_request_count_current": current.get("scheduled_request_count", 0),
        "disposition_counts": counts,
        "task_status_counts": task_counts,
        "coverage": sorted(
            merged.values(),
            key=lambda item: (
                str(item.get("parent_request_key", "")),
                str(item.get("source_id", "")),
            ),
        ),
    }


def initialize_session(config_path: str | Path) -> dict[str, Any]:
    config, profile, nodes, edges = ENGINE.load_bundle(config_path)
    result = ENGINE.solve(config, profile, nodes, edges)
    research_credits = _initial_research_credits(config)
    provider_plan = _provider_plan_from_result(result, nodes, config)
    tasks = _merge_tasks([], provider_plan["requests"], None, 0)
    provider_coverage = _merge_provider_coverage(None, provider_plan, tasks, 0)
    active_incumbent = copy.deepcopy(result.get("active_incumbent"))
    session = {
        "session_version": SESSION_VERSION,
        "session_id": _session_id(config, profile),
        "trip_scope": TRIP_SCOPE,
        "engine_version": ENGINE.ENGINE_VERSION,
        "config_source": _display_path(config_path),
        "config": config,
        "profile": profile,
        "nodes": _as_node_list(nodes),
        "edges": copy.deepcopy(edges),
        "internal_surface_transfers": [],
        "research_tasks": tasks,
        "provider_coverage_audit": provider_coverage,
        "research_credits": research_credits,
        "search_round": 0,
        "active_incumbent": active_incumbent,
        "incumbent_history": [active_incumbent] if active_incumbent else [],
        "active_hypothesis": None,
        "hypothesis_history": [],
        "hypothesis_outcomes": {},
        "hypothesis_tree": {},
        "finalization_status": "open",
        "finalization_history": [],
        "stop_assessment": None,
        "no_result_stop_assessment": None,
        "frontier_history": [],
        "frontier_control_version": FRONTIER_CONTROL_VERSION,
        "evidence_bundles": [],
        "result": result,
    }
    _sync_hypothesis_tree(session)
    _refresh_frontier_exploration(session)
    return compact_session(session)


def _upgrade_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Add scheduler metadata without invalidating an issued v0.6 task id."""
    state = copy.deepcopy(raw)
    _restore_research_questions(state)
    _restore_finalization_view(state)
    _restore_hypothesis_views(state)
    _restore_research_question_views(state)
    config = state.get("config", {})
    tasks = state.get("research_tasks", [])
    bundle_ids_by_task: dict[str, list[str]] = {}
    for bundle in state.get("evidence_bundles", []):
        if not isinstance(bundle, dict):
            continue
        task_id = str(bundle.get("request_id") or "")
        bundle_id = str(bundle.get("bundle_id") or "")
        if task_id and bundle_id:
            values = bundle_ids_by_task.setdefault(task_id, [])
            if bundle_id not in values:
                values.append(bundle_id)
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        request = copy.deepcopy(task)
        try:
            normalized = _normalized_request(request)
        except LiveProviderError:
            continue
        signature = task.setdefault(
            "task_signature", _canonical_task_signature(normalized)
        )
        task.setdefault("generation", 1)
        task.setdefault("budget_context", {})
        task.setdefault("consumer_contexts", [])
        task.setdefault("endpoint_policy", normalized["endpoint_policy"])
        task.setdefault("origins", normalized["origins"])
        task.setdefault("destinations", normalized["destinations"])
        task.setdefault("evidence_contract_version", state.get("session_version", SESSION_VERSION))
        task.setdefault("task_revision", _task_revision(normalized, signature))
        task.setdefault("candidate_round", 0)
        task_id = str(task.get("task_id") or "")
        known_bundle_ids = bundle_ids_by_task.get(task_id, [])
        raw_bundle_ids = task.get("attempt_bundle_ids", [])
        task_bundle_ids = (
            [str(value) for value in raw_bundle_ids if value]
            if isinstance(raw_bundle_ids, list)
            else []
        )
        for bundle_id in known_bundle_ids:
            if bundle_id not in task_bundle_ids:
                task_bundle_ids.append(bundle_id)
        task["attempt_bundle_ids"] = task_bundle_ids
        raw_attempt_count = task.get("attempt_count", 0)
        if isinstance(raw_attempt_count, bool) or not isinstance(raw_attempt_count, int):
            raw_attempt_count = 0
        task["attempt_count"] = max(raw_attempt_count, len(task_bundle_ids))
        task.setdefault("research_credit_cost", research_credit_cost(task))
    credits = state.get("research_credits")
    if not isinstance(credits, dict):
        credits = _initial_research_credits(config)
        legacy_ledger = state.get("budget_ledger")
        legacy_attempt_ids = list(
            dict.fromkeys(
                [
                    bundle_id
                    for bundle_ids in bundle_ids_by_task.values()
                    for bundle_id in bundle_ids
                ]
                or (
                    legacy_ledger.get("attempted_attempt_ids", [])
                    if isinstance(legacy_ledger, dict)
                    else []
                )
            )
        )
        for attempt_id in legacy_attempt_ids:
            credits["entries"][f"legacy:{attempt_id}"] = {
                "item_id": f"legacy:{attempt_id}",
                "task_id": None,
                "cost": 1,
                "status": "committed",
                "execution_origin": "provider_query",
                "legacy_attempt_id": attempt_id,
            }
        credits["used"] = len(legacy_attempt_ids)
        if credits["used"] > credits["research_credit_budget"]:
            credits["research_credit_budget"] = credits["used"]
        credits["remaining"] = (
            int(credits["research_credit_budget"]) - int(credits["used"])
        )
        if isinstance(legacy_ledger, dict):
            credits["legacy_query_budget"] = copy.deepcopy(legacy_ledger)
        state["research_credits"] = credits
    else:
        defaults = _initial_research_credits(config)
        for key, value in defaults.items():
            credits.setdefault(key, copy.deepcopy(value))
    state.pop("budget_ledger", None)
    state.setdefault("search_round", 0)
    state.setdefault("active_incumbent", copy.deepcopy(state.get("result", {}).get("active_incumbent")))
    state.setdefault(
        "incumbent_history",
        [copy.deepcopy(state["active_incumbent"])] if state.get("active_incumbent") else [],
    )
    state.setdefault("active_hypothesis", None)
    state.setdefault("hypothesis_history", [])
    state.setdefault("hypothesis_outcomes", {})
    state.setdefault("hypothesis_tree", {})
    state.setdefault("finalization_status", "open")
    state.setdefault("finalization_history", [])
    state.setdefault("stop_assessment", None)
    state.setdefault("no_result_stop_assessment", None)
    state.setdefault("frontier_history", [])
    state.setdefault("internal_surface_transfers", [])
    if state.get("frontier_control_version") != FRONTIER_CONTROL_VERSION:
        if state.get("finalization_status") != "final":
            migrated_ids: list[str] = []
            contexts = {
                "origin_market_scan": {
                    "hypothesis_id": "market-discovery-origin",
                    "hypothesis_kind": "origin_market_discovery",
                    "route_shape": "origin_pool_to_open_destination",
                    "geographic_cell": "ORIGIN_MARKET",
                },
                "destination_market_scan": {
                    "hypothesis_id": "market-discovery-destination",
                    "hypothesis_kind": "destination_market_discovery",
                    "route_shape": "open_origin_to_destination_pool",
                    "geographic_cell": "DESTINATION_MARKET",
                },
            }
            for task in tasks if isinstance(tasks, list) else []:
                if not isinstance(task, dict):
                    continue
                context = contexts.get(str(task.get("request_type")))
                if context is None:
                    continue
                was_baseline = task.get("hypothesis_id") == "conventional-baseline"
                if was_baseline:
                    task.update(copy.deepcopy(context))
                date_search = task.get("date_search")
                if isinstance(date_search, dict):
                    date_search["frontier_role"] = context["hypothesis_kind"]
                corridor = task.get("corridor_context")
                if isinstance(corridor, dict):
                    corridor.update(
                        {
                            "corridor_id": context["hypothesis_id"],
                            "corridor_kind": context["hypothesis_kind"],
                            "route_shape": context["route_shape"],
                            "geographic_cell": context["geographic_cell"],
                        }
                    )
                try:
                    refreshed_plan = PROVIDER_SCHEDULER.schedule_provider_requests(
                        [copy.deepcopy(task)],
                        copy.deepcopy(state.get("nodes", [])),
                        config,
                    )
                except PROVIDER_SCHEDULER.ProviderExecutionError:
                    refreshed_plan = {"requests": []}
                if refreshed_plan.get("requests"):
                    refreshed = refreshed_plan["requests"][0]
                    task["source_suggestions"] = copy.deepcopy(
                        refreshed.get("source_suggestions", [])
                    )
                    task["selection_advice"] = copy.deepcopy(
                        refreshed.get("selection_advice", task.get("selection_advice"))
                    )
                if was_baseline:
                    migrated_ids.append(str(task.get("task_id")))
            baseline_outcome = state.get("hypothesis_outcomes", {}).get(
                "conventional-baseline"
            )
            if isinstance(baseline_outcome, dict) and migrated_ids:
                baseline_outcome["covered_task_ids"] = [
                    task_id
                    for task_id in baseline_outcome.get("covered_task_ids", [])
                    if str(task_id) not in set(migrated_ids)
                ]
            if migrated_ids:
                state["stop_assessment"] = None
                state["no_result_stop_assessment"] = None
        state["frontier_control_version"] = FRONTIER_CONTROL_VERSION
    try:
        provider_plan = _provider_plan_from_result(
            state.get("result", {}),
            state.get("nodes", []),
            state.get("config", {}),
        )
        state["provider_coverage_audit"] = _merge_provider_coverage(
            None,
            provider_plan,
            state.get("research_tasks", []),
            int(state.get("search_round", 0)),
        )
    except LiveProviderError:
        if not isinstance(state.get("provider_coverage_audit"), dict):
            state["provider_coverage_audit"] = {}
    _sync_hypothesis_tree(state)
    _refresh_frontier_exploration(state)
    return state


def validate_session(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LiveProviderError("state must be an object")
    state = _upgrade_session(raw)
    if state.get("session_version") != SESSION_VERSION:
        raise LiveProviderError(f"state.session_version must be {SESSION_VERSION}")
    if state.get("trip_scope") != TRIP_SCOPE:
        raise LiveProviderError(f"state.trip_scope must be {TRIP_SCOPE}")
    _text(state.get("session_id"), "state.session_id")
    for field in ("config", "profile", "result"):
        if not isinstance(state.get(field), dict):
            raise LiveProviderError(f"state.{field} must be an object")
    if not isinstance(state.get("nodes"), list) or not state["nodes"]:
        raise LiveProviderError("state.nodes must be a non-empty array")
    if not isinstance(state.get("edges"), list):
        raise LiveProviderError("state.edges must be an array")
    if not isinstance(state.get("research_tasks"), list):
        raise LiveProviderError("state.research_tasks must be an array")
    for index, task in enumerate(state["research_tasks"]):
        if not isinstance(task, dict) or task.get("status") not in TASK_STATUSES:
            raise LiveProviderError(f"state.research_tasks[{index}] has an invalid status")
        attempt_count = task.get("attempt_count")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise LiveProviderError(
                f"state.research_tasks[{index}].attempt_count must be non-negative"
            )
        attempt_bundle_ids = task.get("attempt_bundle_ids")
        if not isinstance(attempt_bundle_ids, list) or not all(
            isinstance(value, str) and value for value in attempt_bundle_ids
        ):
            raise LiveProviderError(
                f"state.research_tasks[{index}].attempt_bundle_ids must be a string array"
            )
    if not isinstance(state.get("evidence_bundles"), list):
        raise LiveProviderError("state.evidence_bundles must be an array")
    if not isinstance(state.get("research_credits"), dict):
        raise LiveProviderError("state.research_credits must be an object")
    if state.get("active_hypothesis") is not None and not isinstance(
        state.get("active_hypothesis"), dict
    ):
        raise LiveProviderError("state.active_hypothesis must be an object or null")
    if not isinstance(state.get("hypothesis_history"), list):
        raise LiveProviderError("state.hypothesis_history must be an array")
    if not isinstance(state.get("hypothesis_outcomes"), dict):
        raise LiveProviderError("state.hypothesis_outcomes must be an object")
    if not isinstance(state.get("hypothesis_tree"), dict):
        raise LiveProviderError("state.hypothesis_tree must be an object")
    if state.get("finalization_status") not in {"open", "final"}:
        raise LiveProviderError("state.finalization_status must be open or final")
    if not isinstance(state.get("finalization_history"), list):
        raise LiveProviderError("state.finalization_history must be an array")
    evaluated_at = finalization_evaluated_at(state)
    if evaluated_at is not None and state.get("finalization_status") != "final":
        raise LiveProviderError(
            "state.finalization.evaluated_at requires final status"
        )
    if state.get("stop_assessment") is not None and not isinstance(
        state.get("stop_assessment"), dict
    ):
        raise LiveProviderError("state.stop_assessment must be an object or null")
    return state


def _normalize_common_observation(raw: Any, index: int) -> dict[str, Any]:
    field = f"observations[{index}]"
    if not isinstance(raw, dict):
        raise LiveProviderError(f"{field} must be an object")
    status = _text(raw.get("evidence_status"), f"{field}.evidence_status").lower()
    if status not in TERMINAL_STATUSES:
        raise LiveProviderError(
            f"{field}.evidence_status must be verified_observed, no_result, or rejected"
        )
    observed_at = _datetime(raw.get("observed_at"), f"{field}.observed_at")
    normalized = {
        "observation_id": _text(raw.get("observation_id"), f"{field}.observation_id"),
        "evidence_status": status,
        "provider": _text(raw.get("provider"), f"{field}.provider"),
        "url": _http_url(raw.get("url"), f"{field}.url"),
        "observed_at": _iso(observed_at),
    }
    if raw.get("notes") is not None:
        if not isinstance(raw["notes"], list) or not all(
            isinstance(item, str) for item in raw["notes"]
        ):
            raise LiveProviderError(f"{field}.notes must be a string array")
        normalized["notes"] = list(raw["notes"])
    if raw.get("discovery_provenance") is not None:
        if status != VERIFIED_STATUS:
            raise LiveProviderError(
                f"{field}.discovery_provenance is only valid for verified_observed"
            )
        provenance = raw["discovery_provenance"]
        if not isinstance(provenance, list) or not provenance:
            raise LiveProviderError(
                f"{field}.discovery_provenance must be a non-empty array"
            )
        normalized_provenance: list[dict[str, Any]] = []
        allowed_purposes = {
            "itinerary_and_fare_rule_discovery",
            "route_discovery",
            "calendar_anchor",
            "price_indication",
        }
        for provenance_index, item in enumerate(provenance):
            item_field = f"{field}.discovery_provenance[{provenance_index}]"
            if not isinstance(item, dict):
                raise LiveProviderError(f"{item_field} must be an object")
            purpose = _text(item.get("purpose"), f"{item_field}.purpose").lower()
            if purpose not in allowed_purposes:
                raise LiveProviderError(f"{item_field}.purpose is unsupported")
            discovery_status = _text(
                item.get("admission_status"), f"{item_field}.admission_status"
            ).lower()
            if discovery_status != "research_only":
                raise LiveProviderError(
                    f"{item_field}.admission_status must be research_only"
                )
            entry: dict[str, Any] = {
                "provider": _text(item.get("provider"), f"{item_field}.provider"),
                "url": _http_url(item.get("url"), f"{item_field}.url"),
                "observed_at": _iso(
                    _datetime(item.get("observed_at"), f"{item_field}.observed_at")
                ),
                "purpose": purpose,
                "admission_status": "research_only",
            }
            if item.get("displayed_price") is not None:
                displayed = item["displayed_price"]
                if not isinstance(displayed, dict):
                    raise LiveProviderError(
                        f"{item_field}.displayed_price must be an object"
                    )
                entry["displayed_price"] = {
                    "amount": _number(
                        displayed.get("amount"),
                        f"{item_field}.displayed_price.amount",
                    ),
                    "currency": _currency(
                        displayed.get("currency"),
                        f"{item_field}.displayed_price.currency",
                    ),
                }
            if item.get("notes") is not None:
                notes = item["notes"]
                if not isinstance(notes, list) or not all(
                    isinstance(note, str) for note in notes
                ):
                    raise LiveProviderError(f"{item_field}.notes must be a string array")
                entry["notes"] = list(notes)
            normalized_provenance.append(entry)
        normalized["discovery_provenance"] = normalized_provenance
    if status != VERIFIED_STATUS:
        normalized["reason"] = _text(raw.get("reason"), f"{field}.reason")
    return normalized


def _source_identity_matches(
    provider: Any, url: Any, assigned_source: dict[str, Any]
) -> bool:
    provider_key = "".join(
        character
        for character in str(provider or "").casefold()
        if character.isalnum()
    )
    assigned_keys = {
        "".join(character for character in str(value).casefold() if character.isalnum())
        for value in (assigned_source.get("source_id"), assigned_source.get("name"))
    }
    if provider_key and any(
        key and (provider_key == key or provider_key in key or key in provider_key)
        for key in assigned_keys
    ):
        return True
    try:
        observed_host = (urlparse(str(url)).hostname or "").casefold()
        assigned_host = (
            urlparse(str(assigned_source.get("url", ""))).hostname or ""
        ).casefold()
    except ValueError:
        return False
    return bool(
        observed_host
        and assigned_host
        and (
            observed_host == assigned_host
            or observed_host.endswith("." + assigned_host)
            or assigned_host.endswith("." + observed_host)
        )
    )


def _assigned_source_fulfillment(
    task: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    assigned = task.get("assigned_source")
    if not isinstance(assigned, dict):
        return {"status": "not_assigned", "fulfilled": True}
    admission = str(assigned.get("admission_policy", ""))
    direct_or_research = admission == "seller_quote_or_research_then_seller"
    research_required = (
        ("research_then" in admission and not direct_or_research)
        or admission
        in {
            "topology_only",
            "topology_or_schedule_then_operator",
            "schedule_then_operator",
            "route_then_operator",
        }
    )
    direct_matches: list[str] = []
    provenance_matches: list[str] = []
    for observation in observations:
        observation_id = str(observation.get("observation_id", ""))
        if _source_identity_matches(
            observation.get("provider"), observation.get("url"), assigned
        ):
            direct_matches.append(observation_id)
        for provenance in observation.get("discovery_provenance", []):
            if _source_identity_matches(
                provenance.get("provider"), provenance.get("url"), assigned
            ):
                provenance_matches.append(observation_id)
                break
    verified_present = any(
        observation.get("evidence_status") == VERIFIED_STATUS
        for observation in observations
    )
    if verified_present and research_required:
        fulfilled = bool(provenance_matches)
        method = "research_provenance" if fulfilled else "missing_research_provenance"
    else:
        fulfilled = bool(direct_matches or provenance_matches)
        method = (
            "direct_source"
            if direct_matches
            else "research_provenance"
            if provenance_matches
            else "source_not_demonstrated"
        )
    return {
        "status": "fulfilled" if fulfilled else "not_demonstrated",
        "fulfilled": fulfilled,
        "assigned_source_id": assigned.get("source_id"),
        "admission_policy": admission,
        "method": method,
        "direct_match_observation_ids": direct_matches,
        "provenance_match_observation_ids": provenance_matches,
        "enforcement": "audited_not_automatically_rejected",
    }


def _normalize_discovered_nodes(
    raw_nodes: Any, state: dict[str, Any], task: dict[str, Any]
) -> list[dict[str, Any]]:
    if raw_nodes is None:
        return []
    if not isinstance(raw_nodes, list):
        raise LiveProviderError("discovered_nodes must be an array")
    existing = {node["node_id"].upper(): copy.deepcopy(node) for node in state["nodes"]}
    admitted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        field = f"discovered_nodes[{index}]"
        if not isinstance(raw, dict):
            raise LiveProviderError(f"{field} must be an object")
        node_id = _text(raw.get("node_id"), f"{field}.node_id").upper()
        if node_id in seen:
            raise LiveProviderError(f"duplicate discovered node {node_id}")
        seen.add(node_id)
        node = copy.deepcopy(raw)
        node.update(
            {
                "node_id": node_id,
                "node_type": _text(raw.get("node_type"), f"{field}.node_type").lower(),
                "location": _text(raw.get("location"), f"{field}.location"),
                "country_or_region": _text(
                    raw.get("country_or_region"), f"{field}.country_or_region"
                ),
                "timezone": _text(raw.get("timezone"), f"{field}.timezone"),
                "discovery_source": {
                    "url": _http_url(raw.get("source_url"), f"{field}.source_url"),
                    "observed_at": _iso(
                        _datetime(raw.get("observed_at"), f"{field}.observed_at")
                    ),
                    "request_id": task["task_id"],
                },
            }
        )
        node.pop("source_url", None)
        node.pop("observed_at", None)
        if node_id in existing:
            prior = existing[node_id]
            if prior.get("node_type") != node["node_type"]:
                raise LiveProviderError(f"discovered node {node_id} conflicts with existing node_type")
            old_coordinates = prior.get("coordinates")
            new_coordinates = node.get("coordinates")
            if old_coordinates and new_coordinates and old_coordinates != new_coordinates:
                raise LiveProviderError(f"discovered node {node_id} conflicts with existing coordinates")
            continue
        admitted.append(node)
        existing[node_id] = node
    try:
        ENGINE.validate_nodes(list(existing.values()))
    except ENGINE.PlannerError as exc:
        raise LiveProviderError(f"discovered_nodes are invalid: {exc}") from exc
    return admitted


def _validate_discovered_node_usage(
    discovered_nodes: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Keep endpoint discovery strict while admitting atomic-flight via airports.

    ``internal_only`` nodes describe the physical geometry of one verified
    provider-priced ticket.  They never widen an exact or node-pool task endpoint.
    """

    if not discovered_nodes:
        return
    endpoint_references: set[str] = set()
    internal_flight_references: set[str] = set()
    policies = task.get("endpoint_policy", {})
    for observation in observations:
        if observation.get("evidence_status") != VERIFIED_STATUS:
            continue
        for side in ("from", "to"):
            if policies.get(side) in {"discovered", "provider_authorized_anywhere"}:
                endpoint_references.add(observation[f"{side}_node"])
        ticket = observation.get("ticket_context")
        if observation.get("mode") == "flight" and isinstance(ticket, dict):
            internal_flight_references.update(ticket.get("via_nodes", []))
            ticketed_final_node = ticket.get("ticketed_final_node")
            if ticketed_final_node:
                internal_flight_references.add(str(ticketed_final_node).upper())

    for node in discovered_nodes:
        node_id = node["node_id"]
        used_as_endpoint = node_id in endpoint_references
        used_as_internal_flight_node = node_id in internal_flight_references
        if not used_as_endpoint and not used_as_internal_flight_node:
            raise LiveProviderError(
                f"discovered node {node_id} is not referenced by an authorized endpoint "
                "or verified atomic flight itinerary"
            )
        if used_as_internal_flight_node and node["node_type"] != "airport":
            raise LiveProviderError(
                f"atomic flight via node {node_id} must have node_type airport"
            )
        node["discovery_scope"] = (
            "endpoint_authorized" if used_as_endpoint else "internal_only"
        )


def _endpoint_allowed(
    node_id: str,
    side: str,
    task: dict[str, Any],
    known_nodes: set[str],
    declared_nodes: set[str],
) -> bool:
    policy = task.get("endpoint_policy", {}).get(side, "exact")
    exact = task.get("from_node" if side == "from" else "to_node")
    allowed = task.get("origins" if side == "from" else "destinations", [])
    allowed = {str(item).upper() for item in allowed if str(item).upper() != "ANYWHERE"}
    if policy in {"exact", "exact_node"}:
        return bool(exact) and node_id == str(exact).upper()
    if policy in {"allowed", "pool", "node_pool"}:
        return node_id in allowed
    if policy in {"discovered", "provider_authorized_anywhere"}:
        return node_id in declared_nodes or node_id in known_nodes
    return False


def _border_crossing_audit(
    state: dict[str, Any],
    start: str,
    end: str,
    via_nodes: list[str],
    declared: int,
) -> dict[str, Any]:
    """Compute only the minimum proven by node country codes.

    This deliberately does not infer borders from names, coordinates, routes,
    or general geographic knowledge.  Unknown country codes are skipped, so
    the result remains a lower bound between the known anchors.
    """
    nodes = {
        str(node.get("node_id", "")).upper(): node
        for node in state["nodes"]
        if isinstance(node, dict)
    }
    node_path = [start, *[str(node).upper() for node in via_nodes], end]
    audited_path: list[dict[str, Any]] = []
    known_sequence: list[tuple[str, str]] = []
    unknown_nodes: list[str] = []
    for node_id in node_path:
        raw_code = nodes.get(node_id, {}).get("country_code")
        country_code = (
            raw_code.strip().upper()
            if isinstance(raw_code, str)
            and len(raw_code.strip()) == 2
            and raw_code.strip().isalpha()
            else None
        )
        audited_path.append({"node_id": node_id, "country_code": country_code})
        if country_code is None:
            unknown_nodes.append(node_id)
        else:
            known_sequence.append((node_id, country_code))
    transitions = [
        {
            "from_node": previous_node,
            "from_country_code": previous_code,
            "to_node": next_node,
            "to_country_code": next_code,
        }
        for (previous_node, previous_code), (next_node, next_code) in zip(
            known_sequence, known_sequence[1:]
        )
        if previous_code != next_code
    ]
    computed_minimum = len(transitions)
    return {
        "method": "known_node_country_code_changes_lower_bound",
        "declared": declared,
        "computed_minimum": computed_minimum,
        "effective": max(declared, computed_minimum),
        "audited_node_path": audited_path,
        "known_country_code_transitions": transitions,
        "unknown_country_code_nodes": list(dict.fromkeys(unknown_nodes)),
    }


def _normalize_atomic_segments(
    raw_segments: Any,
    field: str,
    component_ids: list[str],
    mode: str,
    start: str,
    end: str,
    departure: datetime,
    arrival: datetime,
    known_nodes: set[str],
    planned_exit_node: str | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Validate complete atomic component timing without allocating its price."""
    if not isinstance(raw_segments, list) or len(raw_segments) != len(component_ids):
        raise LiveProviderError(
            f"{field} must contain one complete segment for every component_edge_id"
        )
    allowed_keys = {
        "component_id",
        "from_node",
        "to_node",
        "mode",
        "departure",
        "arrival",
    }
    normalized_segments: list[dict[str, str]] = []
    previous_to_node: str | None = None
    previous_arrival: datetime | None = None
    for index, (expected_id, raw_segment) in enumerate(
        zip(component_ids, raw_segments)
    ):
        segment_field = f"{field}[{index}]"
        if not isinstance(raw_segment, dict):
            raise LiveProviderError(f"{segment_field} must be an object")
        unknown_keys = sorted(set(raw_segment) - allowed_keys)
        if unknown_keys:
            raise LiveProviderError(
                f"{segment_field} has unsupported fields: {', '.join(unknown_keys)}"
            )
        component_id = _text(
            raw_segment.get("component_id"), f"{segment_field}.component_id"
        )
        if component_id != expected_id:
            raise LiveProviderError(
                f"{segment_field}.component_id must match component_edge_ids order"
            )
        segment_mode = _text(
            raw_segment.get("mode"), f"{segment_field}.mode"
        ).lower()
        if segment_mode != mode:
            raise LiveProviderError(
                f"{segment_field}.mode must match the atomic observation mode"
            )
        segment_start = _text(
            raw_segment.get("from_node"), f"{segment_field}.from_node"
        ).upper()
        segment_end = _text(
            raw_segment.get("to_node"), f"{segment_field}.to_node"
        ).upper()
        unknown_nodes = sorted({segment_start, segment_end} - known_nodes)
        if unknown_nodes:
            raise LiveProviderError(
                f"{segment_field} references unknown nodes: {', '.join(unknown_nodes)}"
            )
        if index == 0 and segment_start != start:
            raise LiveProviderError(
                f"{segment_field}.from_node must match the atomic observation origin"
            )
        if previous_to_node is not None and segment_start != previous_to_node:
            raise LiveProviderError(f"{segment_field} breaks itinerary node continuity")
        segment_departure = _datetime(
            raw_segment.get("departure"), f"{segment_field}.departure"
        )
        segment_arrival = _datetime(
            raw_segment.get("arrival"), f"{segment_field}.arrival"
        )
        if segment_arrival <= segment_departure:
            raise LiveProviderError(
                f"{segment_field}.arrival must be after departure"
            )
        if previous_arrival is not None and segment_departure < previous_arrival:
            raise LiveProviderError(f"{segment_field} overlaps the previous segment")
        normalized_segments.append(
            {
                "component_id": component_id,
                "from_node": segment_start,
                "to_node": segment_end,
                "mode": segment_mode,
                "departure": _iso(segment_departure),
                "arrival": _iso(segment_arrival),
            }
        )
        previous_to_node = segment_end
        previous_arrival = segment_arrival
    if normalized_segments[0]["departure"] != _iso(departure):
        raise LiveProviderError(
            f"{field} first departure must match the atomic observation departure"
        )
    if planned_exit_node is None:
        if previous_to_node != end or normalized_segments[-1]["arrival"] != _iso(arrival):
            raise LiveProviderError(
                f"{field} final arrival must match the atomic observation destination and arrival"
            )
    else:
        if end != planned_exit_node:
            raise LiveProviderError(
                f"{field} planned exit must match the observation destination"
            )
        exit_segments = [
            segment
            for segment in normalized_segments[:-1]
            if segment["to_node"] == planned_exit_node
        ]
        if len(exit_segments) != 1 or exit_segments[0]["arrival"] != _iso(arrival):
            raise LiveProviderError(
                f"{field} planned exit must be one ticketed connection with the observation arrival"
            )
    via_nodes = [segment["to_node"] for segment in normalized_segments[:-1]]
    if len(set(via_nodes)) != len(via_nodes):
        raise LiveProviderError(f"{field} via nodes must be unique")
    return normalized_segments, via_nodes


def _normalize_verified_observation(
    raw: dict[str, Any],
    normalized: dict[str, Any],
    index: int,
    task: dict[str, Any],
    state: dict[str, Any],
    declared_nodes: set[str] | None = None,
) -> dict[str, Any]:
    field = f"observations[{index}]"
    start = _text(raw.get("from_node"), f"{field}.from_node").upper()
    end = _text(raw.get("to_node"), f"{field}.to_node").upper()
    known_nodes = {node["node_id"].upper() for node in state["nodes"]}
    declared_nodes = declared_nodes or set()
    if not _endpoint_allowed(start, "from", task, known_nodes, declared_nodes) or not _endpoint_allowed(
        end, "to", task, known_nodes, declared_nodes
    ):
        raise LiveProviderError(
            f"{field} endpoints must exactly match or satisfy task endpoint policy "
            f"for {task['task_id']}"
        )
    if start not in known_nodes or end not in known_nodes:
        raise LiveProviderError(f"{field} references an unknown node")
    mode = _text(raw.get("mode"), f"{field}.mode").lower()
    if mode not in task["suggested_modes"]:
        raise LiveProviderError(f"{field}.mode is outside task suggested_modes")
    departure = _datetime(raw.get("departure"), f"{field}.departure")
    arrival = _datetime(raw.get("arrival"), f"{field}.arrival")
    if arrival <= departure:
        raise LiveProviderError(f"{field}.arrival must be after departure")
    earliest = _datetime(task["earliest_departure"], "task.earliest_departure")
    latest = _datetime(task["latest_arrival"], "task.latest_arrival")
    if departure < earliest or arrival > latest:
        raise LiveProviderError(f"{field} schedule falls outside the task time window")
    observed_at = _datetime(normalized["observed_at"], f"{field}.observed_at")
    if observed_at > departure:
        raise LiveProviderError(f"{field}.observed_at must not be after departure")
    expires_at = None
    if raw.get("expires_at") is not None:
        expires_at = _datetime(raw["expires_at"], f"{field}.expires_at")
        if expires_at <= observed_at:
            raise LiveProviderError(f"{field}.expires_at must be after observed_at")

    price = raw.get("price")
    if not isinstance(price, dict):
        raise LiveProviderError(f"{field}.price must be an object")
    price_currency = _currency(price.get("currency"), f"{field}.price.currency")
    baggage = raw.get("baggage")
    if not isinstance(baggage, dict):
        raise LiveProviderError(f"{field}.baggage must be an object")
    baggage_status = _text(baggage.get("status"), f"{field}.baggage.status").lower()
    if baggage_status not in {"confirmed", "not_applicable"}:
        raise LiveProviderError(
            f"{field}.baggage.status must be confirmed or not_applicable for verified evidence"
        )
    personal_item = baggage.get("included_personal_item")
    if not isinstance(personal_item, bool):
        raise LiveProviderError(f"{field}.baggage.included_personal_item must be boolean")
    mandatory = baggage.get("mandatory_cost")
    if not isinstance(mandatory, dict):
        raise LiveProviderError(f"{field}.baggage.mandatory_cost must be an object")
    baggage_currency = _currency(
        mandatory.get("currency"), f"{field}.baggage.mandatory_cost.currency"
    )
    configured_currencies = set(state["config"]["fx_to_base"])
    unknown = sorted({price_currency, baggage_currency} - configured_currencies)
    if unknown:
        raise LiveProviderError(
            f"{field} uses currency without config.fx_to_base: {', '.join(unknown)}"
        )

    declared_border_crossing_count = raw.get("border_crossing_count", 0)
    if isinstance(declared_border_crossing_count, bool) or not isinstance(declared_border_crossing_count, int) or declared_border_crossing_count < 0:
        raise LiveProviderError(f"{field}.border_crossing_count must be a non-negative integer")
    normalized.update(
        {
            "from_node": start,
            "to_node": end,
            "mode": mode,
            "departure": _iso(departure),
            "arrival": _iso(arrival),
            "price": {
                "amount": _number(price.get("amount"), f"{field}.price.amount"),
                "currency": price_currency,
            },
            "baggage": {
                "status": baggage_status,
                "included_personal_item": personal_item,
                "mandatory_cost": {
                    "amount": _number(
                        mandatory.get("amount"),
                        f"{field}.baggage.mandatory_cost.amount",
                    ),
                    "currency": baggage_currency,
                },
            },
            "border_crossing_count": declared_border_crossing_count,
        }
    )
    if mode == "flight":
        if raw.get("flight_identity") is None:
            raise LiveProviderError(
                f"{field}.flight_identity is required for a current-contract verified flight"
            )
        else:
            normalized["flight_identity"] = _normalize_flight_identity(
                raw["flight_identity"], f"{field}.flight_identity"
            )
    elif raw.get("flight_identity") is not None:
        raise LiveProviderError(f"{field}.flight_identity is only valid for flight mode")
    if expires_at is not None:
        normalized["expires_at"] = _iso(expires_at)
    for optional in (
        "operator",
        "service_id",
        "provider_offer_id",
        "independent_ticket_group",
    ):
        if raw.get(optional) is not None:
            normalized[optional] = _text(raw[optional], f"{field}.{optional}")
    if raw.get("ticket_context") is not None:
        if not isinstance(raw["ticket_context"], dict):
            raise LiveProviderError(f"{field}.ticket_context must be an object")
        ticket = copy.deepcopy(raw["ticket_context"])
        if ticket.get("pricing_scope") != "atomic_offer":
            raise LiveProviderError(
                f"{field}.ticket_context.pricing_scope must be atomic_offer"
            )
        atomic_id = _text(
            ticket.get("atomic_offer_id"),
            f"{field}.ticket_context.atomic_offer_id",
        )
        components = ticket.get("component_edge_ids")
        if (
            not isinstance(components, list)
            or len(components) < 2
            or not all(isinstance(item, str) and item.strip() for item in components)
            or len(set(components)) != len(components)
        ):
            raise LiveProviderError(
                f"{field}.ticket_context.component_edge_ids must contain at least two distinct ids"
            )
        normalized_components = [item.strip() for item in components]
        via_nodes = ticket.get("via_nodes", [])
        if not isinstance(via_nodes, list) or not all(
            isinstance(item, str) and item.strip() for item in via_nodes
        ):
            raise LiveProviderError(
                f"{field}.ticket_context.via_nodes must be a string array"
            )
        normalized_via_nodes = [item.strip().upper() for item in via_nodes]
        use_pattern = ticket.get("use_pattern")
        planned_exit_node = None
        if use_pattern is not None:
            if use_pattern != "planned_exit_at_connection":
                raise LiveProviderError(
                    f"{field}.ticket_context.use_pattern is unsupported"
                )
            planned_exit_node = _text(
                ticket.get("planned_exit_node"),
                f"{field}.ticket_context.planned_exit_node",
            ).upper()
            if planned_exit_node != end:
                raise LiveProviderError(
                    f"{field}.ticket_context.planned_exit_node must match the observation destination"
                )
        elif any(
            ticket.get(name) is not None
            for name in (
                "ticketed_final_node",
                "planned_exit_node",
                "used_component_edge_ids",
                "unused_component_edge_ids",
                "used_via_nodes",
            )
        ):
            raise LiveProviderError(
                f"{field}.ticket_context planned-exit fields require use_pattern"
            )
        if ticket.get("segments") is not None:
            normalized_segments, segment_via_nodes = _normalize_atomic_segments(
                ticket["segments"],
                f"{field}.ticket_context.segments",
                normalized_components,
                mode,
                start,
                end,
                departure,
                arrival,
                known_nodes,
                planned_exit_node,
            )
            if normalized_via_nodes and normalized_via_nodes != segment_via_nodes:
                raise LiveProviderError(
                    f"{field}.ticket_context.via_nodes conflict with segments"
                )
            normalized_via_nodes = segment_via_nodes
            ticket["segments"] = normalized_segments
            if planned_exit_node is not None:
                ticketed_final_node = _text(
                    ticket.get("ticketed_final_node"),
                    f"{field}.ticket_context.ticketed_final_node",
                ).upper()
                if ticketed_final_node != normalized_segments[-1]["to_node"]:
                    raise LiveProviderError(
                        f"{field}.ticket_context.ticketed_final_node must match the full ticketed itinerary"
                    )
                exit_index = next(
                    index
                    for index, segment in enumerate(normalized_segments)
                    if segment["to_node"] == planned_exit_node
                )
                expected_used = normalized_components[: exit_index + 1]
                expected_unused = normalized_components[exit_index + 1 :]
                expected_used_via = [
                    segment["to_node"]
                    for segment in normalized_segments[:exit_index]
                ]
                for list_field, expected in (
                    ("used_component_edge_ids", expected_used),
                    ("unused_component_edge_ids", expected_unused),
                    ("used_via_nodes", expected_used_via),
                ):
                    raw_values = ticket.get(list_field)
                    if not isinstance(raw_values, list) or [
                        str(item).strip().upper()
                        if list_field == "used_via_nodes"
                        else str(item).strip()
                        for item in raw_values
                    ] != expected:
                        raise LiveProviderError(
                            f"{field}.ticket_context.{list_field} conflicts with the full ticketed itinerary"
                        )
                ticket["ticketed_final_node"] = ticketed_final_node
                ticket["planned_exit_node"] = planned_exit_node
                ticket["used_component_edge_ids"] = expected_used
                ticket["unused_component_edge_ids"] = expected_unused
                ticket["used_via_nodes"] = expected_used_via
        elif planned_exit_node is not None:
            raise LiveProviderError(
                f"{field}.ticket_context planned exit requires complete ticketed segments"
            )
        unknown_via = sorted(set(normalized_via_nodes) - known_nodes)
        if unknown_via:
            raise LiveProviderError(
                f"{field}.ticket_context.via_nodes reference unknown nodes: "
                + ", ".join(unknown_via)
            )
        ticket["atomic_offer_id"] = atomic_id
        ticket["component_edge_ids"] = normalized_components
        ticket["via_nodes"] = normalized_via_nodes
        normalized["ticket_context"] = ticket
    if raw.get("path_id") is not None or raw.get("segment_index") is not None:
        raise LiveProviderError(
            f"{field} multi-segment bridge paths are not supported; return one atomic offer"
        )
    for optional in ("atomic_offer_id", "component_edge_ids", "via_nodes"):
        if raw.get(optional) is not None:
            normalized[optional] = copy.deepcopy(raw[optional])
    tags = raw.get("entitlement_tags", [])
    if not isinstance(tags, list) or not all(isinstance(item, str) and item for item in tags):
        raise LiveProviderError(f"{field}.entitlement_tags must be a string array")
    normalized["entitlement_tags"] = list(dict.fromkeys(tags))
    ticket_context = normalized.get("ticket_context", {})
    via_nodes = ticket_context.get(
        "used_via_nodes", ticket_context.get("via_nodes", [])
    )
    crossing_audit = _border_crossing_audit(
        state,
        start,
        end,
        via_nodes if isinstance(via_nodes, list) else [],
        declared_border_crossing_count,
    )
    normalized["border_crossing_audit"] = crossing_audit
    normalized["border_crossing_count"] = crossing_audit["effective"]
    return normalized


def validate_evidence_bundle(
    raw: Any, state: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict):
        raise LiveProviderError("evidence bundle must be an object")
    bundle_version = _text(raw.get("bundle_version"), "bundle_version")
    bundle_id = _text(raw.get("bundle_id"), "bundle_id")
    request_id = _text(raw.get("request_id"), "request_id")
    tasks = {task["task_id"]: task for task in state["research_tasks"]}
    task = tasks.get(request_id)
    if task is None:
        raise LiveProviderError(f"request_id is not present in this session: {request_id}")
    expected_contract = str(task.get("evidence_contract_version", SESSION_VERSION))
    if bundle_version != expected_contract:
        raise LiveProviderError(f"bundle_version must be {expected_contract}")
    if task.get("status") not in {"candidate", "pending"}:
        raise LiveProviderError(f"request {request_id} is not AI-actionable")
    if any(item.get("bundle_id") == bundle_id for item in state["evidence_bundles"]):
        raise LiveProviderError(f"duplicate bundle_id: {bundle_id}")
    discovered_nodes = _normalize_discovered_nodes(
        raw.get("discovered_nodes"), state, task
    )
    prospective_state = copy.deepcopy(state)
    prospective_state["nodes"].extend(copy.deepcopy(discovered_nodes))
    declared_node_ids = {node["node_id"] for node in discovered_nodes}
    observations = raw.get("observations")
    if not isinstance(observations, list) or not observations:
        raise LiveProviderError("observations must be a non-empty array")
    normalized_observations = []
    ids = set()
    for index, observation in enumerate(observations):
        normalized = _normalize_common_observation(observation, index)
        if normalized["observation_id"] in ids:
            raise LiveProviderError(
                f"duplicate observation_id: {normalized['observation_id']}"
            )
        ids.add(normalized["observation_id"])
        if normalized["evidence_status"] == VERIFIED_STATUS:
            normalized = _normalize_verified_observation(
                observation,
                normalized,
                index,
                task,
                prospective_state,
                declared_node_ids,
            )
        normalized_observations.append(normalized)
    _validate_discovered_node_usage(
        discovered_nodes, normalized_observations, task
    )
    normalized_bundle = {
        "bundle_version": bundle_version,
        "bundle_id": bundle_id,
        "request_id": request_id,
        "discovered_nodes": discovered_nodes,
        "observations": normalized_observations,
        "assigned_source_fulfillment": _assigned_source_fulfillment(
            task, normalized_observations
        ),
    }
    return normalized_bundle, task


def _edge_from_observation(
    bundle: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    ticket_context = observation.get("ticket_context") or {}
    itinerary_fields = {
        "from_node": observation["from_node"],
        "to_node": observation["to_node"],
        "mode": observation["mode"],
        "departure": observation["departure"],
        "arrival": observation["arrival"],
    }
    if observation["mode"] == "flight":
        # Exact schedule geometry is the cross-provider identity.  Carrier display
        # names and disclosure of flight numbers vary by seller, so neither may
        # split one physical itinerary into separate priced edges.
        itinerary_fields["identity_basis"] = "schedule_geometry"
    else:
        itinerary_fields["operator"] = observation.get("operator")
        itinerary_fields["service_id"] = observation.get("service_id")
        itinerary_fields["via_nodes"] = ticket_context.get(
            "via_nodes", observation.get("via_nodes", [])
        )
    itinerary_fingerprint = hashlib.sha256(
        json.dumps(itinerary_fields, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    itinerary_id = f"ITIN-{itinerary_fingerprint[:20].upper()}"
    provider_offer_key = json.dumps(
        {
            "itinerary_id": itinerary_id,
            "provider": observation["provider"].casefold(),
            "provider_offer_id": observation.get("provider_offer_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    offer_id = "OFFER-" + hashlib.sha256(
        provider_offer_key.encode("utf-8")
    ).hexdigest()[:20].upper()
    snapshot_fields = {
        key: observation.get(key)
        for key in (
            "provider",
            "provider_offer_id",
            "url",
            "observed_at",
            "expires_at",
            "price",
            "baggage",
            "ticket_context",
        )
    }
    snapshot_fingerprint = hashlib.sha256(
        json.dumps(snapshot_fields, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    snapshot_id = f"SNAP-{snapshot_fingerprint[:20].upper()}"
    snapshot = {
        "snapshot_id": snapshot_id,
        "evidence_status": VERIFIED_STATUS,
        "url": observation["url"],
        "observed_at": observation["observed_at"],
        "price": copy.deepcopy(observation["price"]),
        "baggage": copy.deepcopy(observation["baggage"]),
        "bundle_id": bundle["bundle_id"],
        "request_id": bundle["request_id"],
        "observation_id": observation["observation_id"],
    }
    if observation.get("expires_at"):
        snapshot["expires_at"] = observation["expires_at"]
        snapshot["expires_at_status"] = "observed"
    else:
        snapshot["expires_at_status"] = "not_disclosed_by_source"
    if observation.get("ticket_context") is not None:
        snapshot["ticket_context"] = copy.deepcopy(observation["ticket_context"])
    if observation.get("flight_identity") is not None:
        snapshot["flight_identity"] = copy.deepcopy(observation["flight_identity"])
    if observation.get("discovery_provenance") is not None:
        snapshot["discovery_provenance"] = copy.deepcopy(
            observation["discovery_provenance"]
        )
    source = {
        "provider_id": observation["provider"],
        "url": observation["url"],
        "observed_at": observation["observed_at"],
        "evidence_type": "browser_observed",
        "evidence_status": VERIFIED_STATUS,
        "confidence": 1.0,
        "bundle_id": bundle["bundle_id"],
        "request_id": bundle["request_id"],
        "observation_id": observation["observation_id"],
    }
    if observation.get("expires_at"):
        source["expires_at"] = observation["expires_at"]
    if observation.get("discovery_provenance") is not None:
        source["discovery_provenance"] = copy.deepcopy(
            observation["discovery_provenance"]
        )
    edge = {
        "edge_id": f"LIVE-{itinerary_fingerprint[:20].upper()}",
        "itinerary_id": itinerary_id,
        "itinerary_identity": {
            "itinerary_id": itinerary_id,
            **copy.deepcopy(itinerary_fields),
        },
        "offer_fingerprint": snapshot_id,
        "offer_set": {
            "itinerary_id": itinerary_id,
            "offers": [
                {
                    "offer_id": offer_id,
                    "provider": observation["provider"],
                    "snapshots": [snapshot],
                    **(
                        {"provider_offer_id": observation["provider_offer_id"]}
                        if observation.get("provider_offer_id")
                        else {}
                    ),
                }
            ],
        },
        "from_node": observation["from_node"],
        "to_node": observation["to_node"],
        "mode": observation["mode"],
        "departure": observation["departure"],
        "arrival": observation["arrival"],
        "cost": observation["price"],
        "cost_category": "airfare"
        if observation["mode"] == "flight"
        else "intermediate_ground_cost",
        "mandatory_baggage_cost": observation["baggage"]["mandatory_cost"],
        "included_baggage": ["personal_item"]
        if observation["baggage"]["included_personal_item"]
        else [],
        "entitlement_tags": observation["entitlement_tags"],
        "border_crossing_count": observation["border_crossing_count"],
        "border_crossing_audit": copy.deepcopy(
            observation["border_crossing_audit"]
        ),
        "source": source,
    }
    for optional in (
        "operator",
        "service_id",
        "flight_identity",
        "independent_ticket_group",
    ):
        if observation.get(optional):
            edge[optional] = copy.deepcopy(observation[optional])
    for optional in ("ticket_context", "atomic_offer_id", "component_edge_ids", "via_nodes"):
        if observation.get(optional) is not None:
            edge[optional] = copy.deepcopy(observation[optional])
    return edge


def _snapshot_base_price(snapshot: dict[str, Any], config: dict[str, Any]) -> float:
    price = snapshot["price"]
    baggage = snapshot["baggage"]["mandatory_cost"]
    airfare_base = float(price["amount"]) * float(config["fx_to_base"][price["currency"]])
    baggage_base = float(baggage["amount"]) * float(
        config["fx_to_base"][baggage["currency"]]
    )
    return round(airfare_base + baggage_base, 8)


def _selected_snapshot_candidates(
    offer_set: dict[str, Any],
    config: dict[str, Any],
    *,
    evaluated_at: datetime | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for offer in offer_set["offers"]:
        snapshots = [
            snapshot
            for snapshot in offer.get("snapshots", [])
            if evaluated_at is None
            or (
                _datetime(snapshot["observed_at"], "offer snapshot observed_at")
                <= evaluated_at
                and (
                    snapshot.get("expires_at") is None
                    or _datetime(
                        snapshot["expires_at"], "offer snapshot expires_at"
                    )
                    > evaluated_at
                )
            )
        ]
        if not snapshots:
            continue
        latest = max(
            snapshots,
            key=lambda item: (
                _datetime(item["observed_at"], "offer snapshot observed_at"),
                item["snapshot_id"],
            ),
        )
        candidates.append((offer, latest, _snapshot_base_price(latest, config)))
    if not candidates:
        return []
    if evaluated_at is not None:
        return candidates
    as_of = max(
        _datetime(snapshot["observed_at"], "offer snapshot observed_at")
        for _, snapshot, _ in candidates
    )
    current = [
        item
        for item in candidates
        if item[1].get("expires_at") is None
        or _datetime(item[1]["expires_at"], "offer snapshot expires_at") > as_of
    ]
    return current or candidates


def _apply_offer_selection(
    edge: dict[str, Any],
    config: dict[str, Any],
    *,
    evaluated_at: datetime | None = None,
) -> None:
    offer_set = edge["offer_set"]
    candidates = _selected_snapshot_candidates(
        offer_set, config, evaluated_at=evaluated_at
    )
    if not candidates:
        raise LiveProviderError(f"edge {edge['edge_id']} offer_set has no snapshots")
    cheapest_offer, cheapest_snapshot, cheapest_base = min(
        candidates,
        key=lambda item: (item[2], item[0]["provider"].casefold(), item[1]["snapshot_id"]),
    )
    live_settings = config.get("live_provider_session", {})
    preferred_provider = None
    if isinstance(live_settings, dict) and live_settings.get("preferred_provider") is not None:
        preferred_provider = _text(
            live_settings["preferred_provider"],
            "live_provider_session.preferred_provider",
        )
    selected_offer, selected_snapshot, selected_base = (
        cheapest_offer,
        cheapest_snapshot,
        cheapest_base,
    )
    selection_policy = "lowest_verified"
    if preferred_provider:
        preferred = [
            item
            for item in candidates
            if item[0]["provider"].casefold() == preferred_provider.casefold()
        ]
        if preferred:
            selected_offer, selected_snapshot, selected_base = min(
                preferred,
                key=lambda item: (item[2], item[1]["snapshot_id"]),
            )
            selection_policy = "preferred_provider"
        else:
            selection_policy = "preferred_provider_fallback_lowest"
    minimum = {
        "base_fare": copy.deepcopy(cheapest_snapshot["price"]),
        "mandatory_baggage_cost": copy.deepcopy(
            cheapest_snapshot["baggage"]["mandatory_cost"]
        ),
        "complete_mandatory_base_amount": round(cheapest_base, 2),
        "base_currency": config["base_currency"],
        "provider": cheapest_offer["provider"],
        "offer_id": cheapest_offer["offer_id"],
        "snapshot_id": cheapest_snapshot["snapshot_id"],
    }
    offer_set.update(
        {
            "minimum_verified_price": minimum,
            "selected_offer_id": selected_offer["offer_id"],
            "selected_snapshot_id": selected_snapshot["snapshot_id"],
            "selected_provider": selected_offer["provider"],
            "selected_price": {
                "base_fare": copy.deepcopy(selected_snapshot["price"]),
                "mandatory_baggage_cost": copy.deepcopy(
                    selected_snapshot["baggage"]["mandatory_cost"]
                ),
                "complete_mandatory_base_amount": round(selected_base, 2),
                "base_currency": config["base_currency"],
            },
            "selection_policy": selection_policy,
            "selection_as_of": max(
                snapshot["observed_at"] for _, snapshot, _ in candidates
            ),
        }
    )
    if preferred_provider:
        offer_set["preferred_provider"] = preferred_provider
    else:
        offer_set.pop("preferred_provider", None)
    edge["cost"] = copy.deepcopy(selected_snapshot["price"])
    edge["mandatory_baggage_cost"] = copy.deepcopy(
        selected_snapshot["baggage"]["mandatory_cost"]
    )
    edge["included_baggage"] = (
        ["personal_item"]
        if selected_snapshot["baggage"]["included_personal_item"]
        else []
    )
    edge["offer_fingerprint"] = selected_snapshot["snapshot_id"]
    edge["source"] = {
        "provider_id": selected_offer["provider"],
        "url": selected_snapshot["url"],
        "observed_at": selected_snapshot["observed_at"],
        "evidence_type": "browser_observed",
        "evidence_status": VERIFIED_STATUS,
        "confidence": 1.0,
        "bundle_id": selected_snapshot["bundle_id"],
        "request_id": selected_snapshot["request_id"],
        "observation_id": selected_snapshot["observation_id"],
    }
    if selected_snapshot.get("expires_at"):
        edge["source"]["expires_at"] = selected_snapshot["expires_at"]
    if selected_snapshot.get("discovery_provenance") is not None:
        edge["source"]["discovery_provenance"] = copy.deepcopy(
            selected_snapshot["discovery_provenance"]
        )
    if selected_snapshot.get("ticket_context") is not None:
        edge["ticket_context"] = copy.deepcopy(selected_snapshot["ticket_context"])
        edge["itinerary_identity"]["via_nodes"] = copy.deepcopy(
            selected_snapshot["ticket_context"].get("via_nodes", [])
        )
    else:
        edge.pop("ticket_context", None)
        edge["itinerary_identity"].pop("via_nodes", None)
    if selected_snapshot.get("flight_identity") is not None:
        edge["flight_identity"] = copy.deepcopy(selected_snapshot["flight_identity"])
        edge["itinerary_identity"]["flight_identity"] = copy.deepcopy(
            selected_snapshot["flight_identity"]
        )
        edge.pop("identity_migration", None)
        edge["itinerary_identity"].pop("identity_migration", None)


def _merge_offer_edges(
    current_edges: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    edges = copy.deepcopy(current_edges)
    by_itinerary = {
        edge.get("itinerary_id"): edge
        for edge in edges
        if edge.get("itinerary_id") and isinstance(edge.get("offer_set"), dict)
    }
    admitted_edge_ids: list[str] = []
    for candidate in candidates:
        existing = by_itinerary.get(candidate["itinerary_id"])
        if existing is None:
            existing = candidate
            edges.append(existing)
            by_itinerary[candidate["itinerary_id"]] = existing
        else:
            existing_identity = existing.get("flight_identity", {})
            candidate_identity = candidate.get("flight_identity", {})
            existing_numbers = set(existing_identity.get("flight_numbers", []))
            candidate_numbers = set(candidate_identity.get("flight_numbers", []))
            if existing_numbers and candidate_numbers and existing_numbers != candidate_numbers:
                raise LiveProviderError(
                    f"itinerary identity collision for {candidate['itinerary_id']}: "
                    "providers disclosed conflicting flight numbers"
                )
            existing_via = set(existing.get("ticket_context", {}).get("via_nodes", []))
            candidate_via = set(candidate.get("ticket_context", {}).get("via_nodes", []))
            if existing_via and candidate_via and existing_via != candidate_via:
                raise LiveProviderError(
                    f"itinerary identity collision for {candidate['itinerary_id']}: "
                    "providers disclosed conflicting intermediate nodes"
                )
            existing_offers = {
                offer["offer_id"]: offer for offer in existing["offer_set"]["offers"]
            }
            for candidate_offer in candidate["offer_set"]["offers"]:
                existing_offer = existing_offers.get(candidate_offer["offer_id"])
                if existing_offer is None:
                    existing["offer_set"]["offers"].append(copy.deepcopy(candidate_offer))
                    continue
                snapshot_ids = {
                    snapshot["snapshot_id"] for snapshot in existing_offer["snapshots"]
                }
                existing_offer["snapshots"].extend(
                    copy.deepcopy(snapshot)
                    for snapshot in candidate_offer["snapshots"]
                    if snapshot["snapshot_id"] not in snapshot_ids
                )
            if candidate.get("border_crossing_count", 0) > existing.get(
                "border_crossing_count", 0
            ):
                existing["border_crossing_count"] = candidate[
                    "border_crossing_count"
                ]
                existing["border_crossing_audit"] = copy.deepcopy(
                    candidate["border_crossing_audit"]
                )
        _apply_offer_selection(existing, config)
        if existing["edge_id"] not in admitted_edge_ids:
            admitted_edge_ids.append(existing["edge_id"])
    return edges, admitted_edge_ids


def _result_incumbent(result: dict[str, Any]) -> dict[str, Any] | None:
    incumbent = result.get("active_incumbent")
    if incumbent:
        return copy.deepcopy(incumbent)
    routes = result.get("routes", [])
    if not routes:
        return None
    route = min(
        routes,
        key=lambda item: (
            float(
                item.get("mandatory_air_ticket_total", {}).get(
                    "amount", item["total_trip_cost"]
                )
            ),
            float(item.get("total_travel_time_minutes", math.inf)),
            item.get("id", ""),
        ),
    )
    return {
        "route_id": route.get("id"),
        "total_trip_cost": route["total_trip_cost"],
        "currency": result.get("base_currency"),
        "edge_ids": list(route.get("edge_ids", [])),
        "source": "observed_complete_route",
    }


def _evaluate_solver_result(
    state: dict[str, Any], evaluated_at: datetime
) -> dict[str, Any]:
    """Rebuild the solver projection from Offers fresh at one evaluation time."""

    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise LiveProviderError("solver evaluation time must include a timezone")
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    try:
        nodes = ENGINE.validate_nodes(state["nodes"])
        config = ENGINE.validate_config(state["config"], state["profile"])
        config["internal_surface_transfers"] = copy.deepcopy(
            state.get("internal_surface_transfers", [])
        )
        profile = ENGINE.validate_profile(state["profile"])
        edges = ENGINE.validate_edges(state["edges"], nodes, config)
        eligible_edges: list[dict[str, Any]] = []
        excluded_edge_ids: list[str] = []
        for edge in edges:
            candidate = copy.deepcopy(edge)
            offer_set = candidate.get("offer_set")
            if isinstance(offer_set, dict):
                try:
                    _apply_offer_selection(
                        candidate,
                        config,
                        evaluated_at=evaluated_at,
                    )
                except LiveProviderError:
                    excluded_edge_ids.append(str(candidate.get("edge_id")))
                    continue
            else:
                source = candidate.get("source")
                if isinstance(source, dict):
                    observed_at = source.get("observed_at")
                    expires_at = source.get("expires_at")
                    if observed_at is not None and _datetime(
                        observed_at,
                        f"edge {candidate.get('edge_id')} source.observed_at",
                    ) > evaluated_at:
                        excluded_edge_ids.append(str(candidate.get("edge_id")))
                        continue
                    if expires_at is not None and _datetime(
                        expires_at,
                        f"edge {candidate.get('edge_id')} source.expires_at",
                    ) <= evaluated_at:
                        excluded_edge_ids.append(str(candidate.get("edge_id")))
                        continue
            eligible_edges.append(candidate)
        config.setdefault("frontier_search", {})["search_as_of"] = _iso(
            evaluated_at
        )
        result = ENGINE.solve(config, profile, nodes, eligible_edges)
    except ENGINE.PlannerError as exc:
        raise LiveProviderError(f"solver evaluation failed: {exc}") from exc
    result["evaluation_as_of"] = _iso(evaluated_at)
    result["excluded_stale_edge_ids"] = sorted(set(excluded_edge_ids))
    return {
        "evaluated_at": evaluated_at,
        "nodes": nodes,
        "config": config,
        "profile": profile,
        "edges": edges,
        "eligible_edges": eligible_edges,
        "excluded_edge_ids": sorted(set(excluded_edge_ids)),
        "result": result,
    }


def _recompute_session(state: dict[str, Any]) -> dict[str, Any]:
    evaluation = _evaluate_solver_result(state, datetime.now(timezone.utc))
    nodes = evaluation["nodes"]
    config = evaluation["config"]
    profile = evaluation["profile"]
    edges = evaluation["edges"]
    result = evaluation["result"]
    previous = copy.deepcopy(state.get("active_incumbent"))
    active = _result_incumbent(result)
    state["config"] = config
    state["profile"] = profile
    state["nodes"] = _as_node_list(nodes)
    state["edges"] = edges
    state["result"] = result
    state["active_incumbent"] = active
    if active != previous:
        state.setdefault("incumbent_history", []).append(
            {
                "search_round": state.get("search_round", 0),
                "previous": previous,
                "active": copy.deepcopy(active),
            }
        )
    provider_plan = _provider_plan_from_result(result, nodes, config)
    state["research_tasks"] = _merge_tasks(
        state["research_tasks"],
        provider_plan["requests"],
        None,
        int(state.get("search_round", 0)),
    )
    state["provider_coverage_audit"] = _merge_provider_coverage(
        state.get("provider_coverage_audit"),
        provider_plan,
        state["research_tasks"],
        int(state.get("search_round", 0)),
    )
    _sync_hypothesis_tree(state)
    _refresh_frontier_exploration(state)
    return state


def _publish_internal_surface_transfer_tasks(
    state: dict[str, Any], source_task: dict[str, Any]
) -> None:
    """Publish the cheapest viable non-Offer bridge, never a Cartesian batch."""

    if int(state.get("research_credits", {}).get("remaining", 0)) < 1:
        return
    if any(
        task.get("request_type") == "internal_surface_transfer"
        and task.get("status") in {"candidate", "pending"}
        for task in state.get("research_tasks", [])
        if isinstance(task, dict)
    ):
        return

    flights = sorted(
        (
            edge
            for edge in state.get("edges", [])
            if isinstance(edge, dict) and edge.get("mode") == "flight"
        ),
        key=lambda edge: (str(edge.get("departure")), str(edge.get("arrival"))),
    )
    origins = set(state.get("profile", {}).get("origin_airport_nodes", []))
    destinations = set(
        state.get("profile", {}).get("destination_airport_nodes", [])
    )
    existing_pairs = {
        (str(item.get("from_node")), str(item.get("to_node")))
        for item in state.get("internal_surface_transfers", [])
        if isinstance(item, dict)
    }
    existing_tasks = {
        (
            str((task.get("internal_surface_transfer_context") or {}).get("from_node") or task.get("from_node")),
            str((task.get("internal_surface_transfer_context") or {}).get("to_node") or task.get("to_node")),
        )
        for task in state.get("research_tasks", [])
        if task.get("request_type") == "internal_surface_transfer"
    }
    next_sequence = max(
        (int(task.get("sequence", 0)) for task in state.get("research_tasks", [])),
        default=0,
    )
    incumbent = state.get("active_incumbent") or {}
    incumbent_money = incumbent.get("mandatory_air_ticket_total")
    incumbent_amount = (
        float(incumbent_money["amount"])
        if isinstance(incumbent_money, dict)
        and isinstance(incumbent_money.get("amount"), (int, float))
        and not isinstance(incumbent_money.get("amount"), bool)
        else None
    )
    threshold = float(
        state.get("research_credits", {}).get("price_challenge_threshold", 0.05)
    )
    competitive_cutoff = (
        incumbent_amount * (1.0 - threshold)
        if incumbent_amount is not None
        else None
    )
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for previous in flights:
        if previous.get("from_node") not in origins:
            continue
        previous_arrival = _datetime(
            previous.get("arrival"), "previous flight arrival"
        )
        for following in flights:
            if following.get("to_node") not in destinations:
                continue
            if previous.get("edge_id") == following.get("edge_id"):
                continue
            from_node = str(previous.get("to_node") or "")
            to_node = str(following.get("from_node") or "")
            if not from_node or not to_node or from_node == to_node:
                continue
            following_departure = _datetime(
                following.get("departure"), "following flight departure"
            )
            available_minutes = int(
                (following_departure - previous_arrival).total_seconds() // 60
            )
            if available_minutes <= 0:
                continue
            pair = (from_node, to_node)
            if pair in existing_pairs or pair in existing_tasks:
                continue
            optimistic_air_total = 0.0
            for edge in (previous, following):
                effective, _ = ENGINE.edge_effective_cost(
                    edge, state["profile"], state["config"]
                )
                baggage = ENGINE.to_base(
                    edge["mandatory_baggage_cost"],
                    state["config"],
                    f"edge {edge.get('edge_id')}.mandatory_baggage_cost",
                )
                optimistic_air_total += effective + baggage
            if (
                competitive_cutoff is not None
                and optimistic_air_total >= competitive_cutoff
            ):
                continue
            candidate = {
                "from_node": from_node,
                "to_node": to_node,
                "available_minutes": available_minutes,
                "previous_edge_id": previous.get("edge_id"),
                "following_edge_id": following.get("edge_id"),
                "previous_arrival": previous["arrival"],
                "following_departure": following["departure"],
                "optimistic_mandatory_air_ticket_total": round(
                    optimistic_air_total, 2
                ),
            }
            current = candidates.get(pair)
            if current is None or (
                candidate["optimistic_mandatory_air_ticket_total"],
                -candidate["available_minutes"],
                str(candidate["previous_edge_id"]),
                str(candidate["following_edge_id"]),
            ) < (
                current["optimistic_mandatory_air_ticket_total"],
                -current["available_minutes"],
                str(current["previous_edge_id"]),
                str(current["following_edge_id"]),
            ):
                candidates[pair] = candidate

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item["optimistic_mandatory_air_ticket_total"],
            item["from_node"],
            item["to_node"],
        ),
    )
    for candidate in ordered[:1]:
            from_node = candidate["from_node"]
            to_node = candidate["to_node"]
            request = _normalized_request(
                {
                    "request_id": f"internal-surface-{from_node}-{to_node}",
                    "request_type": "internal_surface_transfer",
                    "origins": [from_node],
                    "destinations": [to_node],
                    "from_node": from_node,
                    "to_node": to_node,
                    "earliest_departure": candidate["previous_arrival"],
                    "latest_arrival": candidate["following_departure"],
                    "suggested_modes": [
                        "rail",
                        "bus",
                        "coach",
                        "local_transit",
                        "rideshare",
                        "car",
                    ],
                    "provider_kinds": ["road_routing", "scheduled_transport"],
                    "endpoint_policy": {
                        "from": "exact_node",
                        "to": "exact_node",
                    },
                    "date_search": {"mode": "bounded_window"},
                    "hypothesis_id": source_task.get("hypothesis_id"),
                    "hypothesis_kind": "complete_journey",
                    "route_shape": "internal_surface_transfer",
                }
            )
            next_sequence += 1
            task = _research_task(request, next_sequence)
            task["research_kind"] = "internal_surface_transfer"
            task["research_credit_cost"] = 1
            task["optimistic_mandatory_air_ticket_total"] = candidate[
                "optimistic_mandatory_air_ticket_total"
            ]
            task["internal_surface_transfer_context"] = {
                "from_node": from_node,
                "to_node": to_node,
                "available_minutes": candidate["available_minutes"],
                "previous_edge_id": candidate["previous_edge_id"],
                "following_edge_id": candidate["following_edge_id"],
            }
            task["status"] = "pending"
            state["research_tasks"].append(task)
            existing_tasks.add(pair)


def internal_surface_transfer_attempt_identity(raw_transfer: Any) -> str:
    """Return a semantic replay identity before feasibility normalization."""

    if not isinstance(raw_transfer, dict):
        raise LiveProviderError("Internal Surface Transfer Capture must be an object")
    allowed_fields = {
        "from_node",
        "to_node",
        "approximate_duration_minutes",
        "feasibility",
        "evidence_url",
        "observed_at",
    }
    if set(raw_transfer) != allowed_fields:
        raise LiveProviderError(
            "Internal Surface Transfer Capture has unsupported fields"
        )
    duration = raw_transfer.get("approximate_duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        raise LiveProviderError("approximate_duration_minutes must be positive")
    feasibility = raw_transfer.get("feasibility")
    if feasibility not in {"verified_route", "fallback_plausible", "infeasible"}:
        raise LiveProviderError("Internal Surface Transfer feasibility is unsupported")
    canonical = {
        "from_node": _text(raw_transfer.get("from_node"), "transfer.from_node"),
        "to_node": _text(raw_transfer.get("to_node"), "transfer.to_node"),
        "approximate_duration_minutes": duration,
        "feasibility": feasibility,
        "evidence_url": _http_url(
            raw_transfer.get("evidence_url"), "transfer.evidence_url"
        ),
        "observed_at": _iso(
            _datetime(raw_transfer.get("observed_at"), "transfer.observed_at")
        ),
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def ingest_internal_surface_transfer(
    raw_state: Any,
    task_id: str,
    raw_transfer: Any,
) -> dict[str, Any]:
    state = validate_session(raw_state)
    task = next(
        (
            item
            for item in state.get("research_tasks", [])
            if item.get("task_id") == task_id
        ),
        None,
    )
    if not isinstance(task, dict) or task.get("request_type") != "internal_surface_transfer":
        raise LiveProviderError("Internal Surface Transfer Work Item is unknown")
    attempt_identity = internal_surface_transfer_attempt_identity(raw_transfer)
    context = task.get("internal_surface_transfer_context", {})
    from_node = _text(raw_transfer.get("from_node"), "transfer.from_node")
    to_node = _text(raw_transfer.get("to_node"), "transfer.to_node")
    if from_node != context.get("from_node") or to_node != context.get("to_node"):
        raise LiveProviderError("Internal Surface Transfer endpoints do not match")
    duration = raw_transfer.get("approximate_duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        raise LiveProviderError("approximate_duration_minutes must be positive")
    feasibility = raw_transfer.get("feasibility")
    if feasibility not in {"verified_route", "fallback_plausible", "infeasible"}:
        raise LiveProviderError("Internal Surface Transfer feasibility is unsupported")
    evidence_url = _http_url(raw_transfer.get("evidence_url"), "transfer.evidence_url")
    observed_at = _iso(_datetime(raw_transfer.get("observed_at"), "transfer.observed_at"))
    buffers = state["config"]["search"]["mode_transition_buffers_minutes"]
    required_minutes = (
        int(buffers.get("flight_to_surface", 0))
        + duration
        + int(buffers.get("surface_to_flight", 0))
    )
    if required_minutes > int(context.get("available_minutes", 0)):
        feasibility = "infeasible"
    transfer = {
        "from_node": from_node,
        "to_node": to_node,
        "approximate_duration_minutes": duration,
        "feasibility": feasibility,
        "evidence_url": evidence_url,
        "observed_at": observed_at,
    }
    state["internal_surface_transfers"] = [
        *[
            item
            for item in state.get("internal_surface_transfers", [])
            if (item.get("from_node"), item.get("to_node"))
            != (from_node, to_node)
        ],
        transfer,
    ]
    task["status"] = VERIFIED_STATUS if feasibility != "infeasible" else "rejected"
    task["terminal_capture_sha256"] = attempt_identity
    task["attempt_count"] = int(task.get("attempt_count", 0)) + 1
    state["search_round"] = int(state.get("search_round", 0)) + 1
    state = _recompute_session(state)
    route_edge_ids = [
        str(edge_id)
        for route in state.get("result", {}).get("routes", [])
        for edge_id in route.get("edge_ids", [])
    ]
    _update_active_hypothesis_after_ingest(state, task, route_edge_ids)
    _publish_internal_surface_transfer_tasks(state, task)
    return compact_session(state)


def ingest_evidence(state: dict[str, Any], raw_bundle: Any) -> dict[str, Any]:
    state = validate_session(state)
    if isinstance(raw_bundle, dict) and raw_bundle.get("bundle_id"):
        bundle_id = str(raw_bundle["bundle_id"])
        existing = next(
            (
                item
                for item in state.get("evidence_bundles", [])
                if isinstance(item, dict) and str(item.get("bundle_id")) == bundle_id
            ),
            None,
        )
        if existing is not None:
            existing_replay = copy.deepcopy(existing)
            raw_replay = copy.deepcopy(raw_bundle)
            # Source fulfillment is derived from the issued question and the
            # normalized Observations.  It is not part of Worker intent and
            # therefore does not participate in replay identity.
            existing_replay.pop("assigned_source_fulfillment", None)
            raw_replay.pop("assigned_source_fulfillment", None)
            if existing_replay == raw_replay:
                return compact_session(state)
            raise LiveProviderError(
                f"duplicate bundle_id has different content: {bundle_id}"
            )
    bundle, task = validate_evidence_bundle(raw_bundle, state)
    _invalidate_finalization(state, "new evidence was ingested")
    _activate_hypothesis(state, task, "AI executed this route hypothesis")
    offer_candidates = [
        _edge_from_observation(bundle, observation)
        for observation in bundle["observations"]
        if observation["evidence_status"] == VERIFIED_STATUS
    ]
    state["edges"], admitted_edge_ids = _merge_offer_edges(
        state["edges"], offer_candidates, state["config"]
    )
    state["nodes"].extend(copy.deepcopy(bundle.get("discovered_nodes", [])))
    statuses = {item["evidence_status"] for item in bundle["observations"]}
    if admitted_edge_ids or VERIFIED_STATUS in statuses:
        task["status"] = VERIFIED_STATUS
    elif statuses == {"no_result"}:
        task["status"] = "no_result"
    else:
        task["status"] = "rejected"
    next_round = int(state.get("search_round", 0)) + 1
    attempt_id = str(bundle.get("bundle_id") or "")
    task_bundle_ids = task.setdefault("attempt_bundle_ids", [])
    if attempt_id and attempt_id not in task_bundle_ids:
        task_bundle_ids.append(attempt_id)
    task["attempt_count"] = len(task_bundle_ids)
    task["attempt_number"] = task["attempt_count"]
    task.pop("last_reopen", None)
    task["terminal_round"] = next_round
    if admitted_edge_ids:
        _publish_internal_surface_transfer_tasks(state, task)
    state["evidence_bundles"].append(bundle)
    state["search_round"] = next_round
    state = _recompute_session(state)
    _update_active_hypothesis_after_ingest(state, task, admitted_edge_ids)
    return compact_session(state)


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    state = validate_session(state)
    statuses: dict[str, int] = {}
    for task in state["research_tasks"]:
        statuses[task["status"]] = statuses.get(task["status"], 0) + 1
    return {
        "valid": True,
        "session_id": state["session_id"],
        "trip_scope": state["trip_scope"],
        "tasks": statuses,
        "research_credits": copy.deepcopy(state["research_credits"]),
        "edges": len(state["edges"]),
        "routes": len(state["result"].get("routes", [])),
        "frontier_exploration": {
            "frontier_count": state.get("frontier_archive", {}).get(
                "frontier_count", 0
            ),
            "quality_cell_count": state.get("frontier_archive", {}).get(
                "quality_cell_count", 0
            ),
            "actionable_count": len(
                state.get("frontier_research_view", {}).get(
                    "frontier_actions", []
                )
            ),
        },
        "active_hypothesis": copy.deepcopy(state.get("active_hypothesis")),
        "hypothesis_next_candidate_id": state.get("hypothesis_tree", {}).get(
            "next_candidate_id"
        ),
        "finalization_status": state.get("finalization_status", "open"),
        "stop_assessment": copy.deepcopy(state.get("stop_assessment")),
        "no_result_stop_assessment": copy.deepcopy(
            state.get("no_result_stop_assessment")
        ),
    }


def _finalization_audit(
    state: dict[str, Any],
    *,
    evaluated_at: datetime | None = None,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if "hypothesis_tree" not in state or "finalization_status" not in state:
        state = validate_session(state)
    tree = _sync_hypothesis_tree(state)
    outcomes = state.get("hypothesis_outcomes", {})
    selected_hypothesis_ids = sorted(
        {
            str(event.get("hypothesis_id"))
            for event in state.get("hypothesis_history", [])
            if isinstance(event, dict)
            and event.get("action") in {"select", "reopen"}
            and event.get("hypothesis_id")
        }
    )
    unresolved_selected = [
        hypothesis_id
        for hypothesis_id in selected_hypothesis_ids
        if not isinstance(outcomes.get(hypothesis_id), dict)
        or outcomes[hypothesis_id].get("status")
        not in HYPOTHESIS_TERMINAL_STATUSES
    ]
    unexplained_pending = [
        str(task.get("task_id"))
        for task in state.get("research_tasks", [])
        if isinstance(task, dict)
        and task.get("status") == "pending"
        and (
            not task.get("hypothesis_id")
            or not _hypothesis_outcome_covers_task(
                outcomes.get(str(task.get("hypothesis_id"))), task
            )
        )
    ]
    open_candidates = [
        str(task.get("task_id"))
        for task in state.get("research_tasks", [])
        if isinstance(task, dict) and task.get("status") == "candidate"
    ]
    issues: list[dict[str, Any]] = []
    active = state.get("active_hypothesis")
    if isinstance(active, dict) and active.get("hypothesis_id"):
        issues.append(
            {
                "code": "active_hypothesis_open",
                "hypothesis_id": active.get("hypothesis_id"),
                "message": "Close or defer the active hypothesis before finalizing.",
            }
        )
    if unexplained_pending:
        issues.append(
            {
                "code": "unexplained_pending_tasks",
                "count": len(unexplained_pending),
                "task_ids": unexplained_pending,
                "message": (
                    "Give each affected hypothesis one complete, hard-dead, or "
                    "deferred-low-value disposition before finalizing."
                ),
            }
        )
    if unresolved_selected:
        issues.append(
            {
                "code": "selected_hypotheses_without_terminal_disposition",
                "count": len(unresolved_selected),
                "hypothesis_ids": unresolved_selected,
                "message": "Every selected hypothesis needs one terminal disposition.",
            }
        )
    if evaluation is None:
        evaluation = _evaluate_solver_result(
            state, evaluated_at or datetime.now(timezone.utc)
        )
    evaluated_result = evaluation["result"]
    fresh_routes = list(evaluated_result.get("routes", []))
    stale_routes = list(state.get("result", {}).get("routes", []))
    excluded_edge_ids = set(evaluation.get("excluded_edge_ids", []))
    stale_route_edge_ids = {
        str(edge_id)
        for route in stale_routes
        for edge_id in route.get("edge_ids", [])
    }
    expired_route_edge_ids = sorted(excluded_edge_ids & stale_route_edge_ids)
    route_count = len(fresh_routes)
    if stale_routes and not fresh_routes and expired_route_edge_ids:
        issues.append(
            {
                "code": "complete_candidate_offer_expired",
                "expired_edge_ids": expired_route_edge_ids,
                "message": "Refresh expired payable Offers before finalizing.",
            }
        )
    if route_count == 0:
        issues.append(
            {
                "code": "no_complete_candidate",
                "message": "A verified complete one-way journey is required for final output.",
            }
        )
    assessment = state.get("stop_assessment")
    dispositions: dict[str, int] = {}
    for outcome in outcomes.values():
        if isinstance(outcome, dict):
            key = str(outcome.get("status", "unknown"))
            dispositions[key] = dispositions.get(key, 0) + 1
    return {
        "can_finalize": not issues,
        "issues": issues,
        "route_count": route_count,
        "evaluation_as_of": evaluated_result.get("evaluation_as_of"),
        "fresh_route_ids": [
            str(route.get("id") or route.get("route_id"))
            for route in fresh_routes
        ],
        "active_hypothesis_id": (
            active.get("hypothesis_id") if isinstance(active, dict) else None
        ),
        "selected_hypothesis_ids": selected_hypothesis_ids,
        "unresolved_selected_hypothesis_ids": unresolved_selected,
        "unexplained_pending_task_ids": unexplained_pending,
        "open_candidate_count": len(open_candidates),
        "open_candidate_task_ids": open_candidates,
        "hypothesis_disposition_counts": dispositions,
        "next_candidate_id": tree.get("next_candidate_id"),
        "stop_assessment": copy.deepcopy(assessment),
        "rule": (
            "A complete candidate is an incumbent, not proof that the whole search is complete. "
            "Candidate tasks remain optional; pending work and selected hypotheses require one "
            "hypothesis-level explanation."
        ),
    }


def _export_result(
    state: dict[str, Any],
    *,
    search_status: str = "draft",
    finalization_reason: str | None = None,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Attach the live external-query ledger without relabeling engine budgets.

    The combination engine also reports deterministic residual/frontier query
    allocations.  Those are internal candidate-selection limits, not browser
    attempts, so the export keeps them in a separately labelled audit block.
    """
    if "hypothesis_tree" not in state or "finalization_status" not in state:
        state = validate_session(state)
    if search_status not in {"draft", "final"}:
        raise LiveProviderError("search_status must be draft or final")
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise LiveProviderError("result evaluation time must include a timezone")
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    evaluation = _evaluate_solver_result(state, evaluated_at)
    finalization = _finalization_audit(
        state,
        evaluated_at=evaluated_at,
        evaluation=evaluation,
    )
    if search_status == "final" and not finalization["can_finalize"]:
        codes = ", ".join(issue["code"] for issue in finalization["issues"])
        raise LiveProviderError(f"finalize-result blocked: {codes}")
    result = copy.deepcopy(evaluation["result"])
    incumbent = _result_incumbent(result)
    incumbent_route = next(
        (
            route
            for route in result.get("routes", [])
            if isinstance(route, dict)
            and isinstance(incumbent, dict)
            and route.get("id") == incumbent.get("route_id")
        ),
        None,
    )
    result["cost_scope"] = (
        copy.deepcopy(incumbent_route.get("cost_scope"))
        if isinstance(incumbent_route, dict)
        else None
    )
    result["search_status"] = search_status
    result["incomplete"] = search_status != "final"
    result["finalization_reason"] = finalization_reason
    ledger = copy.deepcopy(state["research_credits"])
    result["ranking_basis"] = "mandatory_air_ticket_total"
    result["mandatory_air_ticket_total"] = (
        copy.deepcopy(incumbent_route.get("mandatory_air_ticket_total"))
        if isinstance(incumbent_route, dict)
        else None
    )
    result["claim_scope"] = "bounded_verified"
    result["claim"] = (
        "Lowest verified Mandatory Air Ticket Total found within the disclosed "
        "Self-access Gateway Pool, searched scope, and Research Credits."
    )
    result["excluded_costs"] = copy.deepcopy(
        (result.get("cost_scope") or {}).get("excluded_costs", {})
    )
    result["internal_surface_transfers"] = copy.deepcopy(
        incumbent_route.get("internal_surface_transfers", [])
        if isinstance(incumbent_route, dict)
        else []
    )
    selected_offers: list[dict[str, Any]] = []
    price_observations: list[dict[str, Any]] = []
    if isinstance(incumbent_route, dict):
        for edge in incumbent_route.get("edges", []):
            if not isinstance(edge, dict) or edge.get("mode") != "flight":
                continue
            offer_set = edge.get("offer_set")
            if not isinstance(offer_set, dict):
                continue
            selected_offer_id = offer_set.get("selected_offer_id")
            selected_snapshot_id = offer_set.get("selected_snapshot_id")
            offer = next(
                (
                    value
                    for value in offer_set.get("offers", [])
                    if isinstance(value, dict)
                    and value.get("offer_id") == selected_offer_id
                ),
                None,
            )
            snapshot = next(
                (
                    value
                    for value in (offer or {}).get("snapshots", [])
                    if isinstance(value, dict)
                    and value.get("snapshot_id") == selected_snapshot_id
                ),
                None,
            )
            if not isinstance(offer, dict) or not isinstance(snapshot, dict):
                continue
            selected = {
                "edge_id": edge.get("edge_id"),
                "offer_id": selected_offer_id,
                "seller": offer.get("provider"),
                "booking_url": snapshot.get("url"),
                "price": copy.deepcopy(snapshot.get("price")),
                "mandatory_baggage_cost": copy.deepcopy(
                    (snapshot.get("baggage") or {}).get("mandatory_cost")
                ),
                "observed_at": snapshot.get("observed_at"),
                "expires_at": snapshot.get("expires_at"),
            }
            selected_offers.append(selected)
            price_observations.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in {
                        "offer_id": selected_offer_id,
                        "observed_at": snapshot.get("observed_at"),
                        "expires_at": snapshot.get("expires_at"),
                    }.items()
                    if value is not None
                }
            )
    result["selected_offers"] = selected_offers
    result["price_observations"] = price_observations
    result["freshness"] = {
        "evaluated_at": _iso(evaluated_at),
        "status": "fresh" if incumbent_route is not None else "no_winner",
        "offers": copy.deepcopy(price_observations),
    }
    warnings: list[dict[str, Any]] = []
    if isinstance(incumbent_route, dict):
        if int(incumbent_route.get("overnight_count", 0)):
            warnings.append(
                {
                    "code": "overnight_wait",
                    "count": int(incumbent_route["overnight_count"]),
                }
            )
        if int(incumbent_route.get("independent_ticket_transfer_count", 0)):
            warnings.append(
                {
                    "code": "unprotected_self_transfer",
                    "count": int(
                        incumbent_route["independent_ticket_transfer_count"]
                    ),
                }
            )
        for transfer in incumbent_route.get("internal_surface_transfers", []):
            if (
                isinstance(transfer, dict)
                and transfer.get("feasibility") == "fallback_plausible"
            ):
                warnings.append(
                    {
                        "code": "surface_transfer_fallback_plausible",
                        "from_node": transfer.get("from_node"),
                        "to_node": transfer.get("to_node"),
                    }
                )
        if (incumbent_route.get("cost_scope") or {}).get(
            "gateway_complete_cost_status"
        ) == "incomplete_unpriced_transfer":
            warnings.append({"code": "unpriced_internal_surface_transfer"})
    result["warnings"] = warnings
    result["search_stopping"] = {
        "disposition": (
            "budget_stop"
            if int(ledger.get("remaining", 0)) <= 0
            else "threshold_stop"
        ),
        "search_effort": ledger["search_effort"],
        "research_credit_budget": ledger["research_credit_budget"],
        "research_credits_used": ledger["used"],
        "research_credits_remaining": ledger["remaining"],
        "price_challenge_threshold": ledger["price_challenge_threshold"],
    }
    origin_gateways = set(
        state.get("profile", {}).get("origin_airport_nodes", [])
    )
    destination_gateways = set(
        state.get("profile", {}).get("destination_airport_nodes", [])
    )
    for route in result.get("routes", []):
        edges = route.get("edges", []) if isinstance(route, dict) else []
        flight_edges = [
            edge
            for edge in edges
            if isinstance(edge, dict) and edge.get("mode") == "flight"
        ]
        if flight_edges:
            origin_gateways.add(str(flight_edges[0].get("from_node")))
            destination_gateways.add(str(flight_edges[-1].get("to_node")))
    result["searched_scope"] = {
        "self_access_gateway_pool": {
            "origin": sorted(value for value in origin_gateways if value),
            "destination": sorted(value for value in destination_gateways if value),
        },
        "search_effort": ledger["search_effort"],
    }
    result["unsearched_scope"] = {
        "claim": (
            "The winner is bounded by the disclosed scope and Research Credit "
            "budget, not a global minimum."
        ),
        "unsearched_task_ids": [
            str(task.get("task_id"))
            for task in state.get("research_tasks", [])
            if task.get("status") in {"candidate", "pending"}
        ],
        "unresolved_low_price_signals": [],
        "unresolved_task_ids": [
            str(task.get("task_id"))
            for task in state.get("research_tasks", [])
            if task.get("status") in {"rejected", "no_result"}
        ],
    }
    statuses: dict[str, int] = {}
    for task in state["research_tasks"]:
        status = str(task.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    result["live_provider_session_audit"] = {
        "session_id": state["session_id"],
        "trip_scope": state["trip_scope"],
        "source_selection_mode": _source_selection_mode(state["config"]),
        "search_round": int(state.get("search_round", 0)),
        "research_credits": {
            "budget_scope": "new_external_research",
            "search_effort": ledger["search_effort"],
            "budget": ledger["research_credit_budget"],
            "used": ledger["used"],
            "reserved": ledger["reserved"],
            "remaining": ledger["remaining"],
            "price_challenge_threshold": ledger[
                "price_challenge_threshold"
            ],
            "pending": statuses.get("pending", 0),
            "task_status_counts": statuses,
            "entries": copy.deepcopy(ledger.get("entries", {})),
        },
        "internal_engine_query_budgets": {
            "budget_scope": (
                "deterministic_internal_candidate_selection_not_browser_attempts"
            ),
            "residual_budget_search": copy.deepcopy(
                result.get("residual_search", {}).get("query_budget")
            ),
            "frontier_search": copy.deepcopy(
                result.get("frontier_coverage", {}).get("query_budget")
            ),
        },
        "provider_coverage": copy.deepcopy(state.get("provider_coverage_audit", {})),
        "frontier_exploration": {
            "archive": copy.deepcopy(state.get("frontier_archive", {})),
            "research_view": copy.deepcopy(
                state.get("frontier_research_view", {})
            ),
            "selection_history": copy.deepcopy(
                state.get("frontier_history", [])
            ),
        },
        "active_hypothesis": copy.deepcopy(state.get("active_hypothesis")),
        "hypothesis_history": copy.deepcopy(state.get("hypothesis_history", [])),
        "hypothesis_outcomes": copy.deepcopy(state.get("hypothesis_outcomes", {})),
        "ordered_hypothesis_tree": copy.deepcopy(state.get("hypothesis_tree", {})),
        "finalization": {
            **copy.deepcopy(finalization),
            "search_status": search_status,
            "incomplete": search_status != "final",
            "reason": finalization_reason,
            "history": copy.deepcopy(state.get("finalization_history", [])),
        },
        "stop_assessment": copy.deepcopy(state.get("stop_assessment")),
        "no_result_stop_assessment": copy.deepcopy(
            state.get("no_result_stop_assessment")
        ),
    }
    return result


def record_stop_assessment(
    state: dict[str, Any],
    reason: str,
    challenger_hypothesis_ids: list[str] | None = None,
) -> dict[str, Any]:
    state = validate_session(state)
    reason = _text(reason, "stop assessment reason")
    if not state.get("result", {}).get("routes"):
        raise LiveProviderError("stop assessment requires a complete verified route")
    challengers = list(challenger_hypothesis_ids or [])
    tree = _sync_hypothesis_tree(state)
    unknown = [item for item in challengers if item not in tree["nodes"]]
    if unknown:
        raise LiveProviderError(f"unknown challenger hypotheses: {unknown}")
    unfinished = [
        item
        for item in challengers
        if tree["nodes"][item].get("status") not in HYPOTHESIS_TERMINAL_STATUSES
    ]
    if unfinished:
        raise LiveProviderError(f"challenger hypotheses are unfinished: {unfinished}")
    state["stop_assessment"] = {
        "reason": reason,
        "challenger_hypothesis_ids": challengers,
        "search_round": int(state.get("search_round", 0)),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return compact_session(state)


def record_no_result_stop(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Persist an AI stop decision when no complete verified route exists."""

    state = validate_session(state)
    reason = _text(reason, "no-result stop reason")
    if state.get("result", {}).get("routes"):
        raise LiveProviderError("no-result stop is only valid without a complete route")
    if state.get("active_hypothesis") is not None:
        raise LiveProviderError("no-result stop requires no active hypothesis")
    tree = _sync_hypothesis_tree(state)
    selected = {
        str(item.get("hypothesis_id"))
        for item in state.get("hypothesis_history", [])
        if item.get("action") == "select" and item.get("hypothesis_id")
    }
    unfinished = sorted(
        hypothesis_id
        for hypothesis_id in selected
        if tree["nodes"].get(hypothesis_id, {}).get("status")
        not in HYPOTHESIS_TERMINAL_STATUSES
    )
    if unfinished:
        raise LiveProviderError(
            f"no-result stop requires terminal selected hypotheses: {unfinished}"
        )
    state["no_result_stop_assessment"] = {
        "reason": reason,
        "search_round": int(state.get("search_round", 0)),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return compact_session(state)


def finalize_result(
    state: dict[str, Any], reason: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = validate_session(state)
    reason = _text(reason, "finalization reason")
    evaluated_at = datetime.now(timezone.utc)
    evaluation = _evaluate_solver_result(state, evaluated_at)
    audit = _finalization_audit(
        state,
        evaluated_at=evaluated_at,
        evaluation=evaluation,
    )
    if not audit["can_finalize"]:
        codes = ", ".join(issue["code"] for issue in audit["issues"])
        raise LiveProviderError(f"finalize-result blocked: {codes}")
    state["finalization_status"] = "final"
    event = {
        "sequence": len(state.setdefault("finalization_history", [])) + 1,
        "search_round": int(state.get("search_round", 0)),
        "reason": reason,
        "route_count": audit["route_count"],
        "selected_hypothesis_ids": copy.deepcopy(
            audit["selected_hypothesis_ids"]
        ),
        "open_candidate_count": audit["open_candidate_count"],
        "next_candidate_id": audit["next_candidate_id"],
        "evaluated_at": _iso(evaluated_at),
    }
    state["finalization_history"].append(event)
    state.pop("finalization_invalidated_reason", None)
    result = _export_result(
        state,
        search_status="final",
        finalization_reason=reason,
        evaluated_at=evaluated_at,
    )
    state.setdefault("finalization", {})["evaluated_at"] = _iso(evaluated_at)
    return compact_session(state), result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist a one-way live-browser Provider Product Session"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--config", required=True)
    initialize.add_argument("--state", required=True)
    tasks = sub.add_parser("tasks")
    tasks.add_argument("--state", required=True)
    tasks.add_argument(
        "--status",
        choices=[
            "all",
            "candidate",
            "pending",
            VERIFIED_STATUS,
            "no_result",
            "rejected",
            "superseded",
        ],
        default="pending",
    )
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--evidence", required=True)
    hypothesis = sub.add_parser("hypothesis")
    hypothesis.add_argument("--state", required=True)
    hypothesis.add_argument(
        "--action",
        required=True,
        choices=[
            "select",
            "complete",
            "hard_dead",
            "deferred_low_value",
        ],
    )
    hypothesis.add_argument("--task-id")
    hypothesis.add_argument("--hypothesis-id")
    hypothesis.add_argument("--parent-hypothesis-id")
    hypothesis.add_argument("--reason", required=True)
    frontier = sub.add_parser("frontier")
    frontier.add_argument("--state", required=True)
    frontier.add_argument("--frontier-id", required=True)
    frontier.add_argument("--reason", required=True)
    draft = sub.add_parser("export-draft")
    draft.add_argument("--state", required=True)
    draft.add_argument("--output", required=True)
    legacy_export = sub.add_parser("export-result")
    legacy_export.add_argument("--state", required=True)
    legacy_export.add_argument("--output", required=True)
    finalize = sub.add_parser("finalize-result")
    finalize.add_argument("--state", required=True)
    finalize.add_argument("--output", required=True)
    finalize.add_argument("--reason", required=True)
    stop = sub.add_parser("stop-assessment")
    stop.add_argument("--state", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--challenger-hypothesis-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            state = initialize_session(args.config)
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
            return 0
        state = validate_session(read_json(args.state))
        if args.command == "tasks":
            tasks = state["research_tasks"]
            if args.status != "all":
                tasks = [task for task in tasks if task["status"] == args.status]
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
        elif args.command == "ingest":
            state = ingest_evidence(state, read_json(args.evidence))
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
        elif args.command == "hypothesis":
            state = record_hypothesis_decision(
                state,
                args.action,
                args.reason,
                task_id=args.task_id,
                hypothesis_id=args.hypothesis_id,
                parent_hypothesis_id=args.parent_hypothesis_id,
            )
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
        elif args.command == "frontier":
            state = publish_frontier_research_task(
                state,
                args.frontier_id,
                args.reason,
            )
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
        elif args.command in {"export-draft", "export-result"}:
            export_result = _export_result(state, search_status="draft")
            write_json_atomic(args.output, export_result)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "search_status": "draft",
                        "incomplete": True,
                        "output": _display_path(args.output),
                        "routes": len(export_result.get("routes", [])),
                        "legacy_command": args.command == "export-result",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "finalize-result":
            state, export_result = finalize_result(state, args.reason)
            write_json_atomic(args.state, state)
            write_json_atomic(args.output, export_result)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "search_status": "final",
                        "incomplete": False,
                        "output": _display_path(args.output),
                        "routes": len(export_result.get("routes", [])),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "stop-assessment":
            state = record_stop_assessment(
                state, args.reason, args.challenger_hypothesis_id
            )
            write_json_atomic(args.state, state)
            print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
        return 0
    except (LiveProviderError, ENGINE.PlannerError) as exc:
        print(f"live_provider_session: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
