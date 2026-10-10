from __future__ import annotations

"""Residual-budget backward search for observed multimodal offers.

This is a deterministic, provider-independent kernel.  It searches only priced,
currently eligible edges.  Unpriced, expired, or future observations can create
bounded research requests, but can never become routes or zero-cost edges.
"""

import argparse
import hashlib
import heapq
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


ENGINE_VERSION = "0.7.1"
SEARCH_MODEL = "residual_budget_backward_recursive"


def _display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = os.path.relpath(resolved, Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return Path(relative).as_posix()


BUDGET_PRESETS: dict[str, dict[str, int]] = {
    "standard": {
        "max_hops": 4,
        "max_labels_per_node": 8,
        "query_budget": 8,
        "geography_quota": 2,
        "mode_quota": 3,
        "max_trace_entries": 2500,
    },
    "deep": {
        "max_hops": 6,
        "max_labels_per_node": 20,
        "query_budget": 24,
        "geography_quota": 6,
        "mode_quota": 8,
        "max_trace_entries": 8000,
    },
    "extreme": {
        "max_hops": 9,
        "max_labels_per_node": 40,
        "query_budget": 64,
        "geography_quota": 16,
        "mode_quota": 20,
        "max_trace_entries": 20000,
    },
}

INTENSITY_ALIASES = {
    "deep_savings": "deep",
    "extreme_exploration": "extreme",
}

SURFACE_MODES = {
    "rail", "bus", "coach", "ferry", "local_transit", "walk", "rideshare", "car"
}
MODE_TRANSITION_BUFFER_KEYS = (
    "surface_to_flight",
    "self_transfer_to_flight",
    "flight_to_flight",
    "flight_to_surface",
    "surface_to_surface",
)

DEFAULTS: dict[str, Any] = {
    "minimum_connection_minutes": 0,
    "max_total_minutes": 7 * 24 * 60,
    "default_query_cost": 1.0,
    "allowed_research_modes": [
        "flight",
        "rail",
        "bus",
        "coach",
        "ferry",
        "local_transit",
    ],
    "search_as_of": None,
    "currency_decimal_places": None,
    "strict_improvement_delta": None,
    "mode_transition_buffers_minutes": {
        key: 0 for key in MODE_TRANSITION_BUFFER_KEYS
    },
}


class ResidualBudgetSearchError(ValueError):
    pass


class _Trace:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: list[dict[str, Any]] = []
        self.reason_counts: Counter[str] = Counter()
        self.total = 0

    def record(self, stage: str, reason: str, **details: Any) -> None:
        self.total += 1
        self.reason_counts[reason] += 1
        if len(self.items) < self.limit:
            self.items.append(
                {
                    "sequence": self.total,
                    "stage": stage,
                    "reason": reason,
                    **details,
                }
            )


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ResidualBudgetSearchError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < minimum:
        raise ResidualBudgetSearchError(f"{field} must be finite and at least {minimum}")
    return result


def _decimal(value: Any, field: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ResidualBudgetSearchError(f"{field} must be decimal numeric") from exc
    if not result.is_finite() or result < minimum:
        raise ResidualBudgetSearchError(f"{field} must be finite and at least {minimum}")
    return result


def _to_minor(value: Any, field: str, minor_unit: int) -> int:
    decimal = _decimal(value, field)
    scaled = decimal * (Decimal(10) ** minor_unit)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ResidualBudgetSearchError(
            f"{field} has more than {minor_unit} currency decimal places"
        )
    return int(integral)


def _money_out(value: int, minor_unit: int) -> int | float:
    if minor_unit == 0:
        return value
    decimal = Decimal(value) / (Decimal(10) ** minor_unit)
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(format(decimal, f".{minor_unit}f"))


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    result = int(_number(value, field, minimum=minimum))
    if result != float(value):
        raise ResidualBudgetSearchError(f"{field} must be an integer")
    return result


def _normalize_mode_transition_buffers(raw: Any) -> dict[str, int]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ResidualBudgetSearchError(
            "config.mode_transition_buffers_minutes must be an object"
        )
    unknown = sorted(set(raw) - set(MODE_TRANSITION_BUFFER_KEYS))
    if unknown:
        raise ResidualBudgetSearchError(
            "config.mode_transition_buffers_minutes has unsupported keys: "
            + ", ".join(unknown)
        )
    result = {key: 0 for key in MODE_TRANSITION_BUFFER_KEYS}
    for key, value in raw.items():
        result[key] = _integer(
            value, f"config.mode_transition_buffers_minutes.{key}", minimum=0
        )
    return result


def _transition_kind(
    previous_mode: str,
    next_mode: str,
    previous_ticket_group: Any = None,
    next_ticket_group: Any = None,
) -> str:
    if previous_mode == "flight" and next_mode == "flight":
        if (
            previous_ticket_group
            and next_ticket_group
            and previous_ticket_group == next_ticket_group
        ):
            return "flight_to_flight"
        return "self_transfer_to_flight"
    if previous_mode in SURFACE_MODES and next_mode == "flight":
        return "surface_to_flight"
    if previous_mode == "flight" and next_mode in SURFACE_MODES:
        return "flight_to_surface"
    return "surface_to_surface"


def _transition_buffer(
    previous_mode: str,
    next_mode: str,
    settings: dict[str, Any],
    previous_ticket_group: Any = None,
    next_ticket_group: Any = None,
) -> tuple[float, str]:
    kind = _transition_kind(
        previous_mode,
        next_mode,
        previous_ticket_group,
        next_ticket_group,
    )
    configured = settings["mode_transition_buffers_minutes"][kind]
    return max(settings["minimum_connection_minutes"], configured), kind


def _buffer_before_suffix(
    candidate_modes: Iterable[str],
    suffix: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[float, list[str]]:
    if not suffix.get("modes"):
        return settings["minimum_connection_minutes"], []
    next_mode = suffix["modes"][0]
    next_group = suffix.get("ticket_groups", (None,))[0]
    candidates = [
        _transition_buffer(mode, next_mode, settings, None, next_group)
        for mode in candidate_modes
    ]
    if not candidates:
        return settings["minimum_connection_minutes"], []
    required = max(minutes for minutes, _ in candidates)
    return required, sorted({kind for minutes, kind in candidates if minutes == required})


def _dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ResidualBudgetSearchError(f"{field} must be an ISO date-time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResidualBudgetSearchError(f"{field} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise ResidualBudgetSearchError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _settings(bundle: dict[str, Any]) -> dict[str, Any]:
    raw = bundle.get("config", {})
    if not isinstance(raw, dict):
        raise ResidualBudgetSearchError("config must be an object")
    requested = str(raw.get("search_intensity", "standard")).strip().lower()
    requested = INTENSITY_ALIASES.get(requested, requested)
    if requested not in BUDGET_PRESETS:
        raise ResidualBudgetSearchError("search_intensity must be standard, deep, or extreme")
    settings = {**DEFAULTS, **BUDGET_PRESETS[requested], **raw}
    settings["search_intensity"] = requested
    for key in (
        "max_hops",
        "max_labels_per_node",
        "query_budget",
        "geography_quota",
        "mode_quota",
        "max_trace_entries",
    ):
        settings[key] = _integer(settings[key], f"config.{key}", minimum=1)
    settings["currency_decimal_places"] = _integer(
        settings["currency_decimal_places"],
        "config.currency_decimal_places",
        minimum=0,
    )
    if settings["currency_decimal_places"] > 6:
        raise ResidualBudgetSearchError(
            "config.currency_decimal_places must not exceed 6"
        )
    for key in ("minimum_connection_minutes", "max_total_minutes", "default_query_cost"):
        settings[key] = _number(settings[key], f"config.{key}")
    if settings["default_query_cost"] <= 0:
        raise ResidualBudgetSearchError("config.default_query_cost must be positive")
    modes = settings.get("allowed_research_modes")
    if not isinstance(modes, list) or not all(isinstance(mode, str) and mode.strip() for mode in modes):
        raise ResidualBudgetSearchError("config.allowed_research_modes must be a string array")
    settings["allowed_research_modes"] = list(dict.fromkeys(mode.strip().lower() for mode in modes))
    settings["mode_transition_buffers_minutes"] = _normalize_mode_transition_buffers(
        settings.get("mode_transition_buffers_minutes")
    )

    date_window = raw.get("date_window", bundle.get("date_window"))
    if not isinstance(date_window, dict):
        raise ResidualBudgetSearchError("date_window with start and end is required")
    start = _dt(date_window.get("start"), "date_window.start")
    end = _dt(date_window.get("end"), "date_window.end")
    if end <= start:
        raise ResidualBudgetSearchError("date_window.end must be after date_window.start")
    settings["_date_start"] = start
    settings["_date_end"] = end
    settings["date_window"] = {"start": _iso(start), "end": _iso(end)}
    return settings


def _nodes(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        source = [dict(value, node_id=value.get("node_id", key)) for key, value in raw.items()]
    else:
        source = raw
    if not isinstance(source, list):
        raise ResidualBudgetSearchError("nodes must be an array or mapping")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise ResidualBudgetSearchError(f"nodes[{index}] must be an object")
        node_id = str(item.get("node_id", "")).strip().upper()
        if not node_id or node_id in result:
            raise ResidualBudgetSearchError(f"nodes[{index}] has a missing or duplicate node_id")
        result[node_id] = {**item, "node_id": node_id}
    return result


def _pool(raw: Any, field: str, nodes: dict[str, dict[str, Any]]) -> set[str]:
    if isinstance(raw, dict):
        raw = raw.get("node_ids", raw.get("nodes"))
    if not isinstance(raw, list) or not raw:
        raise ResidualBudgetSearchError(f"{field} must be a non-empty node array")
    result = {str(item).strip().upper() for item in raw}
    missing = sorted(result - set(nodes))
    if missing:
        raise ResidualBudgetSearchError(f"{field} references unknown nodes: {', '.join(missing)}")
    return result


def _incumbent(raw: Any, minor_unit: int) -> dict[str, Any]:
    edge_ids: list[str] = []
    source = None
    if isinstance(raw, dict):
        amount = raw.get("total_cost", raw.get("amount", raw.get("cost")))
        raw_ids = raw.get("edge_ids", [])
        if not isinstance(raw_ids, list):
            raise ResidualBudgetSearchError("incumbent.edge_ids must be an array")
        edge_ids = [str(item) for item in raw_ids]
        source = raw.get("source")
    else:
        amount = raw
    value = _to_minor(amount, "incumbent.total_cost", minor_unit)
    if value <= 0:
        raise ResidualBudgetSearchError("incumbent.total_cost must be positive")
    return {"total_cost": value, "edge_ids": edge_ids, "source": source}


def _money_amount(value: Any, field: str, minor_unit: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if str(value.get("status", "known")).strip().lower() == "unknown":
            return None
        if "amount" not in value:
            return None
        value = value["amount"]
    return _to_minor(value, field, minor_unit)


def _source(raw: Any, edge_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ResidualBudgetSearchError(f"priced edge {edge_id} needs source evidence")
    required = ("provider_id", "url", "observed_at", "evidence_type", "confidence")
    if any(raw.get(key) in (None, "") for key in required):
        raise ResidualBudgetSearchError(
            f"priced edge {edge_id} source needs provider_id, url, observed_at, evidence_type, and confidence"
        )
    confidence = _number(raw["confidence"], f"edge {edge_id}.source.confidence")
    if confidence > 1:
        raise ResidualBudgetSearchError(f"edge {edge_id}.source.confidence must not exceed 1")
    result = dict(raw)
    result["_observed_at"] = _dt(raw["observed_at"], f"edge {edge_id}.source.observed_at")
    result["_expires_at"] = (
        _dt(raw["expires_at"], f"edge {edge_id}.source.expires_at") if raw.get("expires_at") else None
    )
    return result


def _edges(raw: Any, nodes: dict[str, dict[str, Any]], minor_unit: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ResidualBudgetSearchError("edges must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    atomic_ids: set[str] = set()
    atomic_components: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ResidualBudgetSearchError(f"edges[{index}] must be an object")
        edge = dict(item)
        edge_id = str(edge.get("edge_id", "")).strip()
        start = str(edge.get("from_node", "")).strip().upper()
        end = str(edge.get("to_node", "")).strip().upper()
        if not edge_id or edge_id in seen:
            raise ResidualBudgetSearchError(f"edges[{index}] has a missing or duplicate edge_id")
        if start not in nodes or end not in nodes:
            raise ResidualBudgetSearchError(f"edge {edge_id} references an unknown node")
        cost = _money_amount(
            edge.get("effective_cost", edge.get("cost")), f"edge {edge_id}.cost", minor_unit
        )
        baggage = _money_amount(
            edge.get("mandatory_baggage_cost", 0),
            f"edge {edge_id}.mandatory_baggage_cost",
            minor_unit,
        )
        if baggage is None:
            baggage = 0.0
        if cost is not None:
            cost += baggage
        departure = _dt(edge["departure"], f"edge {edge_id}.departure") if edge.get("departure") else None
        arrival = _dt(edge["arrival"], f"edge {edge_id}.arrival") if edge.get("arrival") else None
        if (departure is None) != (arrival is None):
            raise ResidualBudgetSearchError(f"edge {edge_id} must provide both departure and arrival, or neither")
        if cost is not None and departure is None:
            raise ResidualBudgetSearchError(f"priced edge {edge_id} needs departure and arrival")
        if departure is not None and arrival <= departure:
            raise ResidualBudgetSearchError(f"edge {edge_id} must arrive after departure")
        source = _source(edge.get("source"), edge_id) if cost is not None else None
        research = edge.get("research", {})
        if not isinstance(research, dict):
            raise ResidualBudgetSearchError(f"edge {edge_id}.research must be an object")

        atomic_id = None
        ticket = edge.get("ticket_context")
        if ticket is not None:
            if not isinstance(ticket, dict) or ticket.get("pricing_scope") != "atomic_offer":
                raise ResidualBudgetSearchError(
                    f"edge {edge_id}.ticket_context.pricing_scope must be atomic_offer"
                )
            atomic_id = str(ticket.get("atomic_offer_id", "")).strip()
            components = ticket.get("component_edge_ids")
            if not atomic_id or not isinstance(components, list) or len(components) < 2:
                raise ResidualBudgetSearchError(
                    f"edge {edge_id} atomic offer needs atomic_offer_id and at least two component_edge_ids"
                )
            if atomic_id in atomic_ids:
                raise ResidualBudgetSearchError(
                    f"atomic offer {atomic_id} must be one hyperedge, not multiple priced edges"
                )
            atomic_ids.add(atomic_id)
            atomic_components.update(str(component) for component in components)

        result.append(
            {
                **edge,
                "edge_id": edge_id,
                "from_node": start,
                "to_node": end,
                "mode": str(edge.get("mode", "unknown")).strip().lower(),
                "_cost": cost,
                "_departure": departure,
                "_arrival": arrival,
                "_source": source,
                "_research": research,
                "_atomic_offer_id": atomic_id,
                "_snapshot_status": "not_evaluated",
            }
        )
        seen.add(edge_id)
    split = sorted(edge["edge_id"] for edge in result if edge["_cost"] is not None and edge["edge_id"] in atomic_components)
    if split:
        raise ResidualBudgetSearchError(
            "atomic offer component edges must not carry separate prices: " + ", ".join(split)
        )
    return sorted(result, key=lambda edge: edge["edge_id"])


def _geography(node: dict[str, Any]) -> str:
    return str(
        node.get("geography_bucket")
        or node.get("country_code")
        or node.get("country_or_region")
        or "unknown"
    ).strip().upper()


def _mode_bucket(modes: Iterable[str]) -> str:
    unique = sorted(set(modes))
    return "+".join(unique) if unique else "seed"


def _label_id(seed: str, edge_ids: tuple[str, ...]) -> str:
    token = ">".join(edge_ids) if edge_ids else "seed"
    digest = hashlib.sha1(f"{seed}|{token}".encode("utf-8")).hexdigest()[:12]
    return f"reverse-{digest}"


def _seed_label(node: str, price_cutoff: int) -> dict[str, Any]:
    return {
        "id": _label_id(node, ()),
        "seed": node,
        "node": node,
        "edge_ids": (),
        "node_path": (node,),
        "modes": (),
        "ticket_groups": (),
        "atomic_offer_ids": (),
        "cost": 0.0,
        "hops": 0,
        "departure": None,
        "arrival": None,
        "travel_minutes": 0.0,
        "remaining_budget_at_creation": price_cutoff,
        "observed_expansion_pruned": False,
    }


def _extend(label: dict[str, Any], edge: dict[str, Any], price_cutoff: int) -> dict[str, Any]:
    edge_ids = (edge["edge_id"],) + label["edge_ids"]
    departure = edge["_departure"]
    arrival = label["arrival"] or edge["_arrival"]
    atomic_ids = label["atomic_offer_ids"]
    if edge["_atomic_offer_id"]:
        atomic_ids = (edge["_atomic_offer_id"],) + atomic_ids
    ticket_groups = (edge.get("independent_ticket_group"),) + label["ticket_groups"]
    return {
        "id": _label_id(label["seed"], edge_ids),
        "seed": label["seed"],
        "node": edge["from_node"],
        "edge_ids": edge_ids,
        "node_path": (edge["from_node"],) + label["node_path"],
        "modes": (edge["mode"],) + label["modes"],
        "ticket_groups": ticket_groups,
        "atomic_offer_ids": atomic_ids,
        "cost": label["cost"] + edge["_cost"],
        "hops": label["hops"] + 1,
        "departure": departure,
        "arrival": arrival,
        "travel_minutes": (arrival - departure).total_seconds() / 60.0,
        "remaining_budget_at_creation": price_cutoff - label["cost"] - edge["_cost"],
        "observed_expansion_pruned": False,
    }


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    time_ok = (
        left["departure"] is not None
        and right["departure"] is not None
        and left["arrival"] is not None
        and right["arrival"] is not None
        and left["departure"] >= right["departure"]
        and left["arrival"] <= right["arrival"]
    )
    return (
        left["cost"] <= right["cost"]
        and left["hops"] <= right["hops"]
        and time_ok
        and (
            left["cost"] < right["cost"]
            or left["hops"] < right["hops"]
            or left["departure"] > right["departure"]
            or left["arrival"] < right["arrival"]
        )
    )


def _prefix_cost_lower_bounds(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    origins: set[str],
) -> dict[str, int | None]:
    """Admissible only for paths composed from the eligible observed graph."""
    outgoing: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        if edge["_snapshot_status"] == "eligible" and edge["_cost"] is not None:
            outgoing[edge["from_node"]].append((edge["to_node"], edge["_cost"]))
    distances: dict[str, int | None] = {node: None for node in nodes}
    heap: list[tuple[int, str]] = []
    for origin in sorted(origins):
        distances[origin] = 0.0
        heapq.heappush(heap, (0.0, origin))
    while heap:
        distance, node = heapq.heappop(heap)
        if distances[node] is None or distance != distances[node]:
            continue
        for neighbour, cost in sorted(outgoing.get(node, [])):
            candidate = distance + cost
            if distances[neighbour] is None or candidate < distances[neighbour]:
                distances[neighbour] = candidate
                heapq.heappush(heap, (candidate, neighbour))
    return distances


def _serial_label(
    label: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    final_price_cutoff: int,
    minor_unit: int,
    pareto_ceiling: int,
) -> dict[str, Any]:
    remaining = final_price_cutoff - label["cost"]
    return {
        "label_id": label["id"],
        "seed_destination": label["seed"],
        "node": label["node"],
        "edge_ids": list(label["edge_ids"]),
        "node_path": list(label["node_path"]),
        "known_suffix_cost": _money_out(label["cost"], minor_unit),
        "remaining_budget": _money_out(remaining, minor_unit),
        "remaining_budget_at_creation": _money_out(label["remaining_budget_at_creation"], minor_unit),
        "within_final_price_cutoff": remaining >= 0 or (label["hops"] == 0),
        "channel_membership": {
            "price_improvement": remaining >= 0 or (label["hops"] == 0),
            "pareto_exploration": label["cost"] <= pareto_ceiling,
        },
        "hops": label["hops"],
        "departure": _iso(label["departure"]),
        "arrival": _iso(label["arrival"]),
        "travel_minutes": round(label["travel_minutes"], 6),
        "geography_bucket": _geography(nodes[label["node"]]),
        "mode_bucket": _mode_bucket(label["modes"]),
        "modes": list(label["modes"]),
        "ticket_groups": list(label["ticket_groups"]),
        "atomic_offer_ids": list(dict.fromkeys(label["atomic_offer_ids"])),
        "observed_expansion_pruned": label["observed_expansion_pruned"],
    }


def _route(label: dict[str, Any], minor_unit: int) -> dict[str, Any]:
    digest = hashlib.sha1(">".join(label["edge_ids"]).encode("utf-8")).hexdigest()[:12]
    return {
        "route_id": f"residual-route-{digest}",
        "edge_ids": list(label["edge_ids"]),
        "node_path": list(label["node_path"]),
        "total_cost": _money_out(label["cost"], minor_unit),
        "_total_cost_minor": label["cost"],
        "total_travel_minutes": round(label["travel_minutes"], 6),
        "hops": label["hops"],
        "departure": _iso(label["departure"]),
        "arrival": _iso(label["arrival"]),
        "atomic_offer_ids": list(dict.fromkeys(label["atomic_offer_ids"])),
        "evidence_scope": "priced_current_observed_edges_only",
    }


def _route_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["_total_cost_minor"] <= right["_total_cost_minor"]
        and left["total_travel_minutes"] <= right["total_travel_minutes"]
        and left["hops"] <= right["hops"]
        and (
            left["_total_cost_minor"] < right["_total_cost_minor"]
            or left["total_travel_minutes"] < right["total_travel_minutes"]
            or left["hops"] < right["hops"]
        )
    )


def _heuristic_amount(value: Any, field: str, minor_unit: int) -> int | None:
    if value is None:
        return None
    return _to_minor(value, field, minor_unit)


def _research_requests(
    unresolved_edges: list[dict[str, Any]],
    labels_by_node: dict[str, list[dict[str, Any]]],
    nodes: dict[str, dict[str, Any]],
    origins: set[str],
    price_cutoff: int,
    settings: dict[str, Any],
    trace: _Trace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    minor_unit = settings["currency_decimal_places"]
    for edge in unresolved_edges:
        for suffix in labels_by_node.get(edge["to_node"], []):
            minimum_future_prefix_hops = 0 if edge["from_node"] in origins else 1
            if suffix["hops"] + 1 + minimum_future_prefix_hops > settings["max_hops"]:
                trace.record("research", "research_hop_limit", edge_id=edge["edge_id"], label_id=suffix["id"])
                continue
            if edge["from_node"] in suffix["node_path"]:
                trace.record("research", "research_cycle", edge_id=edge["edge_id"], label_id=suffix["id"])
                continue
            if edge["_arrival"] is not None and suffix["departure"] is not None:
                required_minutes, transition_kind = _transition_buffer(
                    edge["mode"],
                    suffix["modes"][0],
                    settings,
                    edge.get("independent_ticket_group"),
                    suffix.get("ticket_groups", (None,))[0],
                )
                if edge["_arrival"] + timedelta(minutes=required_minutes) > suffix["departure"]:
                    trace.record(
                        "research",
                        "research_time_incompatible",
                        edge_id=edge["edge_id"],
                        label_id=suffix["id"],
                        transition_kind=transition_kind,
                        required_connection_minutes=required_minutes,
                    )
                    continue
            remaining = price_cutoff - suffix["cost"]
            if remaining < 0:
                trace.record(
                    "research",
                    "nonnegative_cost_ceiling",
                    edge_id=edge["edge_id"],
                    known_suffix_cost=_money_out(suffix["cost"], minor_unit),
                    price_cutoff=_money_out(price_cutoff, minor_unit),
                )
                continue
            research = edge["_research"]
            estimate = _heuristic_amount(
                research.get("expected_cost", edge.get("expected_cost")),
                f"edge {edge['edge_id']}.research.expected_cost",
                minor_unit,
            )
            estimate_basis = "provided_research_heuristic"
            if estimate is None and edge["_cost"] is not None:
                estimate = edge["_cost"]
                estimate_basis = "stale_or_future_snapshot_for_priority_only"
            if estimate is None:
                estimate = 0.0
                estimate_basis = "optimistic_zero_for_priority_only"
            prefix_estimate = _heuristic_amount(
                research.get("expected_prefix_cost"),
                f"edge {edge['edge_id']}.research.expected_prefix_cost",
                minor_unit,
            )
            if prefix_estimate is None:
                prefix_estimate = 0.0
            query_cost = _decimal(
                research.get("query_cost", settings["default_query_cost"]),
                f"edge {edge['edge_id']}.research.query_cost",
            )
            if query_cost <= 0:
                raise ResidualBudgetSearchError(f"edge {edge['edge_id']}.research.query_cost must be positive")
            estimated_total = suffix["cost"] + estimate + prefix_estimate
            expected_improvement = max(0, price_cutoff - estimated_total)
            score = (Decimal(expected_improvement) / (Decimal(10) ** minor_unit)) / query_cost
            modes = research.get("candidate_modes")
            if modes is None:
                modes = [edge["mode"]] if edge["mode"] != "unknown" else settings["allowed_research_modes"]
            if not isinstance(modes, list) or not all(isinstance(mode, str) and mode.strip() for mode in modes):
                raise ResidualBudgetSearchError(f"edge {edge['edge_id']}.research.candidate_modes must be an array")
            modes = list(dict.fromkeys(mode.strip().lower() for mode in modes))
            mode_bucket = str(research.get("mode_bucket") or _mode_bucket(modes)).strip().lower()
            geography = str(research.get("geography_bucket") or _geography(nodes[edge["from_node"]])).strip().upper()
            required_buffer, transition_rules = _buffer_before_suffix(
                modes, suffix, settings
            )
            latest_arrival = (
                suffix["departure"] - timedelta(minutes=required_buffer)
                if suffix["departure"]
                else settings["_date_end"]
            )
            earliest_departure = edge["_departure"] or settings["_date_start"]
            request_key = f"{edge['edge_id']}|{suffix['id']}"
            request_id = "research-" + hashlib.sha1(request_key.encode("utf-8")).hexdigest()[:12]
            refresh = edge["_snapshot_status"] in {"expired", "future_snapshot"}
            allowed_scheduler_types = {
                "budgeted_predecessor_scan",
                "direct_closure",
                "unresolved_connector",
                "refresh_quote",
            }
            scheduler_type = research.get("scheduler_task_type")
            if scheduler_type is None:
                if refresh:
                    scheduler_type = "refresh_quote"
                elif edge["from_node"] in origins:
                    scheduler_type = "direct_closure"
                elif research.get("connection_kind") == "unresolved_connector":
                    scheduler_type = "unresolved_connector"
                else:
                    scheduler_type = "budgeted_predecessor_scan"
            scheduler_type = str(scheduler_type).strip().lower()
            if scheduler_type not in allowed_scheduler_types:
                raise ResidualBudgetSearchError(
                    f"edge {edge['edge_id']}.research.scheduler_task_type is unsupported"
                )
            endpoint_policy = research.get("endpoint_policy")
            if endpoint_policy is None:
                endpoint_policy = {
                    "from": "exact_node",
                    "to": "exact_node",
                    "allow_region_expansion": False,
                }
            if not isinstance(endpoint_policy, dict):
                raise ResidualBudgetSearchError(
                    f"edge {edge['edge_id']}.research.endpoint_policy must be an object"
                )
            candidates.append(
                {
                    "request_id": request_id,
                    "request_type": "refresh_observed_offer"
                    if refresh
                    else "price_and_schedule_discovery",
                    "scheduler_task_type": scheduler_type,
                    "endpoint_policy": endpoint_policy,
                    "hypothesis_edge_id": edge["edge_id"],
                    "from_node": edge["from_node"],
                    "to_node": edge["to_node"],
                    "earliest_departure": _iso(earliest_departure),
                    "latest_arrival": _iso(latest_arrival),
                    "mode_transition_buffer": {
                        "minutes": required_buffer,
                        "rules": transition_rules,
                        "suffix_mode": suffix["modes"][0] if suffix["modes"] else None,
                        "candidate_predecessor_modes": modes,
                    },
                    "known_suffix_label_id": suffix["id"],
                    "known_suffix_edge_ids": list(suffix["edge_ids"]),
                    "known_suffix_cost": _money_out(suffix["cost"], minor_unit),
                    "remaining_budget_before_unknown_edge": _money_out(remaining, minor_unit),
                    "estimated_edge_cost_for_priority_only": _money_out(estimate, minor_unit),
                    "estimated_prefix_cost_for_priority_only": _money_out(prefix_estimate, minor_unit),
                    "estimate_basis": estimate_basis,
                    "expected_improvement": _money_out(expected_improvement, minor_unit),
                    "query_cost": float(query_cost),
                    "priority_score": float(score),
                    "search_channel": "price_improvement"
                    if expected_improvement > 0
                    else "pareto_exploration",
                    "candidate_modes": modes,
                    "geography_bucket": geography,
                    "mode_bucket": mode_bucket,
                    "would_directly_reach_origin_pool": edge["from_node"] in origins,
                    "evidence_status": edge["_snapshot_status"],
                    "not_a_route": True,
                    "priority_rule": "expected_improvement_divided_by_query_cost",
                    "_priority_score_decimal": score,
                    "_expected_improvement_minor": expected_improvement,
                }
            )
    best_by_edge: dict[str, dict[str, Any]] = {}
    for candidate in sorted(
        candidates,
        key=lambda item: (-item["_priority_score_decimal"], -item["_expected_improvement_minor"], item["request_id"]),
    ):
        best_by_edge.setdefault(candidate["hypothesis_edge_id"], candidate)
    deduplicated = list(best_by_edge.values())

    selected: list[dict[str, Any]] = []
    geography_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    rejected_counts: Counter[str] = Counter()
    ordered = sorted(
        deduplicated,
        key=lambda item: (-item["_priority_score_decimal"], -item["_expected_improvement_minor"], item["request_id"]),
    )
    for candidate in ordered:
        geography = candidate["geography_bucket"]
        mode = candidate["mode_bucket"]
        reason = None
        if geography_counts[geography] >= settings["geography_quota"]:
            reason = "geography_quota"
        elif mode_counts[mode] >= settings["mode_quota"]:
            reason = "mode_quota"
        elif len(selected) >= settings["query_budget"]:
            reason = "query_budget_exhausted"
        if reason:
            rejected_counts[reason] += 1
            trace.record(
                "query_allocation",
                reason,
                request_id=candidate["request_id"],
                geography_bucket=geography,
                mode_bucket=mode,
            )
            continue
        item = dict(candidate)
        item.pop("_priority_score_decimal", None)
        item.pop("_expected_improvement_minor", None)
        item["query_sequence"] = len(selected) + 1
        selected.append(item)
        geography_counts[geography] += 1
        mode_counts[mode] += 1
        trace.record(
            "query_allocation",
            "research_request_selected",
            request_id=item["request_id"],
            priority_score=item["priority_score"],
            query_sequence=item["query_sequence"],
        )
    audit = {
        "limits": {
            "query_budget": settings["query_budget"],
            "geography_quota": settings["geography_quota"],
            "mode_quota": settings["mode_quota"],
        },
        "selected_by_geography": dict(sorted(geography_counts.items())),
        "selected_by_mode": dict(sorted(mode_counts.items())),
        "rejected_reason_counts": dict(sorted(rejected_counts.items())),
    }
    public_ordered = []
    for candidate in ordered:
        item = dict(candidate)
        item.pop("_priority_score_decimal", None)
        item.pop("_expected_improvement_minor", None)
        public_ordered.append(item)
    return selected, public_ordered, audit


def residual_budget_search(
    bundle: dict[str, Any], *, complete_route_validator: Any = None
) -> dict[str, Any]:
    """Run one deterministic research round over a bundle of nodes and edges."""
    if not isinstance(bundle, dict):
        raise ResidualBudgetSearchError("bundle must be an object")
    settings = _settings(bundle)
    nodes = _nodes(bundle.get("nodes"))
    origins = _pool(bundle.get("origin_nodes", bundle.get("origin_pool")), "origin_nodes", nodes)
    destinations = _pool(
        bundle.get("destination_nodes", bundle.get("destination_pool")),
        "destination_nodes",
        nodes,
    )
    minor_unit = settings["currency_decimal_places"]
    initial = _incumbent(bundle.get("incumbent"), minor_unit)
    edges = _edges(bundle.get("edges"), nodes, minor_unit)
    trace = _Trace(settings["max_trace_entries"])

    observed_times = [
        edge["_source"]["_observed_at"]
        for edge in edges
        if edge["_source"] is not None
    ]
    if settings.get("search_as_of"):
        as_of = _dt(settings["search_as_of"], "config.search_as_of")
    elif observed_times:
        as_of = max(observed_times)
    else:
        as_of = settings["_date_start"]

    snapshot_counts: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for edge in edges:
        if edge["_cost"] is None:
            status = "price_unknown"
        elif edge["_source"]["_observed_at"] > as_of:
            status = "future_snapshot"
        elif edge["_source"]["_expires_at"] is not None and edge["_source"]["_expires_at"] <= as_of:
            status = "expired"
        elif edge["_departure"] < settings["_date_start"] or edge["_arrival"] > settings["_date_end"]:
            status = "outside_date_window"
        else:
            status = "eligible"
        edge["_snapshot_status"] = status
        snapshot_counts[status] += 1
        if status == "eligible":
            eligible.append(edge)
        elif status != "outside_date_window":
            unresolved.append(edge)
        trace.record("evidence", status, edge_id=edge["edge_id"])

    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in eligible:
        incoming[edge["to_node"]].append(edge)
    for values in incoming.values():
        values.sort(key=lambda edge: (edge["_arrival"], edge["_departure"], edge["edge_id"]), reverse=True)

    prefix_bounds = _prefix_cost_lower_bounds(nodes, eligible, origins)
    initial_cost = initial["total_cost"]
    raw_delta = settings.get("strict_improvement_delta")
    strict_delta = (
        1
        if raw_delta is None
        else _to_minor(raw_delta, "config.strict_improvement_delta", minor_unit)
    )
    if strict_delta <= 0:
        raise ResidualBudgetSearchError("config.strict_improvement_delta must be positive")
    if strict_delta >= initial_cost:
        raise ResidualBudgetSearchError(
            "config.strict_improvement_delta must be less than the incumbent"
        )
    current_incumbent = initial_cost
    current_price_cutoff = current_incumbent - strict_delta
    initial_price_cutoff = current_price_cutoff
    raw_pareto_ceiling = bundle.get("config", {}).get("pareto_cost_ceiling")
    pareto_ceiling = (
        _to_minor(raw_pareto_ceiling, "config.pareto_cost_ceiling", minor_unit)
        if raw_pareto_ceiling is not None
        else initial_cost
    )
    if pareto_ceiling < initial_cost:
        raise ResidualBudgetSearchError(
            "config.pareto_cost_ceiling must be at least the initial incumbent"
        )
    incumbent_edge_ids = list(initial["edge_ids"])
    incumbent_route_id = None
    incumbent_history: list[dict[str, Any]] = []
    labels_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    heap: list[tuple[int, int, int, str, dict[str, Any]]] = []
    serial = 0
    for destination in sorted(destinations):
        label = _seed_label(destination, current_price_cutoff)
        labels_by_node[destination].append(label)
        bound = prefix_bounds.get(destination)
        priority = label["cost"] + (bound if bound is not None else 0)
        heapq.heappush(heap, (priority, label["cost"], label["hops"], label["id"], label))
        serial += 1

    complete_routes: list[dict[str, Any]] = []
    while heap:
        _, _, _, _, suffix = heapq.heappop(heap)
        if suffix["cost"] > current_price_cutoff and suffix["hops"]:
            trace.record(
                "channel",
                "price_channel_inactive_after_incumbent_update",
                label_id=suffix["id"],
                known_suffix_cost=_money_out(suffix["cost"], minor_unit),
                active_incumbent=_money_out(current_incumbent, minor_unit),
                price_cutoff=_money_out(current_price_cutoff, minor_unit),
                pareto_channel_retained=suffix["cost"] <= pareto_ceiling,
            )
        if suffix["hops"] >= settings["max_hops"]:
            trace.record("bound", "max_hops", label_id=suffix["id"], hops=suffix["hops"])
            continue
        for edge in incoming.get(suffix["node"], []):
            if edge["from_node"] in suffix["node_path"]:
                trace.record("expand", "cycle", label_id=suffix["id"], edge_id=edge["edge_id"])
                continue
            if suffix["departure"] is not None:
                required_minutes, transition_kind = _transition_buffer(
                    edge["mode"],
                    suffix["modes"][0],
                    settings,
                    edge.get("independent_ticket_group"),
                    suffix.get("ticket_groups", (None,))[0],
                )
                if edge["_arrival"] + timedelta(minutes=required_minutes) > suffix["departure"]:
                    reason = (
                        "minimum_connection"
                        if settings["mode_transition_buffers_minutes"][transition_kind]
                        <= settings["minimum_connection_minutes"]
                        else "mode_transition_buffer"
                    )
                    trace.record(
                        "expand",
                        reason,
                        label_id=suffix["id"],
                        edge_id=edge["edge_id"],
                        transition_kind=transition_kind,
                        required_connection_minutes=required_minutes,
                    )
                    continue
            candidate = _extend(suffix, edge, current_price_cutoff)
            trace.record(
                "residual_budget",
                "confirmed_suffix_deduction",
                label_id=candidate["id"],
                edge_id=edge["edge_id"],
                budget_before=_money_out(current_price_cutoff - suffix["cost"], minor_unit),
                confirmed_edge_cost=_money_out(edge["_cost"], minor_unit),
                remaining_budget=_money_out(candidate["remaining_budget_at_creation"], minor_unit),
            )
            if candidate["cost"] > pareto_ceiling:
                trace.record(
                    "bound",
                    "pareto_cost_ceiling",
                    label_id=candidate["id"],
                    known_suffix_cost=_money_out(candidate["cost"], minor_unit),
                    pareto_cost_ceiling=_money_out(pareto_ceiling, minor_unit),
                    proof="all admissible edge costs are nonnegative",
                )
                continue
            if candidate["cost"] > current_price_cutoff:
                trace.record(
                    "channel",
                    "price_channel_cost_ceiling",
                    label_id=candidate["id"],
                    known_suffix_cost=_money_out(candidate["cost"], minor_unit),
                    active_incumbent=_money_out(current_incumbent, minor_unit),
                    price_cutoff=_money_out(current_price_cutoff, minor_unit),
                    pareto_channel_retained=True,
                )
            if candidate["travel_minutes"] > settings["max_total_minutes"]:
                trace.record(
                    "bound",
                    "max_total_minutes",
                    label_id=candidate["id"],
                    travel_minutes=round(candidate["travel_minutes"], 6),
                    maximum=settings["max_total_minutes"],
                )
                continue

            existing = labels_by_node[candidate["node"]]
            dominator = next((label for label in existing if _dominates(label, candidate)), None)
            if dominator:
                trace.record(
                    "pareto",
                    "pareto_dominated",
                    label_id=candidate["id"],
                    dominated_by=dominator["id"],
                )
                continue
            for dominated in [label for label in existing if _dominates(candidate, label)]:
                existing.remove(dominated)
                trace.record(
                    "pareto",
                    "pareto_replaced",
                    label_id=dominated["id"],
                    dominated_by=candidate["id"],
                )
            if len(existing) >= settings["max_labels_per_node"]:
                trace.record(
                    "pareto",
                    "label_limit",
                    label_id=candidate["id"],
                    node=candidate["node"],
                    limit=settings["max_labels_per_node"],
                )
                continue
            existing.append(candidate)

            if candidate["node"] in origins:
                route = _route(candidate, minor_unit)
                audit = complete_route_validator(route["edge_ids"]) if complete_route_validator else {"accepted": True}
                route["complete_route_audit"] = audit
                if not audit.get("accepted", False):
                    trace.record(
                        "feasibility",
                        "complete_route_audit_rejected",
                        route_id=route["route_id"],
                        reason_codes=audit.get("reason_codes", []),
                    )
                    continue
                complete_routes.append(route)
                if candidate["cost"] <= current_price_cutoff:
                    previous = current_incumbent
                    current_incumbent = candidate["cost"]
                    current_price_cutoff = current_incumbent - strict_delta
                    incumbent_edge_ids = list(candidate["edge_ids"])
                    incumbent_route_id = route["route_id"]
                    update = {
                        "sequence": len(incumbent_history) + 1,
                        "previous_total_cost": _money_out(previous, minor_unit),
                        "new_total_cost": _money_out(current_incumbent, minor_unit),
                        "improvement": _money_out(previous - current_incumbent, minor_unit),
                        "strict_improvement_delta": _money_out(strict_delta, minor_unit),
                        "new_price_improvement_cutoff": _money_out(
                            current_price_cutoff, minor_unit
                        ),
                        "route_id": route["route_id"],
                        "edge_ids": route["edge_ids"],
                    }
                    incumbent_history.append(update)
                    trace.record("incumbent", "lower_complete_route", **update)
                else:
                    trace.record(
                        "pareto",
                        "complete_route_not_price_improving",
                        route_id=route["route_id"],
                        total_cost=route["total_cost"],
                    )
                continue

            lower_bound = prefix_bounds.get(candidate["node"])
            if lower_bound is not None:
                trace.record(
                    "ordering",
                    "observed_prefix_price_floor",
                    label_id=candidate["id"],
                    known_suffix_cost=_money_out(candidate["cost"], minor_unit),
                    observed_prefix_price_floor=_money_out(lower_bound, minor_unit),
                    use="queue_ordering_only_not_pruning",
                )
            priority = candidate["cost"] + (lower_bound if lower_bound is not None else 0)
            heapq.heappush(
                heap,
                (priority, candidate["cost"], candidate["hops"], candidate["id"], candidate),
            )

    requests, all_research, diversity_audit = _research_requests(
        unresolved,
        labels_by_node,
        nodes,
        origins,
        current_price_cutoff,
        settings,
        trace,
    )

    serialized_labels = [
        _serial_label(label, nodes, current_price_cutoff, minor_unit, pareto_ceiling)
        for node in sorted(labels_by_node)
        for label in sorted(labels_by_node[node], key=lambda item: (item["cost"], item["id"]))
    ]
    serialized_by_id = {label["label_id"]: label for label in serialized_labels}
    best_remaining: dict[str, dict[str, Any]] = {}
    best_cost_minor: dict[str, int] = {}
    for labels in labels_by_node.values():
        for internal in labels:
            node = internal["node"]
            if node not in best_cost_minor or internal["cost"] < best_cost_minor[node]:
                best_cost_minor[node] = internal["cost"]
                label = serialized_by_id[internal["id"]]
                best_remaining[node] = {
                    "label_id": label["label_id"],
                    "known_suffix_cost": label["known_suffix_cost"],
                    "remaining_budget": label["remaining_budget"],
                }

    routes_by_id = {route["route_id"]: route for route in complete_routes}
    routes_internal = sorted(
        routes_by_id.values(),
        key=lambda route: (route["_total_cost_minor"], route["total_travel_minutes"], route["route_id"]),
    )
    pareto = [
        route
        for route in routes_internal
        if not any(
            other["route_id"] != route["route_id"] and _route_dominates(other, route)
            for other in routes_internal
        )
    ]
    pareto_ids = [route["route_id"] for route in pareto]
    routes: list[dict[str, Any]] = []
    for route in routes_internal:
        public = dict(route)
        public.pop("_total_cost_minor", None)
        public["price_improving_relative_to_initial_incumbent"] = (
            route["_total_cost_minor"] <= initial_price_cutoff
        )
        public["equals_final_incumbent"] = route["_total_cost_minor"] == current_incumbent
        public["pareto_nondominated"] = route["route_id"] in pareto_ids
        routes.append(public)
    return {
        "engine_version": ENGINE_VERSION,
        "search_model": SEARCH_MODEL,
        "search_intensity": settings["search_intensity"],
        "currency": str(bundle.get("currency", "UNSPECIFIED")),
        "currency_decimal_places": minor_unit,
        "origin_nodes": sorted(origins),
        "destination_nodes": sorted(destinations),
        "date_window": settings["date_window"],
        "incumbent": {
            "initial_total_cost": _money_out(initial_cost, minor_unit),
            "strict_improvement_delta": _money_out(strict_delta, minor_unit),
            "initial_price_improvement_cutoff": _money_out(initial_price_cutoff, minor_unit),
            "initial_edge_ids": list(initial["edge_ids"]),
            "final_total_cost": _money_out(current_incumbent, minor_unit),
            "final_price_improvement_cutoff": _money_out(current_price_cutoff, minor_unit),
            "final_edge_ids": incumbent_edge_ids,
            "final_route_id": incumbent_route_id,
            "improved": current_incumbent < initial_cost,
            "total_improvement": _money_out(initial_cost - current_incumbent, minor_unit),
            "history": incumbent_history,
        },
        "reverse_labels": serialized_labels,
        "remaining_budget": {
            "definition": "final strict-improvement cutoff minus the cost of the confirmed suffix",
            "strict_improvement_delta": _money_out(strict_delta, minor_unit),
            "price_improvement_cutoff": _money_out(current_price_cutoff, minor_unit),
            "by_node": dict(sorted(best_remaining.items())),
        },
        "routes": routes,
        "pareto_route_ids": pareto_ids,
        "search_channels": {
            "price_improvement": {
                "active_incumbent": _money_out(current_incumbent, minor_unit),
                "strict_improvement_delta": _money_out(strict_delta, minor_unit),
                "cutoff": _money_out(current_price_cutoff, minor_unit),
                "purpose": "find a complete route at or below incumbent minus strict improvement delta",
            },
            "pareto_exploration": {
                "cost_ceiling": _money_out(pareto_ceiling, minor_unit),
                "purpose": "retain cost/time/hop trade-offs independently of price-incumbent updates",
            },
        },
        "research_requests": requests,
        "research_candidates": all_research,
        "query_budget": {
            "limit": settings["query_budget"],
            "used": len(requests),
            "remaining": settings["query_budget"] - len(requests),
            "candidate_count": len(all_research),
        },
        "diversity_quotas": diversity_audit,
        "snapshot_audit": {
            "as_of": _iso(as_of),
            "status_counts": dict(sorted(snapshot_counts.items())),
            "eligible_edge_ids": sorted(edge["edge_id"] for edge in eligible),
            "unresolved_edge_ids": sorted(edge["edge_id"] for edge in unresolved),
        },
        "safe_pruning": {
            "rules": [
                {
                    "rule": "nonnegative_channel_cost_ceiling",
                    "scope": "price or Pareto channel according to its explicit ceiling",
                    "proof": "all admitted monetary edge costs use nonnegative integer minor units; unknown edges are not assigned a route cost",
                },
                {
                    "rule": "time_and_hop_feasibility",
                    "scope": "exact supplied timestamps and configured limits",
                },
                {
                    "rule": "unknown_edge_is_not_fact",
                    "scope": "route construction",
                    "proof": "unknown, expired, and future edges may create requests but never labels or routes",
                },
            ],
            "reason_counts": dict(sorted(trace.reason_counts.items())),
        },
        "trace": trace.items,
        "trace_truncated": trace.total > len(trace.items),
        "searched_scope": {
            "observed_nodes": len(nodes),
            "input_edges": len(edges),
            "eligible_priced_edges": len(eligible),
            "unresolved_or_refresh_edges": len(unresolved),
            "reverse_label_count": len(serialized_labels),
            "complete_pareto_channel_routes": len(routes),
            "claim": "best price improvements found within eligible observed edges and configured limits; research requests are hypotheses",
        },
    }


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResidualBudgetSearchError(f"cannot read JSON bundle {path}: {exc}") from exc


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run residual-budget backward search from one JSON bundle")
    parser.add_argument("--bundle", required=True, help="JSON containing nodes, edges, pools, incumbent, and config")
    parser.add_argument("--output", help="write structured JSON here; stdout when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = residual_budget_search(read_json(args.bundle))
        if args.output:
            write_json(args.output, result)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "engine_version": ENGINE_VERSION,
                        "output": _display_path(args.output),
                        "final_incumbent": result["incumbent"]["final_total_cost"],
                        "research_requests": len(result["research_requests"]),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ResidualBudgetSearchError as exc:
        print(f"residual_budget_search: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "ENGINE_VERSION",
    "SEARCH_MODEL",
    "BUDGET_PRESETS",
    "ResidualBudgetSearchError",
    "residual_budget_search",
]


if __name__ == "__main__":
    raise SystemExit(main())
