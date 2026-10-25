"""Preserve observations and admit independently usable Offers.

Admission establishes usable source facts, not request eligibility, source
coverage, transfer protection, or an independently verified booking. The caller
persists this complete record in its existing Session; this Module owns no store.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


def _identifier(value: Any, prefix: str) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(value)
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _issue(issues: list[str], field: str, detail: str, *, missing: bool = False) -> None:
    issues.append(f"{'missing' if missing else 'invalid'}: {field}: {detail}")


def _text(value: Any, field: str, issues: list[str]) -> None:
    if value is None or value == "":
        _issue(issues, field, "required", missing=True)
    elif not _has_text(value):
        _issue(issues, field, "must be non-empty text")


def _instant(value: Any, field: str, issues: list[str]) -> datetime | None:
    if value is None or value == "":
        _issue(issues, field, "Offset Timestamp required", missing=True)
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("missing offset")
        # Confirm the instant is representable before any caller normalizes it.
        parsed.astimezone(timezone.utc)
        return parsed
    except (AttributeError, TypeError, ValueError, OverflowError):
        _issue(issues, field, "must be a valid timestamp carrying a UTC offset")
        return None


def _url(value: Any, field: str, issues: list[str]) -> str | None:
    """Return an optional displayable reference; never decide Offer admission."""
    if value is None or value == "":
        _issue(issues, field, "reference unavailable; query by seller and itinerary instead", missing=True)
        return None
    try:
        if not isinstance(value, str):
            raise ValueError("reference is not text")
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host
                or host == "invalid" or host.endswith(".invalid")
                or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError("not a displayable HTTP reference")
        _ = parsed.port
        return value
    except (AttributeError, TypeError, ValueError):
        _issue(issues, field, "reference cannot be used as an HTTP(S) link; original retained in raw")
        return None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _metadata_time(value: Any, field: str, issues: list[str]) -> str | None:
    if value is None:
        return None
    instant = _instant(value, field, issues)
    return _utc(instant) if instant is not None else None


def _capture_time(data: dict[str, Any], receipt_time: datetime,
                  issues: list[str]) -> tuple[str | None, str]:
    supplied = [(key, data[key]) for key in ("captured_at", "observed_at", "capturedAt")
                if data.get(key) not in (None, "")]
    if not supplied:
        _issue(issues, "captured_at", "capture time unknown; recording time is not a substitute", missing=True)
        return None, "unknown"
    instants = [_instant(value, key, issues) for key, value in supplied]
    if any(instant is None for instant in instants):
        return None, "unreliable"
    first = instants[0]
    if any(instant != first for instant in instants[1:]):
        _issue(issues, "captured_at", "reported capture-time aliases identify different instants")
        return None, "unreliable"
    if first > receipt_time:
        _issue(issues, "captured_at", "reported time is later than recording time; capture time remains unreliable")
        return None, "unreliable"
    return _utc(first), "known"


def _money(value: Any, field: str, issues: list[str]) -> None:
    if value is None:
        _issue(issues, field, "explicit monetary amount and currency required", missing=True)
        return
    if not isinstance(value, dict):
        _issue(issues, field, "must be an object")
        return
    amount = value.get("amount")
    if amount is None:
        _issue(issues, field + ".amount", "required", missing=True)
    elif (isinstance(amount, bool) or not isinstance(amount, (int, float))
          or (isinstance(amount, float) and not math.isfinite(amount)) or amount < 0):
        _issue(issues, field + ".amount", "must be a finite non-negative number")
    currency = value.get("currency")
    if currency is None or currency == "":
        _issue(issues, field + ".currency", "required", missing=True)
    elif not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        _issue(issues, field + ".currency", "must be a three-letter uppercase currency code")


def _admission(issues: list[str], *, signal: bool = False) -> dict[str, Any]:
    unique = list(dict.fromkeys(issues))
    status = ("invalid" if any(item.startswith("invalid:") for item in unique)
              else "incomplete" if unique else "signal" if signal else "admitted")
    return {"status": status, "issues": unique}


def normalize_observation(raw: Any, *, received_at: str) -> dict[str, Any]:
    """Return a lossless record, including per-Offer admission outcomes.

    Inputs are JSON values. Incomplete or contradictory journey/price facts
    affect admission; auxiliary time/reference problems remain metadata issues.
    A usable sibling Offer can still be admitted. Recording time never
    substitutes for capture time or Provider quote time.
    """
    data = raw if isinstance(raw, dict) else {}
    observation_id = data.get("observation_id")
    if not _has_text(observation_id):
        observation_id = _identifier(raw, "observation-")
    common: list[str] = []
    metadata: list[str] = []
    receipt_issues: list[str] = []
    receipt_time = _instant(received_at, "received_at", receipt_issues)
    if receipt_time is None:
        raise ValueError("received_at must be the runtime's actual timezone-aware recording time")
    received_at = _utc(receipt_time)
    if not isinstance(raw, dict):
        _issue(common, "observation", "must be an object")
    source_raw = data.get("source", {})
    if not isinstance(source_raw, dict):
        _issue(common, "source", "must be an object")
        source_raw = {}
    provider = source_raw.get("provider", data.get("provider"))
    page_url = source_raw.get("page_url", data.get("page_url", data.get("url")))
    seller = source_raw.get("seller", data.get("seller"))
    for field, normalized in (("provider", provider), ("seller", seller)):
        if data.get(field) is not None and normalized != data[field]:
            _issue(common, field, "conflicts with source identity")
    for key in ("page_url", "url"):
        if data.get(key) is not None and page_url != data[key]:
            _issue(metadata, key, "differs from the selected source page reference; originals retained in raw")
    _text(provider, "source.provider", common)
    page_url = _url(page_url, "source.page_url", metadata)
    source = {"provider": provider, "page_url": page_url}
    if seller is not None:
        _text(seller, "source.seller", common)
        source["seller"] = seller
    captured_at, capture_time_status = _capture_time(data, receipt_time, metadata)
    capture_time = datetime.fromisoformat(captured_at) if captured_at is not None else None
    provider_quote_time = copy.deepcopy(data.get("provider_quote_time"))
    if data.get("provider_quote_time") is not None:
        quote_time = data["provider_quote_time"]
        if isinstance(quote_time, dict) and quote_time.get("state") in ("unknown", "not_applicable"):
            if quote_time.get("value") is not None:
                _issue(metadata, "provider_quote_time", "unknown or not-applicable state conflicts with its timestamp")
                provider_quote_time.pop("value", None)
        else:
            timestamp = quote_time.get("value") if isinstance(quote_time, dict) else quote_time
            parsed_quote = _instant(timestamp, "provider_quote_time", metadata)
            if parsed_quote is not None and parsed_quote > (capture_time or receipt_time):
                _issue(metadata, "provider_quote_time", "is later than the known observation or recording time")
                parsed_quote = None
            if parsed_quote is None:
                provider_quote_time = {"state": "unknown", "reason": "Reported quote time is unreliable; original retained in raw."}
            elif isinstance(quote_time, dict):
                provider_quote_time["value"] = _utc(parsed_quote)
            else:
                provider_quote_time = _utc(parsed_quote)
    source_valid_until = _metadata_time(data.get("source_valid_until"), "source_valid_until", metadata)
    if data.get("kind") not in (None, "quote", "signal"):
        _issue(common, "kind", "must be quote or signal")
    if data.get("evidence_status") not in (None, "verified_observed", "ai_proposed", "research_observed", "rejected", "no_result"):
        _issue(common, "evidence_status", "unsupported observation status")
    is_signal = (data.get("kind") == "signal" or data.get("evidence_status") in
                 ("ai_proposed", "research_observed", "rejected", "no_result"))
    movements: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    movement_issues: dict[str, list[str]] = {}
    raw_movements = data.get("movements", [])
    if not isinstance(raw_movements, list):
        _issue(common, "movements", "must be an array")
        raw_movements = []
    extra_issues: list[str] = []
    for index, movement_raw in enumerate(raw_movements):
        field = f"movements[{index}]"
        problems: list[str] = []
        movement = copy.deepcopy(movement_raw) if isinstance(movement_raw, dict) else {"raw": copy.deepcopy(movement_raw)}
        if not isinstance(movement_raw, dict):
            _issue(problems, field, "must be an object")
        identifier = movement.get("movement_id")
        for key in ("movement_id", "mode", "from_node", "to_node"):
            _text(movement.get(key), f"{field}.{key}", problems)
        departure = _instant(movement.get("departure"), f"{field}.departure", problems)
        arrival = _instant(movement.get("arrival"), f"{field}.arrival", problems)
        if departure is not None and arrival is not None and arrival <= departure:
            _issue(problems, field, "arrival must follow departure")
        if movement.get("mode") == "flight":
            identity = movement.get("flight_identity")
            if identity is None:
                _issue(problems, field + ".flight_identity", "required", missing=True)
            elif not isinstance(identity, dict):
                _issue(problems, field + ".flight_identity", "must be an object")
            else:
                carrier = identity.get("marketing_carrier") or identity.get("operating_carrier")
                _text(carrier, field + ".flight_identity.carrier", problems)
                numbers = identity.get("flight_numbers")
                if numbers is None or numbers == []:
                    _issue(problems, field + ".flight_identity.flight_numbers", "required for a complete flight quote", missing=True)
                elif not isinstance(numbers, list) or not all(_has_text(item) for item in numbers):
                    _issue(problems, field + ".flight_identity.flight_numbers", "must be an array of non-empty strings")
        if _has_text(identifier):
            if identifier in by_id:
                _issue(problems, field + ".movement_id", "duplicate identifier")
                movement_issues[identifier].extend(problems)
            else:
                by_id[identifier] = movement
                movement_issues[identifier] = problems
        else:
            extra_issues.extend(problems)
        movement["admission"] = _admission(problems)
        movements.append(movement)
    offers: list[dict[str, Any]] = []
    raw_offers = data.get("offers", [])
    if not isinstance(raw_offers, list):
        _issue(common, "offers", "must be an array")
        raw_offers = []
    for index, offer_raw in enumerate(raw_offers):
        field = f"offers[{index}]"
        problems = list(common)
        offer_metadata = list(metadata)
        offer = copy.deepcopy(offer_raw) if isinstance(offer_raw, dict) else {"raw": copy.deepcopy(offer_raw)}
        if not isinstance(offer_raw, dict):
            _issue(problems, field, "must be an object")
        if not _has_text(offer.get("offer_id")):
            offer["offer_id"] = _identifier([observation_id, index, offer_raw], "offer-")
        stage = offer.get("verification_stage")
        if stage is not None and (not isinstance(stage, str) or stage not in {
            "unknown", "listing", "selected_itinerary", "fare_review", "checkout"
        }):
            _issue(problems, field + ".verification_stage", "must be unknown, listing, selected_itinerary, fare_review or checkout")
        _money(offer.get("price"), field + ".price", problems)
        if offer.get("price_scope") is None:
            _issue(problems, field + ".price_scope", "party_total must be explicit", missing=True)
        elif offer["price_scope"] != "party_total":
            _issue(problems, field + ".price_scope", "must be party_total; no per-person conversion is inferred")
        count = offer.get("traveler_count")
        if count is None:
            _issue(problems, field + ".traveler_count", "required", missing=True)
        elif isinstance(count, bool) or not isinstance(count, int) or count < 1:
            _issue(problems, field + ".traveler_count", "must be a positive integer")
        offer_seller = offer.get("seller", seller)
        _text(offer_seller, field + ".seller", problems)
        # A meta-search page often names the airline as seller; keep the offer's
        # own seller and note the difference instead of discarding the price.
        if seller is not None and offer_seller != seller:
            _issue(offer_metadata, field + ".seller", "differs from the source seller; offer seller kept")
        if offer_seller is not None:
            offer["seller"] = offer_seller
        if offer.get("provider") is not None and offer["provider"] != provider:
            _issue(offer_metadata, field + ".provider", "differs from the observation provider; originals retained in raw")
        offer_capture_status = capture_time_status
        if offer.get("observed_at") is not None:
            offer_time = _instant(offer["observed_at"], field + ".observed_at", offer_metadata)
            if offer_time is not None and capture_time is not None and offer_time != capture_time:
                _issue(offer_metadata, field + ".observed_at", "conflicts with observation capture time")
                offer_time = None
            elif offer_time is not None and offer_time > receipt_time:
                _issue(offer_metadata, field + ".observed_at", "is later than recording time")
                offer_time = None
            offer["observed_at"] = _utc(offer_time) if offer_time is not None else None
            if offer_time is None:
                offer_capture_status = "unreliable"
        for key in ("expires_at", "provider_price_valid_until"):
            if key in offer:
                offer[key] = _metadata_time(offer[key], field + "." + key, offer_metadata)
        for key in ("booking_url", "url"):
            if key in offer:
                offer[key] = _url(offer[key], field + "." + key, offer_metadata)
        for key in ("mandatory_baggage_cost", "required_accommodation_cost"):
            if key in offer and offer[key] is not None:
                _money(offer[key], field + "." + key, problems)
        refs = offer.get("movement_ids")
        if refs is None or refs == []:
            _issue(problems, field + ".movement_ids", "non-empty list required", missing=True)
        elif not isinstance(refs, list) or not all(_has_text(item) for item in refs):
            _issue(problems, field + ".movement_ids", "must be an array of non-empty identifiers")
        else:
            if len(set(refs)) != len(refs):
                _issue(problems, field + ".movement_ids", "contains duplicate identifiers")
            for identifier in refs:
                if identifier not in by_id:
                    _issue(problems, field + ".movement_ids", f"references absent movement {identifier}", missing=True)
                else:
                    problems.extend(movement_issues[identifier])
        offer["admission"] = _admission(problems, signal=is_signal)
        offer["metadata_issues"] = list(dict.fromkeys(offer_metadata))
        offer["capture_time_status"] = offer_capture_status
        offers.append(offer)
    issues = list(common) + extra_issues
    for offer in offers:
        issues.extend(offer["admission"]["issues"])
    if not offers and not is_signal:
        _issue(issues, "offers", "at least one payable Offer required", missing=True)
    admission = _admission(issues, signal=is_signal)
    if any(offer["admission"]["status"] == "admitted" for offer in offers):
        admission["status"] = "admitted"
    result = {
        "observation_id": observation_id, "raw": copy.deepcopy(raw),
        "source": source, "captured_at": captured_at, "received_at": received_at,
        "admission": admission, "movements": movements, "offers": offers,
        "capture_time_status": capture_time_status,
        "metadata_issues": list(dict.fromkeys(metadata + [issue for offer in offers for issue in offer["metadata_issues"]])),
    }
    if "provider_quote_time" in data:
        result["provider_quote_time"] = provider_quote_time
    if "source_valid_until" in data:
        result["source_valid_until"] = source_valid_until
    return result
