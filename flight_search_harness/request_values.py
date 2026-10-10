"""Canonical public-request values used by Product Adapters.

The public request contract deliberately uses one vocabulary for values shared
by every journey shape.  Product Kernels keep their independent internal
contracts; the concrete Product Adapter translates these normalized values at
the Product Adapter Seam.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import HarnessProtocolError


_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SELF_TRANSFER_POLICIES = {"allow", "avoid", "never"}
_ENDPOINT_POLICIES = {"anchor_required", "any_admitted_gateway"}
_GROUND_MODES = {
    "rail",
    "bus",
    "coach",
    "ferry",
    "local_transit",
    "walk",
    "rideshare",
    "car",
}
_ENVELOPE_FIELDS = {"request_version", "request_id", "journey_shape", "intent", "metadata"}
_SHARED_FIELDS = {
    "base_currency",
    "traveler_count",
    "baggage_policy",
    "overnight_allowed",
    "self_transfer_policy",
    "fx_to_base",
    "fx_snapshot",
}
_PRODUCT_FIELDS = {
    "one_way": {
        "origin",
        "destination",
        "departure_window",
        "date_flexibility",
        "budget",
        "allowed_modes",
        "strategy",
        "maximum_total_duration_minutes",
        "maximum_provider_queries",
    },
    "random_loop": {
        "departure_airports",
        "return_airports",
        "first_departure_window",
        "maximum_total_duration_minutes",
        "min_legs",
        "max_legs",
        "connection_buffer_minutes",
        "max_connection_wait_minutes",
        "first_leg_earliest_local_time",
        "airport_transit",
        "maximum_provider_queries",
        "quality_gate",
        "airport_catalog_ref",
        "airport_transit_catalog_ref",
    },
    "round_trip": {
        "origin",
        "destination",
        "outbound_departure_window",
        "return_departure_window",
        "outbound_date_flexibility",
        "return_date_flexibility",
        "minimum_stay_nights",
        "maximum_stay_nights",
        "budget",
        "allowed_modes",
        "maximum_total_duration_minutes",
        "airport_transit_unknown_policy",
        "fx_snapshot",
    },
}
_PRODUCT_REQUIRED = {
    "one_way": {"origin", "destination", "departure_window"},
    "random_loop": {
        "departure_airports",
        "return_airports",
        "first_departure_window",
        "maximum_total_duration_minutes",
        "min_legs",
        "max_legs",
        "connection_buffer_minutes",
        "max_connection_wait_minutes",
        "quality_gate",
    },
    "round_trip": {
        "origin",
        "destination",
        "outbound_departure_window",
        "return_departure_window",
    },
}

_REQUEST_SCHEMA_FILES = {
    "one_way": "one-way-request.schema.json",
    "random_loop": "random-loop-request.schema.json",
    "round_trip": "round-trip-request.schema.json",
}
_REQUEST_SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas" / "requests"


class _SchemaMismatch(ValueError):
    pass


@lru_cache(maxsize=None)
def _schema_document(name: str) -> dict[str, Any]:
    try:
        value = json.loads((_REQUEST_SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessProtocolError(f"cannot load public request Schema {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessProtocolError(f"public request Schema {name} must be an object")
    return value


def _schema_pointer(document: Any, fragment: str) -> Any:
    current = document
    if fragment in {"", "#"}:
        return current
    if not fragment.startswith("#/"):
        raise HarnessProtocolError(f"unsupported public Schema reference: {fragment}")
    for raw_token in fragment[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise HarnessProtocolError(f"unresolved public Schema reference: {fragment}")
        current = current[token]
    return current


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise HarnessProtocolError(f"unsupported public Schema type: {expected}")


def _validate_string_format(value: str, format_name: str, path: str) -> None:
    try:
        if format_name == "date":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                raise ValueError
            date.fromisoformat(value)
        elif format_name == "date-time":
            if "T" not in value:
                raise ValueError
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        elif format_name == "uri":
            if not urlsplit(value).scheme:
                raise ValueError
    except ValueError as exc:
        raise _SchemaMismatch(f"{path} must use {format_name} format") from exc


def _validate_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    document_name: str,
    path: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        reference_name, separator, raw_fragment = reference.partition("#")
        target_name = reference_name or document_name
        target = _schema_pointer(
            _schema_document(target_name),
            f"#{raw_fragment}" if separator else "",
        )
        if not isinstance(target, dict):
            raise HarnessProtocolError(f"public Schema reference is not an object: {reference}")
        _validate_schema_value(
            value,
            target,
            document_name=target_name,
            path=path,
        )

    for member in schema.get("allOf", []):
        _validate_schema_value(value, member, document_name=document_name, path=path)

    if "anyOf" in schema:
        if not any(
            _schema_branch_matches(
                value, member, document_name=document_name, path=path
            )
            for member in schema["anyOf"]
        ):
            raise _SchemaMismatch(f"{path} must match an allowed shape")

    if "oneOf" in schema:
        matches = 0
        for member in schema["oneOf"]:
            try:
                _validate_schema_value(
                    value, member, document_name=document_name, path=path
                )
                matches += 1
            except _SchemaMismatch:
                pass
        if matches != 1:
            raise _SchemaMismatch(f"{path} must match exactly one allowed shape")

    conditional = schema.get("if")
    if isinstance(conditional, dict):
        try:
            _validate_schema_value(
                value, conditional, document_name=document_name, path=path
            )
        except _SchemaMismatch:
            pass
        else:
            consequence = schema.get("then")
            if isinstance(consequence, dict):
                _validate_schema_value(
                    value, consequence, document_name=document_name, path=path
                )

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if isinstance(expected_types, list) and not any(
        _schema_type_matches(value, str(expected)) for expected in expected_types
    ):
        raise _SchemaMismatch(f"{path} has an invalid type")
    if "const" in schema and value != schema["const"]:
        raise _SchemaMismatch(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise _SchemaMismatch(f"{path} has an unsupported value")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise _SchemaMismatch(f"{path} is missing required fields: {', '.join(missing)}")
        minimum_properties = schema.get("minProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            raise _SchemaMismatch(f"{path} has too few fields")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, dict):
            for name in value:
                _validate_schema_value(
                    name,
                    property_names,
                    document_name=document_name,
                    path=f"{path} field name",
                )
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, member in properties.items():
                if name in value:
                    _validate_schema_value(
                        value[name],
                        member,
                        document_name=document_name,
                        path=f"{path}.{name}",
                    )
            extras = set(value) - set(properties)
            additional = schema.get("additionalProperties", True)
            if additional is False and extras:
                raise _SchemaMismatch(
                    f"{path} has unsupported fields: {', '.join(sorted(extras))}"
                )
            if isinstance(additional, dict):
                for name in extras:
                    _validate_schema_value(
                        value[name],
                        additional,
                        document_name=document_name,
                        path=f"{path}.{name}",
                    )

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise _SchemaMismatch(f"{path} has too few items")
        if schema.get("uniqueItems") is True:
            canonical = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(canonical) != len(set(canonical)):
                raise _SchemaMismatch(f"{path} items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(
                    item,
                    item_schema,
                    document_name=document_name,
                    path=f"{path}[{index}]",
                )
        contains_schema = schema.get("contains")
        if isinstance(contains_schema, dict) and not any(
            _schema_branch_matches(
                item,
                contains_schema,
                document_name=document_name,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ):
            raise _SchemaMismatch(f"{path} does not contain a required item")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise _SchemaMismatch(f"{path} is too short")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise _SchemaMismatch(f"{path} has an invalid format")
        format_name = schema.get("format")
        if isinstance(format_name, str):
            _validate_string_format(value, format_name, path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise _SchemaMismatch(f"{path} must be finite")
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _SchemaMismatch(f"{path} must be at least {minimum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            raise _SchemaMismatch(f"{path} must be greater than {exclusive_minimum}")


def _validate_public_schema(request: dict[str, Any], shape: str) -> None:
    schema_name = _REQUEST_SCHEMA_FILES[shape]
    try:
        _validate_schema_value(
            request,
            _schema_document(schema_name),
            document_name=schema_name,
            path="request",
        )
    except _SchemaMismatch as exc:
        raise HarnessProtocolError(str(exc)) from exc


def _schema_branch_matches(
    value: Any,
    schema: dict[str, Any],
    *,
    document_name: str,
    path: str,
) -> bool:
    try:
        _validate_schema_value(
            value, schema, document_name=document_name, path=path
        )
    except _SchemaMismatch:
        return False
    return True


def _reject_unknown(raw: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise HarnessProtocolError(
            f"{field} has unsupported fields: " + ", ".join(unknown)
        )


def _parsed_window(
    window: dict[str, str], field: str
) -> tuple[date | datetime, date | datetime]:
    parsed: list[date | datetime] = []
    kinds: list[type] = []
    for key in ("start", "end"):
        raw = window[key]
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("offset required")
            kinds.append(datetime)
            parsed.append(value)
        except ValueError:
            try:
                value = date.fromisoformat(raw)
            except ValueError as exc:
                raise HarnessProtocolError(
                    f"intent.{field}.{key} must be an ISO date or offset date-time"
                ) from exc
            kinds.append(date)
            parsed.append(value)
    if kinds[0] is not kinds[1]:
        raise HarnessProtocolError(
            f"intent.{field} start and end must use the same date representation"
        )
    if parsed[1] <= parsed[0]:
        raise HarnessProtocolError(f"intent.{field} must be strictly increasing")
    return parsed[0], parsed[1]


def validate_fx_snapshot_freshness(
    snapshot: Any,
    *,
    now_utc: datetime | None = None,
) -> None:
    """Require a source-attributed FX snapshot to be current at evaluation time.

    Product Adapters call this at both request admission and finalization.  The
    function is stateless so a resumed Run never relies on its start-time
    freshness decision.
    """

    if snapshot is None:
        return
    if not isinstance(snapshot, dict):
        raise HarnessProtocolError("intent.fx_snapshot must be an object")
    try:
        observed_at = datetime.fromisoformat(
            str(snapshot.get("observed_at") or "").replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            str(snapshot.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise HarnessProtocolError(
            "intent.fx_snapshot timestamps must be offset date-times"
        ) from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise HarnessProtocolError(
            "intent.fx_snapshot.observed_at must include a UTC offset"
        )
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise HarnessProtocolError(
            "intent.fx_snapshot.expires_at must include a UTC offset"
        )
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise HarnessProtocolError("FX freshness evaluation time must include a UTC offset")
    current = current.astimezone(timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= observed_at:
        raise HarnessProtocolError(
            "intent.fx_snapshot.expires_at must be after observed_at"
        )
    if observed_at > current:
        raise HarnessProtocolError(
            "intent.fx_snapshot.observed_at must not be in the future"
        )
    if current >= expires_at:
        raise HarnessProtocolError("intent.fx_snapshot is expired")


def _validate_effective_regional_modes(
    intent: dict[str, Any],
    origin: dict[str, Any],
    destination: dict[str, Any],
) -> None:
    allowed_modes = set(
        intent.get("allowed_modes", ["flight", *sorted(_GROUND_MODES)])
    )
    for field, side in (("origin", origin), ("destination", destination)):
        effective_ground_modes = set(
            side.get("allowed_ground_modes", sorted(_GROUND_MODES))
        )
        missing = sorted(effective_ground_modes - allowed_modes)
        if missing:
            raise HarnessProtocolError(
                "intent.allowed_modes excludes effective "
                f"intent.{field}.allowed_ground_modes: {', '.join(missing)}"
            )


def _require_anchor_utc_offsets(side: dict[str, Any], field: str) -> None:
    for index, anchor in enumerate(side["anchor_nodes"]):
        offset = anchor.get("utc_offset")
        if not isinstance(offset, str) or not offset.strip():
            raise HarnessProtocolError(
                f"intent.{field}.anchor_nodes[{index}].utc_offset is required "
                "for a date-only departure window"
            )


def validate_public_request(request: Any) -> dict[str, Any]:
    """Validate the strict public request-v2 surface before creating a Run."""

    if not isinstance(request, dict):
        raise HarnessProtocolError("Search Mission start requires request")
    _reject_unknown(request, _ENVELOPE_FIELDS, "request")
    if request.get("request_version") != "2.0":
        raise HarnessProtocolError("request_version must be 2.0")
    shape = request.get("journey_shape")
    if shape not in _PRODUCT_FIELDS:
        raise HarnessProtocolError("request.journey_shape is unsupported")
    _validate_public_schema(request, shape)
    intent = request.get("intent")
    if not isinstance(intent, dict):
        raise HarnessProtocolError("request.intent must be an object")
    _reject_unknown(intent, _SHARED_FIELDS | _PRODUCT_FIELDS[shape], "intent")
    required_fields = (_SHARED_FIELDS - {"fx_snapshot"}) | _PRODUCT_REQUIRED[shape]
    missing = sorted(required_fields - set(intent))
    if missing:
        raise HarnessProtocolError(
            "request.intent is missing required fields: " + ", ".join(missing)
        )
    shared = canonical_shared_intent(intent)

    snapshot = intent.get("fx_snapshot")
    if len(shared["fx_to_base"]) > 1 and not isinstance(snapshot, dict):
        raise HarnessProtocolError(
            "non-Base-Currency comparison requires source-attributed intent.fx_snapshot"
        )
    if isinstance(snapshot, dict):
        if (
            snapshot.get("base_currency") != shared["base_currency"]
            or snapshot.get("rates") != shared["fx_to_base"]
        ):
            raise HarnessProtocolError(
                "intent.fx_snapshot must match base_currency and fx_to_base"
            )
        for field in ("source_url", "provider", "observed_at", "expires_at"):
            if not isinstance(snapshot.get(field), str) or not snapshot[field].strip():
                raise HarnessProtocolError(f"intent.fx_snapshot.{field} is required")
        validate_fx_snapshot_freshness(snapshot)

    if shape in {"one_way", "round_trip"}:
        origin = canonical_region_side(intent, "origin")
        destination = canonical_region_side(intent, "destination")
        if shape == "one_way" and any(
            side["endpoint_policy"] == "any_admitted_gateway"
            for side in (origin, destination)
        ):
            raise HarnessProtocolError(
                "one-way endpoint_policy must be anchor_required until gateway "
                "admission is explicitly evidence-backed"
            )
        allowed_modes = intent.get("allowed_modes")
        if shape == "one_way" and allowed_modes is not None:
            if not isinstance(allowed_modes, list) or "flight" not in allowed_modes:
                raise HarnessProtocolError(
                    "one-way intent.allowed_modes must include flight"
                )
        _validate_effective_regional_modes(intent, origin, destination)
        for field, money in (
            ("budget", intent.get("budget")),
            ("origin.max_ground_cost", origin.get("max_ground_cost")),
            ("destination.max_ground_cost", destination.get("max_ground_cost")),
        ):
            if (
                isinstance(money, dict)
                and money.get("currency") not in shared["fx_to_base"]
            ):
                raise HarnessProtocolError(
                    f"intent.{field}.currency requires a matching fx_to_base rate"
                )

    windows: dict[str, tuple[date, date]] = {}
    if shape == "one_way":
        canonical_window(intent, "departure_window")
        windows["outbound"] = _parsed_window(
            canonical_window(intent, "departure_window"), "departure_window"
        )
        flexibility = intent.get("date_flexibility")
        if isinstance(flexibility, dict):
            start, end = windows["outbound"]
            start_date = start.date() if isinstance(start, datetime) else start
            end_date = end.date() if isinstance(end, datetime) else end
            for raw_date in flexibility.get("exact_departure_dates", []):
                selected = date.fromisoformat(str(raw_date))
                if selected < start_date or selected > end_date:
                    raise HarnessProtocolError(
                        "intent.date_flexibility exact date is outside "
                        "departure_window"
                    )
    elif shape == "random_loop":
        canonical_window(intent, "first_departure_window")
        _parsed_window(
            canonical_window(intent, "first_departure_window"),
            "first_departure_window",
        )
        if intent["min_legs"] > intent["max_legs"]:
            raise HarnessProtocolError(
                "intent.min_legs must not exceed intent.max_legs"
            )
        if (
            intent["max_connection_wait_minutes"]
            <= intent["connection_buffer_minutes"]
        ):
            raise HarnessProtocolError(
                "intent.max_connection_wait_minutes must exceed "
                "intent.connection_buffer_minutes"
            )
        minimum_efficiency = intent["quality_gate"][
            "minimum_complete_loop_efficiency_km_per_base_currency_unit"
        ]
        target_efficiency = intent["quality_gate"].get(
            "target_complete_loop_efficiency_km_per_base_currency_unit"
        )
        if (
            target_efficiency is not None
            and target_efficiency < minimum_efficiency
        ):
            raise HarnessProtocolError(
                "intent.quality_gate target efficiency must not be below its minimum"
            )
    else:
        for direction in ("outbound", "return"):
            field = f"{direction}_departure_window"
            windows[direction] = _parsed_window(canonical_window(intent, field), field)
            start, _ = windows[direction]
            if isinstance(start, date) and not isinstance(start, datetime):
                relevant_side = origin if direction == "outbound" else destination
                _require_anchor_utc_offsets(
                    relevant_side,
                    "origin" if direction == "outbound" else "destination",
                )
            flexibility = intent.get(f"{direction}_date_flexibility")
            if isinstance(flexibility, dict):
                for raw_date in flexibility.get("exact_departure_dates", []):
                    selected = date.fromisoformat(str(raw_date))
                    start, end = windows[direction]
                    start_date = start.date() if isinstance(start, datetime) else start
                    end_date = end.date() if isinstance(end, datetime) else end
                    if selected < start_date or selected > end_date:
                        raise HarnessProtocolError(
                            f"intent.{direction}_date_flexibility exact date is outside "
                            f"{field}"
                        )
        minimum_stay = intent.get("minimum_stay_nights")
        maximum_stay = intent.get("maximum_stay_nights")
        if (
            minimum_stay is not None
            and maximum_stay is not None
            and minimum_stay > maximum_stay
        ):
            raise HarnessProtocolError(
                "intent.minimum_stay_nights must not exceed maximum_stay_nights"
            )
    return copy.deepcopy(request)


def canonical_shared_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy values shared by every public v2 intent."""

    base_currency = intent.get("base_currency")
    if not isinstance(base_currency, str) or not _CURRENCY_PATTERN.fullmatch(
        base_currency
    ):
        raise HarnessProtocolError(
            "intent.base_currency must be an uppercase three-letter currency code"
        )

    traveler_count = intent.get("traveler_count")
    if (
        isinstance(traveler_count, bool)
        or not isinstance(traveler_count, int)
        or traveler_count < 1
    ):
        raise HarnessProtocolError("intent.traveler_count must be a positive integer")

    baggage_policy = intent.get("baggage_policy")
    if not isinstance(baggage_policy, dict):
        raise HarnessProtocolError("intent.baggage_policy must be an object")
    _reject_unknown(
        baggage_policy,
        {"cabin_baggage", "checked_bag_count", "paid_baggage_allowed"},
        "intent.baggage_policy",
    )
    cabin_baggage = baggage_policy.get("cabin_baggage")
    checked_bag_count = baggage_policy.get("checked_bag_count")
    paid_baggage_allowed = baggage_policy.get("paid_baggage_allowed")
    if not isinstance(cabin_baggage, str) or not cabin_baggage.strip():
        raise HarnessProtocolError(
            "intent.baggage_policy.cabin_baggage must be a non-empty string"
        )
    if (
        isinstance(checked_bag_count, bool)
        or not isinstance(checked_bag_count, int)
        or checked_bag_count < 0
    ):
        raise HarnessProtocolError(
            "intent.baggage_policy.checked_bag_count must be a non-negative integer"
        )
    if not isinstance(paid_baggage_allowed, bool):
        raise HarnessProtocolError(
            "intent.baggage_policy.paid_baggage_allowed must be boolean"
        )

    overnight_allowed = intent.get("overnight_allowed")
    if not isinstance(overnight_allowed, bool):
        raise HarnessProtocolError("intent.overnight_allowed must be boolean")
    self_transfer_policy = intent.get("self_transfer_policy")
    if self_transfer_policy not in _SELF_TRANSFER_POLICIES:
        raise HarnessProtocolError(
            "intent.self_transfer_policy must be allow, avoid, or never"
        )

    fx_to_base = intent.get("fx_to_base")
    if not isinstance(fx_to_base, dict) or not fx_to_base:
        raise HarnessProtocolError("intent.fx_to_base must be a non-empty object")
    normalized_rates: dict[str, float] = {}
    for raw_currency, raw_rate in fx_to_base.items():
        currency = str(raw_currency).upper()
        if not _CURRENCY_PATTERN.fullmatch(currency):
            raise HarnessProtocolError(
                f"intent.fx_to_base.{raw_currency} has an invalid currency code"
            )
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
            raise HarnessProtocolError(
                f"intent.fx_to_base.{raw_currency} must be a positive number"
            )
        rate = float(raw_rate)
        if not math.isfinite(rate) or rate <= 0:
            raise HarnessProtocolError(
                f"intent.fx_to_base.{raw_currency} must be a positive number"
            )
        normalized_rates[currency] = rate
    if base_currency not in normalized_rates or not math.isclose(
        normalized_rates[base_currency], 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise HarnessProtocolError(
            f"intent.fx_to_base.{base_currency} is required and must equal 1"
        )

    return {
        "base_currency": base_currency,
        "traveler_count": traveler_count,
        "baggage_policy": {
            "cabin_baggage": cabin_baggage.strip(),
            "checked_bag_count": checked_bag_count,
            "paid_baggage_allowed": paid_baggage_allowed,
        },
        "overnight_allowed": overnight_allowed,
        "self_transfer_policy": self_transfer_policy,
        "fx_to_base": normalized_rates,
    }


def canonical_window(intent: dict[str, Any], field: str) -> dict[str, str]:
    """Copy a required canonical departure window for product validation."""

    window = intent.get(field)
    if not isinstance(window, dict):
        raise HarnessProtocolError(f"intent.{field} must be an object")
    start = window.get("start")
    end = window.get("end")
    if not isinstance(start, str) or not start.strip():
        raise HarnessProtocolError(f"intent.{field}.start must be a non-empty string")
    if not isinstance(end, str) or not end.strip():
        raise HarnessProtocolError(f"intent.{field}.end must be a non-empty string")
    return {"start": start.strip(), "end": end.strip()}


def canonical_region_side(intent: dict[str, Any], field: str) -> dict[str, Any]:
    """Copy a fixed-destination region side without choosing a gateway."""

    side = intent.get(field)
    if not isinstance(side, dict):
        raise HarnessProtocolError(f"intent.{field} must be an object")
    _reject_unknown(
        side,
        {
            "region_id",
            "label",
            "country_codes",
            "policy",
            "endpoint_policy",
            "anchor_nodes",
            "seed_gateway_nodes",
            "max_access_time_minutes",
            "max_egress_time_minutes",
            "max_ground_cost",
            "allowed_ground_modes",
        },
        f"intent.{field}",
    )
    anchors = side.get("anchor_nodes")
    gateways = side.get("seed_gateway_nodes", [])
    if not isinstance(anchors, list) or not anchors:
        raise HarnessProtocolError(
            f"intent.{field}.anchor_nodes must be a non-empty array"
        )
    if not isinstance(gateways, list):
        raise HarnessProtocolError(
            f"intent.{field}.seed_gateway_nodes must be an array"
        )
    if not all(isinstance(node, dict) for node in [*anchors, *gateways]):
        raise HarnessProtocolError(
            f"intent.{field} anchor and seed gateway nodes must be objects"
        )
    endpoint_policy = side.get("endpoint_policy")
    if endpoint_policy not in _ENDPOINT_POLICIES:
        raise HarnessProtocolError(
            f"intent.{field}.endpoint_policy must be anchor_required or "
            "any_admitted_gateway"
        )
    if side.get("policy") == "exact" and endpoint_policy != "anchor_required":
        raise HarnessProtocolError(
            f"intent.{field}.policy=exact requires endpoint_policy=anchor_required"
        )
    normalized = copy.deepcopy(side)
    normalized["anchor_nodes"] = copy.deepcopy(anchors)
    normalized["seed_gateway_nodes"] = copy.deepcopy(gateways)
    normalized["endpoint_policy"] = endpoint_policy
    return normalized


def derived_intent_id(
    journey_shape: str,
    intent: dict[str, Any],
    request_id: Any = None,
) -> str:
    """Derive an internal Product Kernel intent ID from public request values."""

    identity_input = {
        "journey_shape": journey_shape,
        "request_id": request_id if isinstance(request_id, str) else None,
        "intent": intent,
    }
    canonical = json.dumps(
        identity_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{journey_shape.replace('_', '-')}-{digest}"


def one_way_baggage(baggage_policy: dict[str, Any]) -> dict[str, Any]:
    """Translate the public baggage vocabulary to the one-way Profile."""

    cabin = baggage_policy["cabin_baggage"]
    checked = baggage_policy["checked_bag_count"]
    mode = cabin if checked == 0 else f"{cabin}; checked_bag_count={checked}"
    return {
        "mode": mode,
        "paid_baggage": baggage_policy["paid_baggage_allowed"],
    }


def round_trip_baggage(baggage_policy: dict[str, Any]) -> dict[str, Any]:
    """Translate the public baggage vocabulary to round-trip required items."""

    required_items = [baggage_policy["cabin_baggage"]]
    checked = baggage_policy["checked_bag_count"]
    if checked:
        required_items.append(
            "1 checked bag" if checked == 1 else f"{checked} checked bags"
        )
    return {
        "required_items": required_items,
        "paid_baggage_allowed": baggage_policy["paid_baggage_allowed"],
    }


def money_in_base_currency(
    value: dict[str, Any] | None,
    shared: dict[str, Any],
    field: str,
) -> dict[str, Any] | None:
    """Translate public Money to Product Kernel Base Currency deterministically."""

    if value is None:
        return None
    currency = str(value["currency"])
    rate = shared["fx_to_base"].get(currency)
    if rate is None:
        raise HarnessProtocolError(
            f"{field}.currency requires a matching fx_to_base rate"
        )
    return {
        "amount": float(value["amount"]) * float(rate),
        "currency": shared["base_currency"],
    }

