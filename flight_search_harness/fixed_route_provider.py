"""Shared fixed-route flight Provider foundation for one-way and round-trip."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlparse

from .contracts import HarnessProtocolError


PRIMARY_FLIGHT_SOURCE_ID = "google-flights"
MAX_SOURCE_HINTS = 3
PRICE_SELECTION_METHODS = {
    "lowest_price_view",
    "price_ascending",
    "all_visible_compared",
}


def _compact_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(source[key])
        for key in (
            "source_id",
            "name",
            "url",
            "source_type",
            "evidence_role",
            "admission_policy",
            "binding",
            "activation_state",
        )
        if source.get(key) is not None
    }


def attempted_source_ids(state: dict[str, Any], task_id: str) -> set[str]:
    result: set[str] = set()
    for bundle in state.get("evidence_bundles", []):
        if not isinstance(bundle, dict):
            continue
        bundle_task_id = bundle.get("task_id") or bundle.get("request_id")
        if str(bundle_task_id) != task_id:
            continue
        result.update(
            _observed_source_ids(
                {"product_payload": {"observations": bundle.get("observations", [])}}
            )
        )
    return result


def apply_foundation(
    raw_brief: dict[str, Any], *, attempted_sources: set[str]
) -> dict[str, Any]:
    brief = copy.deepcopy(raw_brief)
    google_option: dict[str, Any] | None = None
    for field in ("source_suggestions", "source_options"):
        options = brief.get(field)
        if not isinstance(options, list):
            continue
        compact = [
            _compact_source(item)
            for item in options
            if isinstance(item, dict) and item.get("source_id")
        ]
        for item in compact:
            if item.get("source_id") == PRIMARY_FLIGHT_SOURCE_ID:
                google_option = copy.deepcopy(item)
        brief[field] = compact[:MAX_SOURCE_HINTS]

    applies_to_flight_task = google_option is not None
    ladder = brief.get("query_scope_ladder", {})
    scopes = ladder.get("scope_ladder", []) if isinstance(ladder, dict) else []
    expanding_scopes = [
        item
        for item in scopes
        if isinstance(item, dict) and item.get("expands_known_pool") is True
    ]
    google_supports_required_scope = True
    if expanding_scopes:
        google_supports_required_scope = any(
            PRIMARY_FLIGHT_SOURCE_ID
            in item.get("source_support", {}).get("supported", [])
            for item in expanding_scopes
        )
    first_attempt = (
        applies_to_flight_task
        and google_supports_required_scope
        and not attempted_sources
    )
    required = None
    if first_attempt:
        required = {
            "source_id": PRIMARY_FLIGHT_SOURCE_ID,
            "name": google_option.get("name", "Google Flights"),
            "url": google_option.get("url", "https://www.google.com/travel/flights"),
            "binding": True,
        }
    dynamic = brief.get("dynamic_source_rules")
    if isinstance(dynamic, list):
        brief["dynamic_source_rules"] = [
            _compact_source(item)
            for item in dynamic
            if isinstance(item, dict) and item.get("activation_state") == "applicable"
        ][:MAX_SOURCE_HINTS]
    source_policy = brief.get("source_policy")
    if isinstance(source_policy, dict):
        brief["source_policy"] = {
            "research_only_source_requires_payable_offer_verification": bool(
                source_policy.get(
                    "research_only_source_requires_payable_offer_verification",
                    False,
                )
            )
        }
    brief["provider_foundation"] = {"required_source": copy.deepcopy(required)}
    brief["price_ordering"] = {
        "objective": "lowest_total_price",
        "required_evidence_field": "price_selection_method",
        "rule": (
            "Before capturing a fare, establish the lowest compatible result "
            "through the Provider's lowest-price view, ascending price sort, "
            "or comparison of all visible Browser results. Default "
            "recommended ordering and an unopened lower-price label are not "
            "lowest-price evidence."
        ),
    }
    return brief


def validate_first_attempt(
    attempt: dict[str, Any], *, required_source: dict[str, Any] | None
) -> None:
    if not required_source:
        return
    expected = str(required_source["source_id"])
    observed = _observed_source_ids(attempt)
    if expected not in observed or (
        observed - {expected}
        and not _required_source_closed_before_fallback(attempt, expected)
    ):
        raise HarnessProtocolError(
            f"fixed-route first flight attempt requires source_id={expected}; "
            f"observed={sorted(observed) or ['<missing>']}"
        )


def _required_source_closed_before_fallback(
    attempt: dict[str, Any], expected_source_id: str
) -> bool:
    payload = attempt.get("product_payload")
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        return False
    expected_terminal = False
    fallback_verified = False
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        source_ids = _observed_source_ids(
            {"product_payload": {"observations": [observation]}}
        )
        status = observation.get("evidence_status")
        if expected_source_id in source_ids and status in {"no_result", "rejected"}:
            expected_terminal = True
        elif source_ids - {expected_source_id} and status == "verified_observed":
            fallback_verified = True
        elif source_ids - {expected_source_id}:
            return False
    return expected_terminal and fallback_verified


def validate_price_ordering(attempt: dict[str, Any]) -> None:
    """Require an explicit lowest-price view for every fare-bearing flight fact."""

    payload = attempt.get("product_payload")
    observations = (
        payload.get("observations")
        if isinstance(payload, dict)
        else attempt.get("observations")
    )
    if not isinstance(observations, list):
        return
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or not _fare_bearing_flight(observation):
            continue
        method = str(observation.get("price_selection_method") or "").strip()
        if method not in PRICE_SELECTION_METHODS:
            raise HarnessProtocolError(
                "fare-bearing flight observation "
                f"{index} requires price_selection_method in "
                f"{sorted(PRICE_SELECTION_METHODS)}"
            )


def _fare_bearing_flight(observation: dict[str, Any]) -> bool:
    if observation.get("evidence_status") == "research_observed":
        for candidate in observation.get("gateway_candidates", []):
            signal = candidate.get("signal", {}) if isinstance(candidate, dict) else {}
            if isinstance(signal, dict) and any(
                signal.get(field) is not None
                for field in (
                    "dated_fare_amount",
                    "lowest_displayed_total",
                    "complete_cost_lower_bound",
                )
            ):
                return True
        return bool(observation.get("temporal_signals"))
    if observation.get("evidence_status") != "verified_observed":
        return False
    modes = {str(observation.get("mode") or "")}
    modes.update(
        str(movement.get("mode") or "")
        for movement in observation.get("movements", [])
        if isinstance(movement, dict)
    )
    itinerary = observation.get("itinerary")
    if isinstance(itinerary, dict):
        modes.update(
            str(segment.get("mode") or "")
            for segment in itinerary.get("segments", [])
            if isinstance(segment, dict)
        )
    return "flight" in modes


def _observed_source_ids(attempt: dict[str, Any]) -> set[str]:
    result: set[str] = set()

    def add_identity(value: Any, url: Any = None) -> None:
        text = str(value or "").strip().lower()
        host = (urlparse(str(url or "")).hostname or "").lower()
        google_host = host == "google.com" or host.endswith(".google.com")
        if text in {"google-flights", "google flights"} or google_host:
            result.add("google-flights")
        elif text:
            result.add(text)

    source = attempt.get("source")
    if isinstance(source, dict):
        add_identity(
            source.get("source_id") or source.get("provider") or source.get("name"),
            source.get("url"),
        )
    payload = attempt.get("product_payload")
    if isinstance(payload, dict):
        for observation in payload.get("observations", []):
            if isinstance(observation, dict):
                add_identity(
                    observation.get("source_id") or observation.get("provider"),
                    observation.get("url"),
                )
    return result
