"""Compact worker-facing projections with immutable refs to full WorkItems."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

PROJECTION_VERSION = "2.0"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def project_item(
    item: dict[str, Any], *, full_item_ref: str, full_item_hash: str
) -> dict[str, Any]:
    payload = item.get("payload", {})
    projected_payload: dict[str, Any] = {
        "task_id": payload.get("task_id"),
        "hypothesis_id": payload.get("hypothesis_id"),
        "research_kind": payload.get("research_kind"),
        "journey_scope": payload.get("journey_scope"),
    }
    if item.get("kind") == "choice":
        projected_payload.update(
            {
                "actions": [_compact_action(value) for value in payload.get("actions", [])],
                "control_summary": _compact_control(payload.get("control_summary", {})),
            }
        )
    else:
        projected_payload["action"] = copy.deepcopy(payload.get("action"))
        projected_payload["work"] = project_work(
            payload.get("work", {}),
            research_kind=payload.get("research_kind"),
            journey_scope=payload.get("journey_scope"),
        )
    return {
        "item_version": "3.0",
        "item_id": item["item_id"],
        "kind": item["kind"],
        "full_item_ref": full_item_ref,
        "full_item_hash": full_item_hash,
        "payload": projected_payload,
    }


def project_work(
    work: dict[str, Any],
    *,
    research_kind: Any = None,
    journey_scope: Any = None,
) -> dict[str, Any]:
    brief = work.get("brief", {}) if isinstance(work.get("brief"), dict) else {}
    query = _first_object(
        brief.get("request"),
        brief.get("query_shape"),
        brief.get("search_scope"),
        brief.get("query_scope_ladder"),
    )
    provider = brief.get("provider_foundation", {})
    required_source = provider.get("required_source") if isinstance(provider, dict) else None
    if required_source is None and isinstance(brief.get("assigned_source"), dict):
        assigned = brief["assigned_source"]
        if assigned.get("source_id"):
            required_source = {
                "source_id": assigned["source_id"],
                "binding": True,
            }
    if required_source is None and isinstance(brief.get("request"), dict):
        source_id = brief["request"].get("required_source_id")
        if isinstance(source_id, str) and source_id:
            required_source = {"source_id": source_id, "binding": True}
    sources = brief.get("source_options") or brief.get("source_suggestions") or []
    source_projection = []
    for source in sources[:2] if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        source_projection.append(
            {
                key: copy.deepcopy(source.get(key))
                for key in (
                    "source_id",
                    "name",
                    "url",
                    "evidence_role",
                    "admission_policy",
                    "binding",
                )
                if source.get(key) is not None
            }
        )
    traveler_requirements = (
        brief.get("traveler_requirements")
        if isinstance(brief.get("traveler_requirements"), dict)
        else {}
    )
    resolved_research_kind = str(
        research_kind
        or brief.get("research_kind")
        or brief.get("request_type")
        or brief.get("task_family")
        or ""
    )
    compact_query = _compact_query(query, resolved_research_kind)
    if brief.get("candidate_modes") is not None:
        compact_query["allowed_modes"] = copy.deepcopy(brief["candidate_modes"])
    traveler_count = copy.deepcopy(
        brief["traveler_count"]
        if brief.get("traveler_count") is not None
        else traveler_requirements.get("traveler_count")
        if traveler_requirements.get("traveler_count") is not None
        else (query.get("traveler_count") if isinstance(query, dict) else None)
    )
    baggage_policy = copy.deepcopy(
        brief["required_baggage"]
        if brief.get("required_baggage") is not None
        else brief["baggage_policy"]
        if brief.get("baggage_policy") is not None
        else traveler_requirements.get("baggage")
        if traveler_requirements.get("baggage") is not None
        else (query.get("baggage_policy") if isinstance(query, dict) else None)
    )
    overnight_allowed = copy.deepcopy(
        brief.get("overnight_allowed")
        if brief.get("overnight_allowed") is not None
        else traveler_requirements.get("overnight_allowed")
        if traveler_requirements.get("overnight_allowed") is not None
        else query.get("overnight_allowed")
        if isinstance(query, dict)
        else None
    )
    self_transfer_policy = copy.deepcopy(
        brief.get("self_transfer_policy")
        if brief.get("self_transfer_policy") is not None
        else traveler_requirements.get("self_transfer_policy")
        if traveler_requirements.get("self_transfer_policy") is not None
        else query.get("self_transfer_policy")
        if isinstance(query, dict)
        else None
    )
    assigned_source = _compact_assigned_source(brief.get("assigned_source"))
    evidence_rule = copy.deepcopy(brief.get("evidence_rule"))
    required_source_batch = compact_query.get("required_source_batch")
    provider_batch_protocol = (
        {
            "binding": True,
            "required_source_batch": copy.deepcopy(required_source_batch),
            "execution_and_stop_rule": evidence_rule,
        }
        if isinstance(required_source_batch, list) and required_source_batch
        else None
    )
    evidence_contract = brief.get("evidence_contract")
    temporal_research_option = (
        copy.deepcopy(evidence_contract.get("temporal_research_option"))
        if isinstance(evidence_contract, dict)
        else None
    )
    projection = {
        "projection_version": PROJECTION_VERSION,
        "work_version": work.get("work_version"),
        "product_id": work.get("product_id"),
        "task_id": work.get("task_id"),
        "hypothesis_id": work.get("hypothesis_id"),
        "research_kind": resolved_research_kind or None,
        "task_family": copy.deepcopy(brief.get("task_family")),
        "request_type": copy.deepcopy(brief.get("request_type")),
        "journey_scope": copy.deepcopy(journey_scope),
        "objective": _objective(brief),
        "query": compact_query,
        "query_scope_ladder": copy.deepcopy(brief.get("query_scope_ladder")),
        "corridor_context": copy.deepcopy(brief.get("corridor_context")),
        "frontier_context": copy.deepcopy(brief.get("frontier_context")),
        "time_window": copy.deepcopy(
            brief.get("time_window")
            or (query.get("travel_windows") if isinstance(query, dict) else None)
        ),
        "date_search": copy.deepcopy(
            brief.get("date_search")
            or (query.get("date_search") if isinstance(query, dict) else None)
        ),
        "traveler_count": traveler_count,
        "required_baggage": baggage_policy,
        "overnight_allowed": overnight_allowed,
        "self_transfer_policy": self_transfer_policy,
        "traveler_requirements": {
            "traveler_count": traveler_count,
            "baggage_policy": copy.deepcopy(baggage_policy),
            "overnight_allowed": overnight_allowed,
            "self_transfer_policy": self_transfer_policy,
        },
        "assigned_source": assigned_source,
        "required_source": copy.deepcopy(required_source),
        "source_options": source_projection,
        "provider_kinds": copy.deepcopy(brief.get("provider_kinds")),
        "price_improvement": copy.deepcopy(brief.get("price_improvement")),
        "price_ordering": copy.deepcopy(brief.get("price_ordering")),
        "capture_requirements": copy.deepcopy(brief.get("must_capture")),
        "conditional_requirements": copy.deepcopy(
            brief.get("conditional_requirements")
        ),
        "no_result": copy.deepcopy(brief.get("no_result")),
        "offer_collection_policy": copy.deepcopy(
            brief.get("offer_collection_policy")
        ),
        "evidence_rule": evidence_rule,
        "provider_batch_protocol": provider_batch_protocol,
        "temporal_execution": copy.deepcopy(brief.get("temporal_execution")),
        "temporal_research_option": temporal_research_option,
        "admission": _compact_contract(evidence_contract),
        "terminal_admission": _compact_contract(brief.get("terminal_evidence_contract")),
        "attempt_schema": copy.deepcopy(work.get("attempt_schema")),
    }
    projection["execution"] = execution_metadata(projection)
    return projection


def execution_metadata(projection: dict[str, Any]) -> dict[str, Any]:
    query = projection.get("query") if isinstance(projection.get("query"), dict) else {}
    research_kind = str(projection.get("research_kind") or "")
    provider = projection.get("assigned_source") or projection.get("required_source") or {}
    provider_id = str(provider.get("source_id") or "ai-selected")
    side = query.get("side")
    common_query = copy.deepcopy(query)
    for key in (
        "required_candidate_node_id",
        "candidate_node",
        "candidate_roles",
        "inclusion_reason",
        "coverage_partition_id",
        "logical_request_id",
        "request_id",
        "parent_id",
    ):
        common_query.pop(key, None)
    batchable = (
        research_kind == "gateway_discovery"
        or projection.get("product_id") == "random_loop"
    ) and provider_id != "ai-selected"
    batch_basis = {
        "product_id": projection.get("product_id"),
        "research_kind": research_kind,
        "task_family": projection.get("task_family"),
        "request_type": projection.get("request_type"),
        "journey_scope": projection.get("journey_scope"),
        "provider_id": provider_id,
        "side": side,
        "query": common_query,
        "query_scope_ladder": projection.get("query_scope_ladder"),
        "corridor_context": projection.get("corridor_context"),
        "time_window": projection.get("time_window"),
        "date_search": projection.get("date_search"),
        "traveler_requirements": projection.get("traveler_requirements"),
        "required_source_batch": query.get("required_source_batch"),
        "provider_batch_protocol": projection.get("provider_batch_protocol"),
        "temporal_execution": projection.get("temporal_execution"),
        "temporal_research_option": projection.get("temporal_research_option"),
    }
    return {
        "provider_id": provider_id,
        "batchable": batchable,
        "batch_key": digest(batch_basis) if batchable else None,
    }


def _objective(brief: dict[str, Any]) -> str:
    for value in (
        brief.get("task_purpose"),
        brief.get("question"),
        (brief.get("evidence_contract") or {}).get("rule")
        if isinstance(brief.get("evidence_contract"), dict)
        else None,
        brief.get("research_kind"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Execute the bounded product research task and return admissible evidence."


def _compact_query(query: dict[str, Any], research_kind: str) -> dict[str, Any]:
    if research_kind in {
        "region_scope_definition",
        "structural_challenger_definition",
    }:
        return copy.deepcopy(query)
    allowed = {
        "trip_type",
        "direction",
        "side",
        "required_candidate_node_id",
        "candidate_node",
        "origins",
        "destinations",
        "from_node",
        "to_node",
        "outbound_departure_window",
        "return_departure_window",
        "departure_window",
        "travel_windows",
        "earliest_departure",
        "latest_departure",
        "date_search",
        "request_kind",
        "request_id",
        "parent_id",
        "must_return",
        "remaining_legs",
        "remaining_trip_minutes",
        "per_leg_price_cap",
        "route_deadline",
        "coverage_partition_id",
        "logical_request_id",
        "required_source_id",
        "required_source_batch",
        "allowed_modes",
        "pool_constraints",
        "schedule_context",
        "traveler_count",
        "baggage_policy",
        "base_currency",
        "fx_to_base",
        "overnight_allowed",
        "self_transfer_policy",
        "from",
        "to",
        "ai_selected_exact_target",
    }
    return {
        key: copy.deepcopy(value)
        for key, value in query.items()
        if key in allowed
    }


def _compact_assigned_source(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    allowed = {
        "source_id",
        "name",
        "url",
        "source_type",
        "execution_kind",
        "admission_policy",
        "fulfillment_rule",
        "binding",
    }
    result = {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key in allowed and value is not None
    }
    return result or None


def _compact_contract(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    allowed = {
        "evidence_status",
        "proposal_type",
        "rule",
        "admission_role",
        "research_only_source_requires_payable_offer_verification",
        "fields",
    }
    return {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key in allowed
    }


def _compact_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    return {
        key: copy.deepcopy(value)
        for key, value in action.items()
        if key
        in {
            "kind",
            "action_key",
            "task_id",
            "hypothesis_id",
            "challenger_id",
            "proposal_id",
            "disposition",
            "status",
            "question",
            "binding",
            "automatic",
        }
    }


def _compact_control(control: Any) -> dict[str, Any]:
    if not isinstance(control, dict):
        return {}
    return {
        key: copy.deepcopy(control.get(key))
        for key in (
            "product_id",
            "active_hypothesis_id",
            "has_complete_result",
            "can_finalize",
            "finalization_issues",
            "browser_attempt_count",
        )
        if control.get(key) is not None
    }


def _first_object(*values: Any) -> dict[str, Any]:
    return next((value for value in values if isinstance(value, dict)), {})
