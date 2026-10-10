#!/usr/bin/env python3
"""Evaluate airport-internal transfer feasibility from synthetic or observed topology.

This module models airport operations only.  A border-control transition is an
operational step and is never interpreted as a passenger eligibility decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable


ZONE_TYPES = {"domestic", "schengen", "non_schengen", "international"}
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class AirportTransitError(ValueError):
    """Raised when catalog or request data violates the module contract."""


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AirportTransitError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AirportTransitError(f"{label} must be an array")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AirportTransitError(f"{label} must be a non-empty string")
    return value


def _clock_minutes(value: str, label: str) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AirportTransitError(f"{label} must use HH:MM") from exc
    if hour == 24 and minute == 0:
        return 24 * 60
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise AirportTransitError(f"{label} is outside 00:00-24:00")
    return hour * 60 + minute


def _validate_intervals(value: Any, label: str) -> None:
    intervals = _require_list(value, label)
    if not intervals:
        raise AirportTransitError(f"{label} must not be empty")
    for index, raw_interval in enumerate(intervals):
        interval = _require_mapping(raw_interval, f"{label}[{index}]")
        days = _require_list(interval.get("days"), f"{label}[{index}].days")
        if not days or any(day not in DAY_NAMES for day in days):
            raise AirportTransitError(
                f"{label}[{index}].days must contain mon..sun values"
            )
        _clock_minutes(interval.get("start"), f"{label}[{index}].start")
        _clock_minutes(interval.get("end"), f"{label}[{index}].end")


def validate_catalog(catalog: dict[str, Any]) -> None:
    """Validate the catalog without requiring third-party JSON Schema packages."""

    catalog = _require_mapping(catalog, "catalog")
    _require_string(catalog.get("catalog_version"), "catalog.catalog_version")
    airports = _require_list(catalog.get("airports"), "catalog.airports")
    airport_ids: set[str] = set()

    for airport_index, raw_airport in enumerate(airports):
        prefix = f"catalog.airports[{airport_index}]"
        airport = _require_mapping(raw_airport, prefix)
        airport_id = _require_string(airport.get("airport_id"), f"{prefix}.airport_id")
        if airport_id in airport_ids:
            raise AirportTransitError(f"duplicate airport_id: {airport_id}")
        airport_ids.add(airport_id)
        _require_string(airport.get("timezone"), f"{prefix}.timezone")

        terminals = _require_list(airport.get("terminals"), f"{prefix}.terminals")
        terminal_ids: set[str] = set()
        nodes: set[tuple[str, str]] = set()
        for terminal_index, raw_terminal in enumerate(terminals):
            terminal_prefix = f"{prefix}.terminals[{terminal_index}]"
            terminal = _require_mapping(raw_terminal, terminal_prefix)
            terminal_id = _require_string(
                terminal.get("terminal_id"), f"{terminal_prefix}.terminal_id"
            )
            if terminal_id in terminal_ids:
                raise AirportTransitError(
                    f"duplicate terminal_id {terminal_id} in {airport_id}"
                )
            terminal_ids.add(terminal_id)
            _validate_intervals(
                terminal.get("open_intervals"),
                f"{terminal_prefix}.open_intervals",
            )
            zones = _require_list(terminal.get("zones"), f"{terminal_prefix}.zones")
            zone_ids: set[str] = set()
            for zone_index, raw_zone in enumerate(zones):
                zone_prefix = f"{terminal_prefix}.zones[{zone_index}]"
                zone = _require_mapping(raw_zone, zone_prefix)
                zone_id = _require_string(zone.get("zone_id"), f"{zone_prefix}.zone_id")
                if zone_id in zone_ids:
                    raise AirportTransitError(
                        f"duplicate zone_id {zone_id} in {airport_id}/{terminal_id}"
                    )
                zone_ids.add(zone_id)
                zone_type = zone.get("zone_type")
                if zone_type not in ZONE_TYPES:
                    raise AirportTransitError(
                        f"{zone_prefix}.zone_type must be one of {sorted(ZONE_TYPES)}"
                    )
                if not isinstance(zone.get("airside_overnight_allowed"), bool):
                    raise AirportTransitError(
                        f"{zone_prefix}.airside_overnight_allowed must be boolean"
                    )
                _validate_intervals(
                    zone.get("open_intervals"), f"{zone_prefix}.open_intervals"
                )
                nodes.add((terminal_id, zone_id))

        connection_ids: set[str] = set()
        for connection_index, raw_connection in enumerate(
            _require_list(airport.get("connections", []), f"{prefix}.connections")
        ):
            connection_prefix = f"{prefix}.connections[{connection_index}]"
            connection = _require_mapping(raw_connection, connection_prefix)
            connection_id = _require_string(
                connection.get("connection_id"),
                f"{connection_prefix}.connection_id",
            )
            if connection_id in connection_ids:
                raise AirportTransitError(
                    f"duplicate connection_id {connection_id} in {airport_id}"
                )
            connection_ids.add(connection_id)
            for endpoint_name in ("from", "to"):
                endpoint = _require_mapping(
                    connection.get(endpoint_name),
                    f"{connection_prefix}.{endpoint_name}",
                )
                node = (
                    _require_string(
                        endpoint.get("terminal_id"),
                        f"{connection_prefix}.{endpoint_name}.terminal_id",
                    ),
                    _require_string(
                        endpoint.get("zone_id"),
                        f"{connection_prefix}.{endpoint_name}.zone_id",
                    ),
                )
                if node not in nodes:
                    raise AirportTransitError(
                        f"{connection_prefix}.{endpoint_name} references unknown node {node}"
                    )
            minimum = connection.get("minimum_transfer_minutes")
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise AirportTransitError(
                    f"{connection_prefix}.minimum_transfer_minutes must be >= 0 integer"
                )
            for flag in (
                "airside_connection",
                "requires_border_control",
                "requires_landside_exit",
                "requires_security_recheck",
            ):
                if not isinstance(connection.get(flag), bool):
                    raise AirportTransitError(f"{connection_prefix}.{flag} must be boolean")
            _validate_intervals(
                connection.get("open_intervals"),
                f"{connection_prefix}.open_intervals",
            )

        lounge_ids: set[str] = set()
        for lounge_index, raw_lounge in enumerate(
            _require_list(airport.get("lounges", []), f"{prefix}.lounges")
        ):
            lounge_prefix = f"{prefix}.lounges[{lounge_index}]"
            lounge = _require_mapping(raw_lounge, lounge_prefix)
            lounge_id = _require_string(lounge.get("lounge_id"), f"{lounge_prefix}.lounge_id")
            if lounge_id in lounge_ids:
                raise AirportTransitError(f"duplicate lounge_id {lounge_id} in {airport_id}")
            lounge_ids.add(lounge_id)
            location = _require_mapping(lounge.get("location"), f"{lounge_prefix}.location")
            node = (
                _require_string(
                    location.get("terminal_id"), f"{lounge_prefix}.location.terminal_id"
                ),
                _require_string(
                    location.get("zone_id"), f"{lounge_prefix}.location.zone_id"
                ),
            )
            if node not in nodes:
                raise AirportTransitError(
                    f"{lounge_prefix}.location references unknown node {node}"
                )
            max_stay = lounge.get("maximum_stay_minutes")
            if not isinstance(max_stay, int) or isinstance(max_stay, bool) or max_stay <= 0:
                raise AirportTransitError(
                    f"{lounge_prefix}.maximum_stay_minutes must be positive integer"
                )
            _validate_intervals(
                lounge.get("open_intervals"), f"{lounge_prefix}.open_intervals"
            )


def load_catalog(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    validate_catalog(catalog)
    return catalog


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AirportTransitError(f"{label} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AirportTransitError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AirportTransitError(f"{label} must include an explicit UTC offset")
    return parsed


def _make_wall_datetime(day: date, minute_of_day: int, tzinfo: Any) -> datetime:
    if minute_of_day == 24 * 60:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=tzinfo)
    return datetime.combine(
        day,
        time(hour=minute_of_day // 60, minute=minute_of_day % 60),
        tzinfo=tzinfo,
    )


def _schedule_windows(
    intervals: Iterable[dict[str, Any]], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = start.date() - timedelta(days=1)
    final_day = end.date() + timedelta(days=1)
    while cursor <= final_day:
        day_name = DAY_NAMES[cursor.weekday()]
        for interval in intervals:
            if day_name not in interval["days"]:
                continue
            start_minutes = _clock_minutes(interval["start"], "interval.start")
            end_minutes = _clock_minutes(interval["end"], "interval.end")
            window_start = _make_wall_datetime(cursor, start_minutes, start.tzinfo)
            window_end = _make_wall_datetime(cursor, end_minutes, start.tzinfo)
            if end_minutes <= start_minutes and end_minutes != 24 * 60:
                window_end += timedelta(days=1)
            if window_end >= start and window_start <= end:
                windows.append((window_start, window_end))
        cursor += timedelta(days=1)
    windows.sort()
    merged: list[tuple[datetime, datetime]] = []
    for window_start, window_end in windows:
        if not merged or window_start > merged[-1][1]:
            merged.append((window_start, window_end))
        elif window_end > merged[-1][1]:
            merged[-1] = (merged[-1][0], window_end)
    return merged


def _schedule_covers(
    intervals: Iterable[dict[str, Any]], start: datetime, end: datetime
) -> bool:
    if end < start:
        return False
    check_end = end if end > start else start + timedelta(microseconds=1)
    return any(
        window_start <= start and window_end >= check_end
        for window_start, window_end in _schedule_windows(intervals, start, check_end)
    )


def _maximum_schedule_overlap_minutes(
    intervals: Iterable[dict[str, Any]], start: datetime, end: datetime
) -> int:
    maximum = 0
    for window_start, window_end in _schedule_windows(intervals, start, end):
        overlap_start = max(window_start, start)
        overlap_end = min(window_end, end)
        if overlap_end > overlap_start:
            maximum = max(maximum, int((overlap_end - overlap_start).total_seconds() // 60))
    return maximum


def _airport_by_id(catalog: dict[str, Any], airport_id: str) -> dict[str, Any]:
    for airport in catalog["airports"]:
        if airport["airport_id"] == airport_id:
            return airport
    raise AirportTransitError(f"unknown airport_id: {airport_id}")


def _airport_indexes(
    airport: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    terminals: dict[str, dict[str, Any]] = {}
    zones: dict[tuple[str, str], dict[str, Any]] = {}
    for terminal in airport["terminals"]:
        terminals[terminal["terminal_id"]] = terminal
        for zone in terminal["zones"]:
            zones[(terminal["terminal_id"], zone["zone_id"])] = zone
    lounges = {lounge["lounge_id"]: lounge for lounge in airport.get("lounges", [])}
    return terminals, zones, lounges


def _request_endpoint(
    request: dict[str, Any], name: str, zones: dict[tuple[str, str], dict[str, Any]]
) -> tuple[tuple[str, str], datetime]:
    endpoint = _require_mapping(request.get(name), f"request.{name}")
    node = (
        _require_string(endpoint.get("terminal_id"), f"request.{name}.terminal_id"),
        _require_string(endpoint.get("zone_id"), f"request.{name}.zone_id"),
    )
    if node not in zones:
        raise AirportTransitError(f"request.{name} references unknown node {node}")
    timestamp = _parse_datetime(
        endpoint.get("local_datetime"), f"request.{name}.local_datetime"
    )
    return node, timestamp


def _all_simple_paths(
    airport: dict[str, Any],
    start: tuple[str, str],
    destination: tuple[str, str],
    airside_only: bool,
) -> list[list[dict[str, Any]]]:
    if start == destination:
        return [[]]
    adjacency: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for connection in airport.get("connections", []):
        if airside_only and (
            not connection["airside_connection"] or connection["requires_landside_exit"]
        ):
            continue
        source = (
            connection["from"]["terminal_id"],
            connection["from"]["zone_id"],
        )
        adjacency.setdefault(source, []).append(connection)
    for edges in adjacency.values():
        edges.sort(key=lambda item: item["connection_id"])

    found: list[list[dict[str, Any]]] = []

    def walk(node: tuple[str, str], visited: set[tuple[str, str]], path: list[dict[str, Any]]) -> None:
        for connection in adjacency.get(node, []):
            target = (connection["to"]["terminal_id"], connection["to"]["zone_id"])
            if target in visited:
                continue
            next_path = path + [connection]
            if target == destination:
                found.append(next_path)
            else:
                walk(target, visited | {target}, next_path)

    walk(start, {start}, [])
    found.sort(
        key=lambda path: (
            sum(item["minimum_transfer_minutes"] for item in path),
            [item["connection_id"] for item in path],
        )
    )
    return found


def _location_reasons(
    terminals: dict[str, dict[str, Any]],
    zones: dict[tuple[str, str], dict[str, Any]],
    node: tuple[str, str],
    start: datetime,
    end: datetime,
) -> list[str]:
    terminal = terminals[node[0]]
    zone = zones[node]
    reasons: list[str] = []
    if not _schedule_covers(terminal["open_intervals"], start, end):
        reasons.append("TERMINAL_NOT_OPEN_FOR_STAY")
    if not _schedule_covers(zone["open_intervals"], start, end):
        reasons.append("ZONE_NOT_OPEN_FOR_STAY")
    if start.date() != end.date() and not zone["airside_overnight_allowed"]:
        reasons.append("AIRSIDE_OVERNIGHT_NOT_ALLOWED")
    return reasons


def _path_flags(path: Iterable[dict[str, Any]]) -> dict[str, bool]:
    path = list(path)
    return {
        "airside_connection": bool(path) and all(edge["airside_connection"] for edge in path),
        "requires_border_control": any(edge["requires_border_control"] for edge in path),
        "requires_landside_exit": any(edge["requires_landside_exit"] for edge in path),
        "requires_security_recheck": any(edge["requires_security_recheck"] for edge in path),
    }


def _evaluate_path(
    path: list[dict[str, Any]],
    start_node: tuple[str, str],
    departure: datetime,
    arrival: datetime,
    terminals: dict[str, dict[str, Any]],
    zones: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[str], datetime]:
    reasons: list[str] = []
    current_node = start_node
    current_time = arrival
    for connection in path:
        finish = current_time + timedelta(minutes=connection["minimum_transfer_minutes"])
        reasons.extend(
            _location_reasons(terminals, zones, current_node, current_time, finish)
        )
        if not _schedule_covers(connection["open_intervals"], current_time, finish):
            reasons.append("CONNECTION_NOT_OPEN")
        current_node = (
            connection["to"]["terminal_id"],
            connection["to"]["zone_id"],
        )
        current_time = finish
    reasons.extend(
        _location_reasons(terminals, zones, current_node, current_time, departure)
    )
    return list(dict.fromkeys(reasons)), current_time


def _lounge_evaluation(
    request: dict[str, Any],
    final_node: tuple[str, str],
    available_start: datetime,
    departure: datetime,
    lounges: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any] | None]:
    raw_requirement = request.get("lounge_requirement")
    if raw_requirement is None:
        return [], None
    requirement = _require_mapping(raw_requirement, "request.lounge_requirement")
    lounge_id = _require_string(
        requirement.get("lounge_id"), "request.lounge_requirement.lounge_id"
    )
    minimum = requirement.get("minimum_stay_minutes", 0)
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise AirportTransitError(
            "request.lounge_requirement.minimum_stay_minutes must be >= 0 integer"
        )
    lounge = lounges.get(lounge_id)
    if lounge is None:
        return ["LOUNGE_NOT_FOUND"], {"lounge_id": lounge_id, "usable_minutes": 0}
    location = (lounge["location"]["terminal_id"], lounge["location"]["zone_id"])
    if location != final_node:
        return ["LOUNGE_NOT_ACCESSIBLE_FROM_DEPARTURE_ZONE"], {
            "lounge_id": lounge_id,
            "usable_minutes": 0,
        }
    schedule_overlap = _maximum_schedule_overlap_minutes(
        lounge["open_intervals"], available_start, departure
    )
    usable = min(schedule_overlap, lounge["maximum_stay_minutes"])
    reasons = [] if usable >= minimum else ["LOUNGE_REQUIREMENT_UNMET"]
    return reasons, {
        "lounge_id": lounge_id,
        "terminal_id": location[0],
        "zone_id": location[1],
        "maximum_stay_minutes": lounge["maximum_stay_minutes"],
        "usable_minutes": usable,
    }


def evaluate_transfer(catalog: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one airport transfer request and return an auditable result."""

    validate_catalog(catalog)
    request = _require_mapping(request, "request")
    airport_id = _require_string(request.get("airport_id"), "request.airport_id")
    airport = _airport_by_id(catalog, airport_id)
    terminals, zones, lounges = _airport_indexes(airport)
    arrival_node, arrival_time = _request_endpoint(request, "arrival", zones)
    departure_node, departure_time = _request_endpoint(request, "departure", zones)
    if departure_time <= arrival_time:
        raise AirportTransitError("departure.local_datetime must be after arrival.local_datetime")
    available_minutes = int((departure_time - arrival_time).total_seconds() // 60)
    airside_only = request.get("airside_only", False)
    if not isinstance(airside_only, bool):
        raise AirportTransitError("request.airside_only must be boolean")

    paths = _all_simple_paths(airport, arrival_node, departure_node, airside_only)
    base = {
        "airport_id": airport_id,
        "arrival_node": {"terminal_id": arrival_node[0], "zone_id": arrival_node[1]},
        "departure_node": {"terminal_id": departure_node[0], "zone_id": departure_node[1]},
        "available_connection_minutes": available_minutes,
        "airside_only": airside_only,
        "eligibility_assessed": False,
    }
    if not paths:
        return {
            **base,
            "feasible": False,
            "reason_codes": ["NO_TOPOLOGY_PATH"],
            "minimum_transfer_minutes": None,
            "connection_ids": [],
            "operational_steps": _path_flags([]),
            "lounge": None,
        }

    shortest_minutes = sum(edge["minimum_transfer_minutes"] for edge in paths[0])
    if shortest_minutes > available_minutes:
        return {
            **base,
            "feasible": False,
            "reason_codes": ["INSUFFICIENT_CONNECTION_TIME"],
            "minimum_transfer_minutes": shortest_minutes,
            "connection_ids": [edge["connection_id"] for edge in paths[0]],
            "operational_steps": _path_flags(paths[0]),
            "lounge": None,
        }

    first_failure: dict[str, Any] | None = None
    for path in paths:
        minimum_minutes = sum(edge["minimum_transfer_minutes"] for edge in path)
        if minimum_minutes > available_minutes:
            continue
        reasons, path_finish = _evaluate_path(
            path,
            arrival_node,
            departure_time,
            arrival_time,
            terminals,
            zones,
        )
        lounge_reasons, lounge = _lounge_evaluation(
            request, departure_node, path_finish, departure_time, lounges
        )
        reasons = list(dict.fromkeys(reasons + lounge_reasons))
        candidate = {
            **base,
            "feasible": not reasons,
            "reason_codes": reasons,
            "minimum_transfer_minutes": minimum_minutes,
            "connection_ids": [edge["connection_id"] for edge in path],
            "operational_steps": _path_flags(path),
            "lounge": lounge,
        }
        if not reasons:
            return candidate
        if first_failure is None:
            first_failure = candidate

    assert first_failure is not None
    return first_failure


def evaluate_cases(
    catalog: dict[str, Any], cases_document: dict[str, Any]
) -> list[dict[str, Any]]:
    cases = _require_list(cases_document.get("cases"), "cases_document.cases")
    results: list[dict[str, Any]] = []
    for index, raw_case in enumerate(cases):
        case = _require_mapping(raw_case, f"cases_document.cases[{index}]")
        case_id = _require_string(case.get("case_id"), f"cases_document.cases[{index}].case_id")
        result = evaluate_transfer(
            catalog,
            _require_mapping(case.get("request"), f"cases_document.cases[{index}].request"),
        )
        results.append({"case_id": case_id, **result})
    return results


def _read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate airport operating topology")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a catalog")
    validate_parser.add_argument("--catalog", required=True)
    evaluate_parser = subparsers.add_parser("evaluate", help="evaluate a cases document")
    evaluate_parser.add_argument("--catalog", required=True)
    evaluate_parser.add_argument("--cases", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        if args.command == "validate":
            payload: Any = {"valid": True, "airport_count": len(catalog["airports"])}
        else:
            payload = {"results": evaluate_cases(catalog, _read_json(args.cases))}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (AirportTransitError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
