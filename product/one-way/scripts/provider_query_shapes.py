"""Compile Provider-compatible endpoint scope ladders without selecting for the AI."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


VERSION = "1.0"
DEFAULT_CAPABILITIES = Path(__file__).resolve().parents[1] / "data" / "provider-query-shapes.json"


class ProviderQueryShapeError(ValueError):
    pass


def load_capabilities(path: str | Path = DEFAULT_CAPABILITIES) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderQueryShapeError(f"cannot load Provider query shapes: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("query_shape_version") != VERSION:
        raise ProviderQueryShapeError(f"query_shape_version must be {VERSION}")
    shapes = raw.get("shapes")
    sources = raw.get("sources")
    if not isinstance(shapes, list) or not shapes or not isinstance(sources, list):
        raise ProviderQueryShapeError("query-shape catalog is malformed")
    known = set(shapes)
    for source in sources:
        if not isinstance(source, dict) or not source.get("source_id"):
            raise ProviderQueryShapeError("query-shape source is malformed")
        support = source.get("support")
        if not isinstance(support, dict) or not set(support) <= known:
            raise ProviderQueryShapeError(f"query-shape support is invalid for {source.get('source_id')}")
    return raw


def capabilities_by_source(path: str | Path = DEFAULT_CAPABILITIES) -> dict[str, dict[str, Any]]:
    raw = load_capabilities(path)
    default = raw.get("default_support", "unknown")
    shapes = list(raw["shapes"])
    result = {}
    for source in raw["sources"]:
        support = {shape: source.get("support", {}).get(shape, default) for shape in shapes}
        result[str(source["source_id"])] = {**copy.deepcopy(source), "support": support}
    return result


def _node_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node.get("node_id")): node
        for node in state.get("nodes", [])
        if isinstance(node, dict) and node.get("node_id")
    }


def _country_codes(node_ids: list[str], state: dict[str, Any]) -> list[str]:
    nodes = _node_map(state)
    return sorted(
        {
            str(nodes[node_id].get("country_code"))
            for node_id in node_ids
            if node_id in nodes and nodes[node_id].get("country_code")
        }
    )


def _source_support(
    shape_id: str, source_ids: list[str], capabilities: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    supported = []
    unknown = []
    unsupported = []
    for source_id in source_ids:
        value = capabilities.get(source_id, {}).get("support", {}).get(shape_id, "unknown")
        if value is True:
            supported.append(source_id)
        elif value is False:
            unsupported.append(source_id)
        else:
            unknown.append(source_id)
    return {"supported": supported, "unknown": unknown, "unsupported": unsupported}


def compile_scope_ladder(
    task: dict[str, Any], state: dict[str, Any], *, catalog_path: str | Path = DEFAULT_CAPABILITIES
) -> dict[str, Any]:
    """Return executable endpoint representations ordered by user-scope fidelity."""

    if not isinstance(task, dict) or not isinstance(state, dict):
        raise ProviderQueryShapeError("task and state must be objects")
    capabilities = capabilities_by_source(catalog_path)
    source_ids = [
        str(item.get("source_id"))
        for item in task.get("source_suggestions", [])
        if isinstance(item, dict) and item.get("source_id")
    ]
    origins = [str(item) for item in task.get("origins", [])]
    destinations = [str(item) for item in task.get("destinations", [])]
    profile = state.get("profile", {}) if isinstance(state.get("profile"), dict) else {}
    origin_pool = [str(item) for item in profile.get("origin_airport_nodes", [])]
    destination_pool = [str(item) for item in profile.get("destination_airport_nodes", [])]
    origin_countries = _country_codes(origin_pool, state)
    destination_countries = _country_codes(destination_pool, state)
    origin_region = profile.get("origin_region", {}) if isinstance(profile.get("origin_region"), dict) else {}
    destination_region = profile.get("destination_region", {}) if isinstance(profile.get("destination_region"), dict) else {}

    shapes: list[dict[str, Any]] = []

    def add(
        shape_id: str,
        from_scope: str,
        from_values: list[str],
        to_scope: str,
        to_values: list[str],
        reason: str,
        *,
        expands_known_pool: bool,
    ) -> None:
        if not from_values or not to_values:
            return
        support = _source_support(shape_id, source_ids, capabilities)
        shapes.append(
            {
                "shape_id": shape_id,
                "rank": len(shapes) + 1,
                "from": {"scope_type": from_scope, "values": list(dict.fromkeys(from_values))},
                "to": {"scope_type": to_scope, "values": list(dict.fromkeys(to_values))},
                "source_support": support,
                "reason": reason,
                "expands_known_pool": expands_known_pool,
                "binding": False,
            }
        )

    open_from = "ANYWHERE" in origins
    open_to = "ANYWHERE" in destinations
    exact_destinations = [item for item in destinations if item != "ANYWHERE"]
    exact_origins = [item for item in origins if item != "ANYWHERE"]
    if open_from and exact_destinations:
        add("airport_pool_to_exact", "airport_pool", origin_pool, "exact_nodes", exact_destinations, "Preserves the user's declared origin airport pool.", expands_known_pool=False)
        add("country_to_exact", "country", origin_countries, "exact_nodes", exact_destinations, "Uses a Provider-supported country origin; returned airports still require pool/access validation.", expands_known_pool=True)
        region_values = [str(value) for value in (origin_region.get("region_id"), origin_region.get("label")) if value]
        add("region_to_exact", "region", region_values, "exact_nodes", exact_destinations, "Uses the user-declared origin region when the Provider supports it.", expands_known_pool=True)
        add("anywhere_to_exact", "anywhere", ["ANYWHERE"], "exact_nodes", exact_destinations, "Use only when the selected Provider actually supports a global broad origin.", expands_known_pool=True)
    elif exact_origins and open_to:
        add("airport_pool_to_anywhere" if len(exact_origins) > 1 else "exact_to_anywhere", "node_pool" if len(exact_origins) > 1 else "exact_node", exact_origins, "anywhere", ["ANYWHERE"], "Broad destination discovery from the issued origin scope.", expands_known_pool=True)
    elif exact_origins and exact_destinations:
        shape_id = "airport_pool_to_airport_pool" if len(exact_origins) > 1 and len(exact_destinations) > 1 else "airport_pool_to_exact" if len(exact_origins) > 1 else "exact_to_exact"
        add(shape_id, "node_pool" if len(exact_origins) > 1 else "exact_node", exact_origins, "node_pool" if len(exact_destinations) > 1 else "exact_node", exact_destinations, "Preserves the issued concrete endpoint pools.", expands_known_pool=False)
    elif open_to and destination_pool:
        add("exact_to_anywhere", "exact_node", exact_origins, "destination_region", destination_pool, "Fallback representation for destination discovery.", expands_known_pool=True)

    return {
        "query_shape_version": VERSION,
        "task_id": task.get("task_id"),
        "decision_owner": "ai",
        "scope_ladder": shapes,
        "instruction": "Choose one Provider-compatible scope. Unknown support may be tested and recorded; false support must not be executed. Scope rank is fidelity advice, not a route decision.",
    }
