"""Round-trip Work Item schedule derivation and admission compatibility."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


COMPATIBILITY_VERSION = "1.0"


class TaskCompatibilityError(ValueError):
    pass


def _datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskCompatibilityError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskCompatibilityError(f"{field} must include a UTC offset")
    return parsed


def derive_positioning_windows(
    flights: list[dict[str, Any]],
    *,
    outbound_hub_node_id: str,
    return_hub_node_id: str,
    connection_buffer_minutes: int,
    discovery_horizon_minutes: int,
) -> dict[str, dict[str, Any]]:
    """Derive positioning windows from verified long-haul Movements."""

    ordered = sorted(
        flights,
        key=lambda item: _datetime(item.get("departure"), "movement.departure"),
    )
    outbound_first = next(
        (
            flight
            for flight in ordered
            if flight.get("from_node") == outbound_hub_node_id
        ),
        None,
    )
    return_last = next(
        (
            flight
            for flight in reversed(ordered)
            if flight.get("to_node") == return_hub_node_id
        ),
        None,
    )
    if outbound_first is None or return_last is None:
        raise TaskCompatibilityError(
            "verified long-haul Movements do not expose both remote hub endpoints"
        )
    buffer = timedelta(minutes=connection_buffer_minutes)
    horizon = timedelta(minutes=discovery_horizon_minutes)
    outbound_deadline = _datetime(
        outbound_first["departure"], "outbound_long_haul.departure"
    ) - buffer
    outbound_start = outbound_deadline - horizon
    return_start = _datetime(
        return_last["arrival"], "return_long_haul.arrival"
    ) + buffer
    return_end = return_start + horizon

    outbound_dates = [outbound_deadline.date().isoformat()]
    if outbound_start.date() != outbound_deadline.date():
        outbound_dates.append(outbound_start.date().isoformat())
    return_dates = [return_start.date().isoformat()]
    if return_end.date() != return_start.date():
        return_dates.append(return_end.date().isoformat())

    return {
        "outbound": {
            "departure_window": {
                "start": outbound_start.isoformat(),
                "end": outbound_deadline.isoformat(),
            },
            "exact_departure_dates": outbound_dates,
            "schedule_context": {
                "must_arrive_by": outbound_deadline.isoformat(),
                "connecting_movement_id": outbound_first["movement_id"],
            },
        },
        "return": {
            "departure_window": {
                "start": return_start.isoformat(),
                "end": return_end.isoformat(),
            },
            "exact_departure_dates": return_dates,
            "schedule_context": {
                "not_before": return_start.isoformat(),
                "connecting_movement_id": return_last["movement_id"],
            },
        },
    }


def path_schedule_issues(
    movements: dict[str, dict[str, Any]],
    path: list[str],
    *,
    departure_window: dict[str, Any],
    exact_departure_dates: list[str] | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> list[str]:
    """Return every published schedule rule violated by one Movement path."""

    if not path:
        return ["path is empty"]
    first = movements[path[0]]
    last = movements[path[-1]]
    departure = first.get("_departure") or _datetime(
        first.get("departure"), "movement.departure"
    )
    arrival = last.get("_arrival") or _datetime(
        last.get("arrival"), "movement.arrival"
    )
    start = _datetime(departure_window.get("start"), "departure_window.start")
    end = _datetime(departure_window.get("end"), "departure_window.end")
    issues = []
    if not start <= departure <= end:
        issues.append("departure is outside departure_window")
    allowed_dates = set(exact_departure_dates or [])
    if allowed_dates and departure.date().isoformat() not in allowed_dates:
        issues.append("departure is outside exact_departure_dates")
    schedule = schedule_context or {}
    if schedule.get("not_before") is not None and departure < _datetime(
        schedule["not_before"], "schedule_context.not_before"
    ):
        issues.append("departure is before not_before")
    if schedule.get("must_arrive_by") is not None and arrival > _datetime(
        schedule["must_arrive_by"], "schedule_context.must_arrive_by"
    ):
        issues.append("arrival is after must_arrive_by")
    return issues
