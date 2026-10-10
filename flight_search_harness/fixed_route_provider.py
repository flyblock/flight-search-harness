"""Shared fixed-route flight Provider foundation for one-way and round-trip."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlparse

from .contracts import HarnessProtocolError


FOUNDATION_VERSION = "1.1"
PRIMARY_FLIGHT_SOURCE_ID = "google-flights"
PRIMARY_FLIGHT_BASE_SCORE = 95
EXCLUDED_FLIGHT_SOURCE_IDS = {"azair"}


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
        filtered = [
            copy.deepcopy(item)
            for item in options
            if isinstance(item, dict)
            and str(item.get("source_id")) not in EXCLUDED_FLIGHT_SOURCE_IDS
        ]
        for item in filtered:
            if item.get("source_id") == PRIMARY_FLIGHT_SOURCE_ID:
                item["foundation_base_score"] = PRIMARY_FLIGHT_BASE_SCORE
                item["foundation_role"] = "primary_fixed_route_flight_search"
                google_option = copy.deepcopy(item)
        if field == "source_suggestions":
            filtered.sort(
                key=lambda item: (
                    0 if item.get("source_id") == PRIMARY_FLIGHT_SOURCE_ID else 1,
                    -int(item.get("registry_priority", 0)),
                    str(item.get("source_id", "")),
                )
            )
        brief[field] = filtered

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
            "base_score": PRIMARY_FLIGHT_BASE_SCORE,
            "role": "primary_fixed_route_flight_search",
            "binding": True,
        }
    brief["provider_foundation"] = {
        "foundation_version": FOUNDATION_VERSION,
        "journey_family": "fixed_route",
        "applies_to_flight_task": applies_to_flight_task,
        "excluded_source_ids": sorted(EXCLUDED_FLIGHT_SOURCE_IDS),
        "primary_source_id": PRIMARY_FLIGHT_SOURCE_ID,
        "primary_base_score": PRIMARY_FLIGHT_BASE_SCORE,
        "first_attempt_required": first_attempt,
        "primary_can_represent_required_scope": google_supports_required_scope,
        "scope_exception_reason": (
            None
            if google_supports_required_scope
            else "primary_source_has_no_verified_support_for_an_expanding_query_shape"
        ),
        "required_source": copy.deepcopy(required),
    }
    return brief


def validate_first_attempt(
    attempt: dict[str, Any], *, required_source: dict[str, Any] | None
) -> None:
    if not required_source:
        return
    expected = str(required_source["source_id"])
    observed = _observed_source_ids(attempt)
    if expected not in observed or observed - {expected}:
        raise HarnessProtocolError(
            f"fixed-route first flight attempt requires source_id={expected}; "
            f"observed={sorted(observed) or ['<missing>']}"
        )


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
