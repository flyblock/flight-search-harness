"""Adapt browser observations into immutable round-trip evidence bundles."""

from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


VERSION = "0.4.0"
REGISTRY_VERSION = "0.5.0"
STATUSES = {"ai_proposed", "verified_observed", "research_observed", "no_result", "rejected"}


class EvidenceAdapterError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceAdapterError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceAdapterError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceAdapterError(f"{field} must include a UTC offset")
    return raw


def _trusted_evaluation_time(value: str | datetime | None) -> tuple[str, datetime]:
    if value is None:
        parsed = datetime.now(timezone.utc)
        return parsed.isoformat(timespec="seconds"), parsed
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise EvidenceAdapterError("trusted_at must include a UTC offset")
        return value.isoformat(timespec="seconds"), value
    raw = _timestamp(value, "trusted_at")
    return raw, datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _normalize_observed_at(
    value: Any,
    field: str,
    trusted_time: tuple[str, datetime],
) -> dict[str, Any]:
    reported = _timestamp(value, field)
    parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
    trusted_raw, trusted = trusted_time
    if parsed <= trusted:
        return {"observed_at": reported}
    return {
        "observed_at": trusted_raw,
        "observed_at_normalization": {
            "reason": "reported_observed_at_after_trusted_evaluation_time",
            "reported_observed_at": reported,
            "trusted_evaluation_at": trusted_raw,
        },
    }


def _normalize_proposed_at(
    value: Any,
    field: str,
    trusted_time: tuple[str, datetime],
) -> dict[str, Any]:
    reported = _timestamp(value, field)
    parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
    trusted_raw, trusted = trusted_time
    if parsed <= trusted:
        return {"proposed_at": reported}
    return {
        "proposed_at": trusted_raw,
        "proposed_at_normalization": {
            "reason": "reported_proposed_at_after_trusted_evaluation_time",
            "reported_proposed_at": reported,
            "trusted_evaluation_at": trusted_raw,
        },
    }


def _observation_time_fields(identity: dict[str, Any]) -> dict[str, Any]:
    result = {"observed_at": identity["observed_at"]}
    if "price_selection_method" in identity:
        result["price_selection_method"] = identity["price_selection_method"]
    if "observed_at_normalization" in identity:
        result["observed_at_normalization"] = copy.deepcopy(
            identity["observed_at_normalization"]
        )
    return result


def _date_only(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError as exc:
        raise EvidenceAdapterError(f"{field} must be an ISO date") from exc


def _identifier_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise EvidenceAdapterError(f"{field} must be a non-empty array")
    identifiers = [_text(item, f"{field}[]") for item in value]
    if len(set(identifiers)) != len(identifiers):
        raise EvidenceAdapterError(f"{field} must not contain duplicates")
    return identifiers


def _date_window(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    start = _date_only(value.get("start"), f"{field}.start")
    end = _date_only(value.get("end"), f"{field}.end")
    if start > end:
        raise EvidenceAdapterError(f"{field}.start must not be after end")
    return {"start": start, "end": end}


def _stay_nights(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceAdapterError(
            f"{field} must be a non-negative integer or null"
        )
    return value


def _provider_scope(
    raw: Any,
    field: str,
    identity: dict[str, Any],
    source_registry: dict[str, dict[str, Any]],
    *,
    disposition: str,
    temporal_signal_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    if raw.get("coverage_semantics") != "provider_ranked_window":
        raise EvidenceAdapterError(
            f"{field}.coverage_semantics must be provider_ranked_window"
        )
    price_selection_method = identity.get("price_selection_method")
    if price_selection_method != "price_sorted_api":
        raise EvidenceAdapterError(
            f"{field} requires price_selection_method=price_sorted_api"
        )
    if identity.get("source_id") == "google-flights":
        raise EvidenceAdapterError(
            f"{field} cannot use manual Google Flights evidence to close pool coverage"
        )
    registered_source = source_registry.get(str(identity.get("source_id")))
    if not isinstance(registered_source, dict):
        raise EvidenceAdapterError(
            f"{field} requires a registered structured Provider"
        )
    expected_host = urlsplit(str(registered_source.get("url") or "")).hostname
    observed_host = urlsplit(str(identity.get("url") or "")).hostname
    if not expected_host or not observed_host or not (
        observed_host == expected_host
        or observed_host.endswith("." + expected_host)
        or expected_host.endswith("." + observed_host)
    ):
        raise EvidenceAdapterError(
            f"{field} source_id does not match the observed Provider URL"
        )
    minimum_stay = _stay_nights(
        raw.get("minimum_stay_nights"), f"{field}.minimum_stay_nights"
    )
    maximum_stay = _stay_nights(
        raw.get("maximum_stay_nights"), f"{field}.maximum_stay_nights"
    )
    if (
        minimum_stay is not None
        and maximum_stay is not None
        and minimum_stay > maximum_stay
    ):
        raise EvidenceAdapterError(
            f"{field}.minimum_stay_nights must not exceed maximum_stay_nights"
        )
    return {
        "observation_id": identity["observation_id"],
        "source_id": identity["source_id"],
        "observed_at": identity["observed_at"],
        "price_selection_method": price_selection_method,
        "coverage_semantics": "provider_ranked_window",
        "scope_disposition": disposition,
        "origin_gateway_node_ids": _identifier_list(
            raw.get("origin_gateway_node_ids"),
            f"{field}.origin_gateway_node_ids",
        ),
        "destination_gateway_node_ids": _identifier_list(
            raw.get("destination_gateway_node_ids"),
            f"{field}.destination_gateway_node_ids",
        ),
        "outbound_departure_window": _date_window(
            raw.get("outbound_departure_window"),
            f"{field}.outbound_departure_window",
        ),
        "return_departure_window": _date_window(
            raw.get("return_departure_window"),
            f"{field}.return_departure_window",
        ),
        "minimum_stay_nights": minimum_stay,
        "maximum_stay_nights": maximum_stay,
        "temporal_signal_ids": list(temporal_signal_ids),
    }


def _money(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    amount = value.get("amount")
    currency = value.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise EvidenceAdapterError(f"{field}.amount must be non-negative")
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        raise EvidenceAdapterError(f"{field}.currency must be a three-letter code")
    return {"amount": float(amount), "currency": currency.strip().upper()}


def _http(value: Any, field: str) -> str:
    url = _text(value, field)
    if any(character.isspace() for character in url):
        raise EvidenceAdapterError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise EvidenceAdapterError(
            f"{field} must be an absolute HTTP(S) URL"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise EvidenceAdapterError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise EvidenceAdapterError(f"{field} must not contain URL credentials")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def _registry(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path) if path else Path(__file__).parents[1] / "data" / "source-registry.json"
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceAdapterError(f"cannot read source registry: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("registry_version") != REGISTRY_VERSION:
        raise EvidenceAdapterError(f"source registry must use version {REGISTRY_VERSION}")
    return {item["source_id"]: item for item in raw.get("sources", [])}


def _node(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    result = copy.deepcopy(raw)
    result["node_id"] = _text(raw.get("node_id"), f"{field}.node_id")
    result["node_type"] = _text(raw.get("node_type"), f"{field}.node_type")
    result["location"] = _text(raw.get("location"), f"{field}.location")
    result["country_or_region"] = _text(raw.get("country_or_region"), f"{field}.country_or_region")
    result["timezone"] = _text(raw.get("timezone"), f"{field}.timezone")
    return result


def _provenance(
    raw: Any,
    field: str,
    sources: dict[str, dict[str, Any]],
    trusted_time: tuple[str, datetime],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvidenceAdapterError(f"{field} must be an array")
    result = []
    for index, item in enumerate(raw):
        item_field = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise EvidenceAdapterError(f"{item_field} must be an object")
        source_id = _text(item.get("source_id"), f"{item_field}.source_id")
        result.append(
            {
                "source_id": source_id,
                "provider": _text(item.get("provider"), f"{item_field}.provider"),
                "url": _http(item.get("url"), f"{item_field}.url"),
                **_normalize_observed_at(
                    item.get("observed_at"),
                    f"{item_field}.observed_at",
                    trusted_time,
                ),
                "evidence_role": sources.get(source_id, {}).get("evidence_role", "unregistered_ai_choice"),
            }
        )
    return result


def _gateway_candidate(raw: Any, field: str, identity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    side = raw.get("side")
    if side not in {"origin", "destination"}:
        raise EvidenceAdapterError(f"{field}.side must be origin or destination")
    signal = raw.get("signal")
    if not isinstance(signal, dict):
        raise EvidenceAdapterError(f"{field}.signal must be an object")
    node = _node(raw.get("node"), f"{field}.node")
    return {
        "candidate_id": _text(raw.get("candidate_id"), f"{field}.candidate_id"),
        "side": side,
        "node": node,
        "signal": _gateway_signal(
            signal, f"{field}.signal", node["node_id"], side
        ),
        "source_id": identity["source_id"],
        "provider": identity["provider"],
        "url": identity["url"],
        **_observation_time_fields(identity),
    }


def _gateway_signal_amount(value: Any, field: str) -> dict[str, Any] | float:
    if isinstance(value, dict):
        normalized = _money(value, field)
        assert normalized is not None
        return normalized
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise EvidenceAdapterError(
            f"{field} must be a non-negative number or money object"
        )
    return float(value)


def _gateway_signal(
    raw: dict[str, Any], field: str, candidate_node_id: str, side: str
) -> dict[str, Any]:
    """Normalize the compact gateway-signal contract at the Evidence Seam."""

    result = copy.deepcopy(raw)
    canonical_kind = raw.get("kind")
    alias_kind = raw.get("signal_kind")
    if canonical_kind is not None and alias_kind is not None and canonical_kind != alias_kind:
        raise EvidenceAdapterError(f"{field}.kind conflicts with signal_kind")
    result["kind"] = _text(
        canonical_kind if canonical_kind is not None else alias_kind,
        f"{field}.kind",
    )
    result.pop("signal_kind", None)

    canonical_fare = raw.get("dated_fare_amount")
    alias_fare = raw.get("lowest_displayed_total")
    normalized_fare = (
        _gateway_signal_amount(canonical_fare, f"{field}.dated_fare_amount")
        if canonical_fare is not None
        else None
    )
    normalized_alias_fare = (
        _gateway_signal_amount(alias_fare, f"{field}.lowest_displayed_total")
        if alias_fare is not None
        else None
    )
    if (
        normalized_fare is not None
        and normalized_alias_fare is not None
        and normalized_fare != normalized_alias_fare
    ):
        raise EvidenceAdapterError(
            f"{field}.dated_fare_amount conflicts with lowest_displayed_total"
        )
    if normalized_fare is not None or normalized_alias_fare is not None:
        result["dated_fare_amount"] = (
            normalized_fare
            if normalized_fare is not None
            else normalized_alias_fare
        )
        date_coverage = raw.get("date_coverage", "sampled_date_pair")
        if date_coverage not in {
            "sampled_date_pair",
            "exact_dates",
            "bounded_window",
        }:
            raise EvidenceAdapterError(
                f"{field}.date_coverage is unsupported"
            )
        result["date_coverage"] = date_coverage
    elif raw.get("date_coverage") is not None:
        raise EvidenceAdapterError(
            f"{field}.date_coverage requires dated_fare_amount"
        )
    result.pop("lowest_displayed_total", None)

    for amount_field in ("complete_cost_lower_bound", "access_cost"):
        if raw.get(amount_field) is not None:
            result[amount_field] = _gateway_signal_amount(
                raw[amount_field], f"{field}.{amount_field}"
            )

    has_pair_price = any(
        result.get(amount_field) is not None
        for amount_field in ("dated_fare_amount", "complete_cost_lower_bound")
    )
    legacy_opposite_field = (
        "destination_gateway_node_id"
        if side == "origin"
        else "origin_gateway_node_id"
    )
    legacy_candidate_field = (
        "origin_gateway_node_id"
        if side == "origin"
        else "destination_gateway_node_id"
    )
    opposite_gateway_node_id = raw.get("opposite_gateway_node_id")
    legacy_opposite_gateway_node_id = raw.get(legacy_opposite_field)
    if (
        opposite_gateway_node_id is not None
        and legacy_opposite_gateway_node_id is not None
        and _text(
            opposite_gateway_node_id, f"{field}.opposite_gateway_node_id"
        )
        != _text(
            legacy_opposite_gateway_node_id, f"{field}.{legacy_opposite_field}"
        )
    ):
        raise EvidenceAdapterError(
            f"{field}.opposite_gateway_node_id conflicts with {legacy_opposite_field}"
        )
    if opposite_gateway_node_id is None:
        opposite_gateway_node_id = legacy_opposite_gateway_node_id
    legacy_candidate_node_id = raw.get(legacy_candidate_field)
    if (
        legacy_candidate_node_id is not None
        and _text(
            legacy_candidate_node_id, f"{field}.{legacy_candidate_field}"
        )
        != candidate_node_id
    ):
        raise EvidenceAdapterError(
            f"{field}.{legacy_candidate_field} conflicts with the candidate node"
        )
    if has_pair_price:
        opposite_gateway_node_id = _text(
            opposite_gateway_node_id, f"{field}.opposite_gateway_node_id"
        )
        if opposite_gateway_node_id == candidate_node_id:
            raise EvidenceAdapterError(
                f"{field}.opposite_gateway_node_id must identify the other gateway"
            )
        result["opposite_gateway_node_id"] = opposite_gateway_node_id
    elif opposite_gateway_node_id is not None:
        result["opposite_gateway_node_id"] = _text(
            opposite_gateway_node_id, f"{field}.opposite_gateway_node_id"
        )
    result.pop("origin_gateway_node_id", None)
    result.pop("destination_gateway_node_id", None)

    currencies = {
        value["currency"]
        for amount_field in (
            "dated_fare_amount",
            "complete_cost_lower_bound",
            "access_cost",
        )
        for value in [result.get(amount_field)]
        if isinstance(value, dict)
    }
    if len(currencies) > 1:
        raise EvidenceAdapterError(
            f"{field} money fields must use one currency"
        )
    return result


def _temporal_signal(raw: Any, field: str, identity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceAdapterError(f"{field} must be an object")
    kind = raw.get("signal_kind")
    if kind not in {"joint_date_pair", "directional_date", "split_date_pair"}:
        raise EvidenceAdapterError(f"{field}.signal_kind is unsupported")
    result = copy.deepcopy(raw)
    result.update(
        {
            "signal_id": _text(raw.get("signal_id"), f"{field}.signal_id"),
            "signal_kind": kind,
            "evidence_status": "research_observed",
            "admission_role": "research_only",
            "source_id": identity["source_id"],
            "provider": identity["provider"],
            "url": identity["url"],
            **_observation_time_fields(identity),
            "observation_id": identity["observation_id"],
        }
    )
    scope = raw.get("availability_scope", "flexible_search")
    if scope not in {"calendar", "date_grid", "flexible_search"}:
        raise EvidenceAdapterError(f"{field}.availability_scope is unsupported")
    result["availability_scope"] = scope
    if kind == "joint_date_pair":
        result["origin_gateway_node_id"] = _text(raw.get("origin_gateway_node_id"), f"{field}.origin_gateway_node_id")
        result["destination_gateway_node_id"] = _text(raw.get("destination_gateway_node_id"), f"{field}.destination_gateway_node_id")
        result["outbound_origin_gateway_node_id"] = _text(
            raw.get("outbound_origin_gateway_node_id", result["origin_gateway_node_id"]),
            f"{field}.outbound_origin_gateway_node_id",
        )
        result["outbound_destination_gateway_node_id"] = _text(
            raw.get("outbound_destination_gateway_node_id", result["destination_gateway_node_id"]),
            f"{field}.outbound_destination_gateway_node_id",
        )
        result["return_origin_gateway_node_id"] = _text(
            raw.get("return_origin_gateway_node_id", result["destination_gateway_node_id"]),
            f"{field}.return_origin_gateway_node_id",
        )
        result["return_destination_gateway_node_id"] = _text(
            raw.get("return_destination_gateway_node_id", result["origin_gateway_node_id"]),
            f"{field}.return_destination_gateway_node_id",
        )
        result["outbound_date"] = _date_only(raw.get("outbound_date"), f"{field}.outbound_date")
        result["return_date"] = _date_only(raw.get("return_date"), f"{field}.return_date")
        result["atomic_total"] = _money(raw.get("atomic_total"), f"{field}.atomic_total")
        if raw.get("outbound_atomic_total") is not None or raw.get("return_atomic_total") is not None:
            raise EvidenceAdapterError(f"{field} joint total must not be allocated to directions")
    elif kind == "directional_date":
        direction = raw.get("direction")
        if direction not in {"outbound", "return"}:
            raise EvidenceAdapterError(f"{field}.direction must be outbound or return")
        result["direction"] = direction
        result["departure_date"] = _date_only(raw.get("departure_date"), f"{field}.departure_date")
        result["from_gateway_node_id"] = _text(raw.get("from_gateway_node_id"), f"{field}.from_gateway_node_id")
        result["to_gateway_node_id"] = _text(raw.get("to_gateway_node_id"), f"{field}.to_gateway_node_id")
        result["atomic_total"] = _money(raw.get("atomic_total"), f"{field}.atomic_total")
    else:
        result["origin_gateway_node_id"] = _text(raw.get("origin_gateway_node_id"), f"{field}.origin_gateway_node_id")
        result["destination_gateway_node_id"] = _text(raw.get("destination_gateway_node_id"), f"{field}.destination_gateway_node_id")
        result["outbound_date"] = _date_only(raw.get("outbound_date"), f"{field}.outbound_date")
        result["return_date"] = _date_only(raw.get("return_date"), f"{field}.return_date")
        result["outbound_atomic_total"] = _money(raw.get("outbound_atomic_total"), f"{field}.outbound_atomic_total")
        result["return_atomic_total"] = _money(raw.get("return_atomic_total"), f"{field}.return_atomic_total")
        if raw.get("atomic_total") is not None:
            raise EvidenceAdapterError(f"{field} split directions must remain separate atomic totals")
    return result


def adapt_observations(
    raw: Any,
    registry_path: str | Path | None = None,
    *,
    trusted_at: str | datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceAdapterError("observations input must be an object")
    if raw.get("observation_version") != VERSION:
        raise EvidenceAdapterError(f"observation_version must be {VERSION}")
    bundle_id = _text(raw.get("bundle_id"), "bundle_id")
    task_id = _text(raw.get("task_id"), "task_id")
    coverage_complete = raw.get("coverage_complete", True)
    if not isinstance(coverage_complete, bool):
        raise EvidenceAdapterError("coverage_complete must be boolean")
    sources = _registry(registry_path)
    trusted_time = _trusted_evaluation_time(trusted_at)
    observations = raw.get("observations")
    if not isinstance(observations, list) or not observations:
        raise EvidenceAdapterError("observations must be a non-empty array")
    statuses: set[str] = set()
    movements: dict[str, dict[str, Any]] = {}
    offers: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    gateway_candidates: dict[str, dict[str, Any]] = {}
    temporal_signals: dict[str, dict[str, Any]] = {}
    provider_scopes: list[dict[str, Any]] = []
    ai_proposals: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        field = f"observations[{index}]"
        if not isinstance(observation, dict):
            raise EvidenceAdapterError(f"{field} must be an object")
        status = observation.get("evidence_status")
        if status not in STATUSES:
            raise EvidenceAdapterError(f"{field}.evidence_status is unsupported")
        statuses.add(status)
        if status == "ai_proposed":
            proposal_type = observation.get("proposal_type")
            if proposal_type != "region_scope":
                raise EvidenceAdapterError(f"{field}.proposal_type is unsupported")
            proposal = observation.get("proposal")
            if not isinstance(proposal, dict):
                raise EvidenceAdapterError(f"{field}.proposal must be an object")
            proposed_by = observation.get("proposed_by", "ai")
            if proposed_by != "ai":
                raise EvidenceAdapterError(f"{field}.proposed_by must be ai")
            normalized_proposal = {
                "observation_id": _text(
                    observation.get("observation_id"), f"{field}.observation_id"
                ),
                "evidence_status": status,
                "proposal_type": proposal_type,
                "proposed_by": "ai",
                **_normalize_proposed_at(
                    observation.get("proposed_at"),
                    f"{field}.proposed_at",
                    trusted_time,
                ),
                "reason": _text(observation.get("reason"), f"{field}.reason"),
                "proposal": copy.deepcopy(proposal),
            }
            ai_proposals.append(copy.deepcopy(normalized_proposal))
            normalized.append(normalized_proposal)
            continue
        reported_identity = {
            "observation_id": _text(observation.get("observation_id"), f"{field}.observation_id"),
            "source_id": _text(observation.get("source_id"), f"{field}.source_id"),
            "provider": _text(observation.get("provider"), f"{field}.provider"),
            "url": _http(observation.get("url"), f"{field}.url"),
            "observed_at": _timestamp(observation.get("observed_at"), f"{field}.observed_at"),
        }
        identity = {
            **reported_identity,
            **_normalize_observed_at(
                reported_identity["observed_at"],
                f"{field}.observed_at",
                trusted_time,
            ),
        }
        if observation.get("price_selection_method") is not None:
            identity["price_selection_method"] = _text(
                observation["price_selection_method"],
                f"{field}.price_selection_method",
            )
        if status in {"no_result", "rejected"}:
            if observation.get("movements") or observation.get("offer") or observation.get("gateway_candidates") or observation.get("temporal_signals"):
                raise EvidenceAdapterError(f"{field} terminal evidence cannot carry facts")
            terminal = {
                **identity,
                "evidence_status": status,
                "reason": _text(observation.get("reason"), f"{field}.reason"),
            }
            if observation.get("date_coverage") is not None:
                date_coverage = observation["date_coverage"]
                if date_coverage not in {
                    "sampled_date_pair",
                    "exact_dates",
                    "bounded_window",
                }:
                    raise EvidenceAdapterError(
                        f"{field}.date_coverage is unsupported"
                    )
                terminal["date_coverage"] = date_coverage
            if observation.get("provider_scope") is not None:
                if status != "no_result":
                    raise EvidenceAdapterError(
                        f"{field}.provider_scope is allowed only for a complete no_result"
                    )
                normalized_scope = _provider_scope(
                    observation["provider_scope"],
                    f"{field}.provider_scope",
                    identity,
                    sources,
                    disposition="no_market_result",
                    temporal_signal_ids=[],
                )
                terminal["provider_scope"] = copy.deepcopy(normalized_scope)
                provider_scopes.append(normalized_scope)
            normalized.append(terminal)
            continue
        if status == "research_observed":
            candidates_raw = observation.get("gateway_candidates")
            signals_raw = observation.get("temporal_signals")
            has_candidates = isinstance(candidates_raw, list) and bool(candidates_raw)
            has_signals = isinstance(signals_raw, list) and bool(signals_raw)
            if has_candidates == has_signals:
                raise EvidenceAdapterError(f"{field} research evidence must carry exactly one of gateway_candidates or temporal_signals")
            candidate_ids = []
            signal_ids = []
            if has_candidates:
                for candidate_index, candidate_raw in enumerate(candidates_raw):
                    candidate = _gateway_candidate(candidate_raw, f"{field}.gateway_candidates[{candidate_index}]", identity)
                    candidate_id = candidate["candidate_id"]
                    if candidate_id in gateway_candidates and gateway_candidates[candidate_id] != candidate:
                        raise EvidenceAdapterError(f"conflicting gateway candidate {candidate_id}")
                    gateway_candidates[candidate_id] = candidate
                    nodes[candidate["node"]["node_id"]] = candidate["node"]
                    candidate_ids.append(candidate_id)
            else:
                for signal_index, signal_raw in enumerate(signals_raw):
                    signal = _temporal_signal(signal_raw, f"{field}.temporal_signals[{signal_index}]", identity)
                    signal_id = signal["signal_id"]
                    if signal_id in temporal_signals and temporal_signals[signal_id] != signal:
                        raise EvidenceAdapterError(f"conflicting temporal signal {signal_id}")
                    temporal_signals[signal_id] = signal
                    signal_ids.append(signal_id)
            normalized_research = {
                **identity,
                "evidence_status": status,
                "gateway_candidate_ids": candidate_ids,
                "temporal_signal_ids": signal_ids,
            }
            if observation.get("provider_scope") is not None:
                if not has_signals:
                    raise EvidenceAdapterError(
                        f"{field}.provider_scope requires temporal_signals"
                    )
                normalized_scope = _provider_scope(
                    observation["provider_scope"],
                    f"{field}.provider_scope",
                    identity,
                    sources,
                    disposition="winner",
                    temporal_signal_ids=signal_ids,
                )
                normalized_research["provider_scope"] = copy.deepcopy(
                    normalized_scope
                )
                provider_scopes.append(normalized_scope)
            if observation.get("date_coverage") is not None:
                date_coverage = observation["date_coverage"]
                if date_coverage not in {
                    "sampled_date_pair",
                    "exact_dates",
                    "bounded_window",
                }:
                    raise EvidenceAdapterError(
                        f"{field}.date_coverage is unsupported"
                    )
                normalized_research["date_coverage"] = date_coverage
            normalized.append(normalized_research)
            continue
        raw_movements = observation.get("movements")
        offer = observation.get("offer")
        if not isinstance(raw_movements, list) or not raw_movements or not isinstance(offer, dict):
            raise EvidenceAdapterError(f"{field} verified evidence requires movements and one offer")
        if any(offer.get(key) != reported_identity[key] for key in ("source_id", "provider", "url", "observed_at")):
            raise EvidenceAdapterError(f"{field} observation identity must match the payable offer")
        role = sources.get(identity["source_id"], {}).get("evidence_role", "unregistered_ai_choice")
        if role == "research_only_until_payable_seller_verified":
            raise EvidenceAdapterError(f"{field} research-only source cannot directly produce verified payable evidence")
        _text(offer.get("seller"), f"{field}.offer.seller")
        for node_index, node_raw in enumerate(observation.get("nodes", [])):
            node = _node(node_raw, f"{field}.nodes[{node_index}]")
            if node["node_id"] in nodes and nodes[node["node_id"]] != node:
                raise EvidenceAdapterError(f"conflicting node {node['node_id']}")
            nodes[node["node_id"]] = node
        for movement in raw_movements:
            if not isinstance(movement, dict):
                raise EvidenceAdapterError(f"{field}.movements[] must be an object")
            movement_id = _text(movement.get("movement_id"), f"{field}.movement_id")
            mode = _text(movement.get("mode"), f"{field}.movements.{movement_id}.mode")
            _text(movement.get("operator"), f"{field}.movements.{movement_id}.operator")
            _text(movement.get("service_id"), f"{field}.movements.{movement_id}.service_id")
            if mode == "flight":
                identity_value = movement.get("flight_identity")
                if not isinstance(identity_value, dict):
                    raise EvidenceAdapterError(f"{field}.movements.{movement_id}.flight_identity is required")
                carrier = identity_value.get("marketing_carrier") or identity_value.get("operating_carrier")
                if not isinstance(carrier, str) or not carrier.strip():
                    raise EvidenceAdapterError(f"{field}.movements.{movement_id}.flight_identity requires carrier")
                numbers = identity_value.get("flight_numbers")
                unknown_reason = identity_value.get("unknown_reason")
                if not (isinstance(numbers, list) and numbers and all(isinstance(number, str) and number.strip() for number in numbers)):
                    if not (isinstance(unknown_reason, dict) and isinstance(unknown_reason.get("code"), str) and unknown_reason["code"].strip()):
                        raise EvidenceAdapterError(f"{field}.movements.{movement_id}.flight_identity requires flight_numbers or structured unknown_reason")
            if movement_id in movements and movements[movement_id] != movement:
                raise EvidenceAdapterError(f"conflicting movement {movement_id}")
            movements[movement_id] = copy.deepcopy(movement)
        offer_id = _text(offer.get("offer_id"), f"{field}.offer.offer_id")
        normalized_offer = {
            **copy.deepcopy(offer),
            **_observation_time_fields(identity),
        }
        if offer_id in offers and offers[offer_id] != normalized_offer:
            raise EvidenceAdapterError(f"conflicting offer {offer_id}")
        offers[offer_id] = normalized_offer
        normalized.append(
            {
                **identity,
                "evidence_status": status,
                "offer_id": offer_id,
                "discovery_provenance": _provenance(
                    observation.get("discovery_provenance"),
                    f"{field}.discovery_provenance",
                    sources,
                    trusted_time,
                ),
            }
        )
    if len(statuses) != 1:
        raise EvidenceAdapterError("one evidence bundle must contain exactly one evidence_status")
    evidence_status = next(iter(statuses))
    return {
        "evidence_version": VERSION,
        "bundle_id": bundle_id,
        "task_id": task_id,
        "evidence_status": evidence_status,
        "coverage_complete": coverage_complete,
        "nodes": list(nodes.values()),
        "gateway_candidates": list(gateway_candidates.values()),
        "temporal_signals": list(temporal_signals.values()),
        "provider_scopes": provider_scopes,
        "ai_proposals": ai_proposals,
        "movements": list(movements.values()),
        "offers": list(offers.values()),
        "observations": normalized,
    }


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--registry")
    args = parser.parse_args(argv)
    try:
        write_json(args.output, adapt_observations(read_json(args.input), args.registry))
        return 0
    except (OSError, json.JSONDecodeError, EvidenceAdapterError) as exc:
        print(f"round_trip_evidence_adapter: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
