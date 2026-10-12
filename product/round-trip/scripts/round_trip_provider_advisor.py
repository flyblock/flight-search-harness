"""Return a few non-binding Provider hints for one round-trip Work Item."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


VERSION = "0.5.0"
MAX_SOURCE_HINTS = 3
FAMILY_SOURCE_HINTS = {
    "region_pool_fare_screen": ["kiwi-com", "google-flights", "skyscanner"],
    "joint_quote": ["google-flights", "skyscanner", "kiwi-com"],
    "split_outbound": ["kiwi-com", "trip-com", "google-flights"],
    "split_return": ["kiwi-com", "trip-com", "google-flights"],
    "nested_atomic": ["kiwi-com", "trip-com", "kayak"],
    "surface_access": ["deutsche-bahn", "trainline", "flixbus"],
    "refresh": ["google-flights", "kiwi-com", "trip-com"],
}


class ProviderAdvisorError(ValueError):
    pass


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_registry(registry_path: str | Path | None = None) -> dict[str, Any]:
    target = (
        Path(registry_path)
        if registry_path
        else Path(__file__).parents[1] / "data" / "source-registry.json"
    )
    registry = read_json(target)
    if registry.get("registry_version") != VERSION:
        raise ProviderAdvisorError(f"Provider registry must use version {VERSION}")
    if registry.get("not_a_whitelist") is not True:
        raise ProviderAdvisorError("Provider registry must remain non-exhaustive")
    sources = registry.get("sources")
    if not isinstance(sources, list):
        raise ProviderAdvisorError("Provider registry sources must be an array")
    identifiers = [item.get("source_id") for item in sources if isinstance(item, dict)]
    if len(identifiers) != len(set(identifiers)):
        raise ProviderAdvisorError("Provider registry contains duplicate source IDs")
    return registry


def task_family(task: dict[str, Any]) -> str:
    if task.get("research_kind") == "refresh":
        return "refresh"
    if task.get("research_kind") == "region_pool_fare_screen":
        return "region_pool_fare_screen"
    scope = task.get("journey_scope")
    if scope == "joint_round_trip":
        return "joint_quote"
    if scope == "one_way":
        return (
            "split_outbound"
            if task.get("direction") == "outbound"
            else "split_return"
        )
    if scope == "atomic_itinerary":
        return "nested_atomic"
    if scope == "surface":
        return "surface_access"
    raise ProviderAdvisorError(
        f"cannot derive task family from task {task.get('task_id')}"
    )


def _hint_ids(task: dict[str, Any], family: str) -> list[str]:
    query = task.get("query_shape", {})
    if not isinstance(query, dict):
        query = {}
    explicit = []
    for field in ("configured_source_ids", "operator_hints", "authority_hints"):
        values = query.get(field, [])
        if isinstance(values, list):
            explicit.extend(
                str(value)
                for value in values
                if isinstance(value, str) and value
            )
    return list(dict.fromkeys([*explicit, *FAMILY_SOURCE_HINTS[family]]))


def recommend_sources(
    state: dict[str, Any],
    task: dict[str, Any],
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    del state  # Provider hints depend on the current Work Item, not Session-wide scores.
    registry = load_registry(registry_path)
    family = task_family(task)
    sources = {
        str(item["source_id"]): item
        for item in registry["sources"]
        if isinstance(item, dict) and item.get("source_id")
    }
    suggestions = []
    for source_id in _hint_ids(task, family):
        source = sources.get(source_id)
        if source is None:
            continue
        suggestions.append(
            {
                key: copy.deepcopy(source[key])
                for key in (
                    "source_id",
                    "name",
                    "url",
                    "source_type",
                    "evidence_role",
                )
                if source.get(key) is not None
            }
        )
        suggestions[-1]["binding"] = False
        if len(suggestions) >= MAX_SOURCE_HINTS:
            break

    query = task.get("query_shape", {})
    operator_hints = (
        set(query.get("operator_hints", [])) if isinstance(query, dict) else set()
    )
    authority_hints = (
        set(query.get("authority_hints", [])) if isinstance(query, dict) else set()
    )
    dynamic = []
    for rule in registry.get("dynamic_source_rules", []):
        source_id = rule.get("source_id") if isinstance(rule, dict) else None
        applicable = (
            source_id == "carrier_official_site" and operator_hints and family != "surface_access"
        ) or (
            source_id == "operator_official_site" and operator_hints and family == "surface_access"
        ) or (
            source_id not in {"carrier_official_site", "operator_official_site"}
            and authority_hints
        )
        if applicable:
            dynamic.append(
                {
                    **copy.deepcopy(rule),
                    "activation_state": "applicable",
                    "binding": False,
                }
            )

    return {
        "task_family": family,
        "suggestions": suggestions,
        "dynamic_source_rules": dynamic[:MAX_SOURCE_HINTS],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--registry")
    args = parser.parse_args(argv)
    try:
        state = read_json(args.state)
        task = next(
            (
                item
                for item in state.get("research_tasks", [])
                if item.get("task_id") == args.task_id
            ),
            None,
        )
        if task is None:
            raise ProviderAdvisorError(f"unknown task_id {args.task_id}")
        print(
            json.dumps(
                recommend_sources(state, task, args.registry),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, json.JSONDecodeError, ProviderAdvisorError) as exc:
        print(f"round_trip_provider_advisor: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
