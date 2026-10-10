"""Compile compatible WorkItems into one Provider execution batch."""

from __future__ import annotations

import copy
from typing import Any


def compile_batches(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    positions: dict[str, int] = {}
    for item in items:
        execution = (
            item.get("payload", {}).get("work", {}).get("execution", {})
            if isinstance(item, dict)
            else {}
        )
        key = execution.get("batch_key") if execution.get("batchable") is True else None
        if not isinstance(key, str) or not key:
            result.append([copy.deepcopy(item)])
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append([copy.deepcopy(item)])
        else:
            result[position].append(copy.deepcopy(item))
    return result
