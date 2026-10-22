"""Shared validation Interface for Product-owned journey cost scope summaries."""

from __future__ import annotations

import copy
import math
from typing import Any

from .contracts import HarnessProtocolError


COST_SCOPE_VERSION = "1.0"
ENDPOINT_POLICIES = {"anchor_required", "any_admitted_gateway"}
ENDPOINT_ROLE_STATUSES = {"verified", "missing", "not_required", "not_applicable"}
ENDPOINT_ROLES = (
    "origin_access",
    "origin_egress",
    "destination_access",
    "destination_egress",
)
COMPLETION_SCOPES = {
    ("anchor_required", "anchor_required"): "anchor_to_anchor",
    ("anchor_required", "any_admitted_gateway"): "origin_anchor_to_destination_gateway",
    ("any_admitted_gateway", "anchor_required"): "origin_gateway_to_destination_anchor",
    ("any_admitted_gateway", "any_admitted_gateway"): "gateway_to_gateway",
}


class CostScopeError(HarnessProtocolError):
    pass


def _money(raw: Any, field: str, currency: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CostScopeError(f"{field} must be a money object")
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise CostScopeError(f"{field}.amount must be numeric")
    amount = float(amount)
    if not math.isfinite(amount) or amount < 0:
        raise CostScopeError(f"{field}.amount must be finite and non-negative")
    if raw.get("currency") != currency:
        raise CostScopeError(f"{field}.currency must equal {currency}")
    return {"amount": round(amount, 2), "currency": currency}


def _optional_money(raw: Any, field: str, currency: str) -> dict[str, Any] | None:
    return None if raw is None else _money(raw, field, currency)


def _required_roles(
    journey_shape: str,
    origin_policy: str,
    destination_policy: str,
) -> set[str]:
    roles = set()
    if origin_policy == "anchor_required":
        roles.add("origin_access")
        if journey_shape == "round_trip":
            roles.add("origin_egress")
    if destination_policy == "anchor_required":
        roles.add("destination_egress")
        if journey_shape == "round_trip":
            roles.add("destination_access")
    return roles


def normalize_cost_scope(raw: Any, *, base_currency: str) -> dict[str, Any]:
    """Validate and normalize one Product-owned Cost Scope Summary."""

    if not isinstance(raw, dict):
        raise CostScopeError("cost_scope must be an object")
    if raw.get("cost_scope_version", COST_SCOPE_VERSION) != COST_SCOPE_VERSION:
        raise CostScopeError(f"cost_scope_version must be {COST_SCOPE_VERSION}")
    journey_shape = raw.get("journey_shape")
    if journey_shape not in {"one_way", "round_trip"}:
        raise CostScopeError("cost_scope.journey_shape must be one_way or round_trip")
    origin_policy = raw.get("origin_endpoint_policy")
    destination_policy = raw.get("destination_endpoint_policy")
    if origin_policy not in ENDPOINT_POLICIES:
        raise CostScopeError("cost_scope.origin_endpoint_policy is unsupported")
    if destination_policy not in ENDPOINT_POLICIES:
        raise CostScopeError("cost_scope.destination_endpoint_policy is unsupported")
    currency = str(base_currency or "").upper()
    if len(currency) != 3:
        raise CostScopeError("base_currency must be a three-letter code")
    if raw.get("base_currency", currency) != currency:
        raise CostScopeError("cost_scope.base_currency is inconsistent")
    gateway_complete = _money(
        raw.get("gateway_complete_cost"),
        "cost_scope.gateway_complete_cost",
        currency,
    )
    raw_roles = raw.get("endpoint_roles")
    if not isinstance(raw_roles, dict) or set(raw_roles) != set(ENDPOINT_ROLES):
        raise CostScopeError(
            "cost_scope.endpoint_roles must contain every canonical Endpoint role"
        )
    required = _required_roles(journey_shape, origin_policy, destination_policy)
    normalized_roles: dict[str, dict[str, Any]] = {}
    verified_required_total = 0.0
    required_complete = True
    for role in ENDPOINT_ROLES:
        value = raw_roles.get(role)
        if not isinstance(value, dict):
            raise CostScopeError(f"cost_scope.endpoint_roles.{role} must be an object")
        status = value.get("status")
        if status not in ENDPOINT_ROLE_STATUSES:
            raise CostScopeError(
                f"cost_scope.endpoint_roles.{role}.status is unsupported"
            )
        if role in required and status not in {"verified", "missing"}:
            raise CostScopeError(f"required Endpoint role {role} has invalid status")
        if role not in required and status not in {"not_required", "not_applicable"}:
            raise CostScopeError(f"non-required Endpoint role {role} has invalid status")
        cost = _optional_money(
            value.get("cost"), f"cost_scope.endpoint_roles.{role}.cost", currency
        )
        if status == "verified":
            if cost is None:
                raise CostScopeError(f"verified Endpoint role {role} requires cost")
            if role in required:
                verified_required_total += float(cost["amount"])
        elif cost is not None:
            raise CostScopeError(f"Endpoint role {role} may carry cost only when verified")
        if role in required and status != "verified":
            required_complete = False
        normalized_roles[role] = {
            "status": status,
            "cost": cost,
            **(
                {"provenance": copy.deepcopy(value["provenance"])}
                if value.get("provenance") is not None
                else {}
            ),
        }

    derived_endpoint = (
        {"amount": round(verified_required_total, 2), "currency": currency}
        if required_complete
        else None
    )
    supplied_endpoint = _optional_money(
        raw.get("endpoint_completion_cost"),
        "cost_scope.endpoint_completion_cost",
        currency,
    )
    if supplied_endpoint != derived_endpoint:
        raise CostScopeError(
            "cost_scope.endpoint_completion_cost does not match verified Endpoint roles"
        )
    expected_completion_status = "complete" if required_complete else "incomplete"
    if raw.get("endpoint_completion_status", expected_completion_status) != expected_completion_status:
        raise CostScopeError(
            "cost_scope.endpoint_completion_status is inconsistent with Endpoint roles"
        )
    derived_comparison = (
        {
            "amount": round(
                float(gateway_complete["amount"])
                + float(derived_endpoint["amount"]),
                2,
            ),
            "currency": currency,
        }
        if derived_endpoint is not None
        else None
    )
    supplied_comparison = _optional_money(
        raw.get("comparison_total"), "cost_scope.comparison_total", currency
    )
    if supplied_comparison != derived_comparison:
        raise CostScopeError(
            "cost_scope.comparison_total does not match its declared cost scope"
        )
    anchor_complete = _optional_money(
        raw.get("anchor_complete_cost"),
        "cost_scope.anchor_complete_cost",
        currency,
    )
    expected_anchor = (
        derived_comparison
        if origin_policy == destination_policy == "anchor_required"
        else None
    )
    if anchor_complete != expected_anchor:
        raise CostScopeError(
            "cost_scope.anchor_complete_cost is inconsistent with Endpoint Policies"
        )
    completion_scope = COMPLETION_SCOPES[(origin_policy, destination_policy)]
    if raw.get("completion_scope") != completion_scope:
        raise CostScopeError(
            "cost_scope.completion_scope is inconsistent with Endpoint Policies"
        )
    return {
        "cost_scope_version": COST_SCOPE_VERSION,
        "journey_shape": journey_shape,
        "base_currency": currency,
        "origin_endpoint_policy": origin_policy,
        "destination_endpoint_policy": destination_policy,
        "completion_scope": completion_scope,
        "gateway_complete_cost": gateway_complete,
        "endpoint_roles": normalized_roles,
        "endpoint_completion_status": expected_completion_status,
        "endpoint_completion_cost": derived_endpoint,
        "anchor_complete_cost": anchor_complete,
        "comparison_total": derived_comparison,
    }
