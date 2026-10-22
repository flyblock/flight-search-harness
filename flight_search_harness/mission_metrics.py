"""Normalize checkpoint metrics from immutable Receipt execution origins."""

from __future__ import annotations

import copy
from typing import Any

from .contracts import HarnessProtocolError


EXECUTION_ORIGINS = {
    "provider_query",
    "evidence_replay",
    "non_provider",
}


def normalize_checkpoint_metrics(
    reported: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return replay-aware metrics for one committed checkpoint.

    Provider query totals remain Worker observations. When a checkpoint also
    contains replayed Evidence, the top-level Provider count is rebuilt from
    per-item metrics so copied historical totals cannot become new queries.
    """

    metrics = copy.deepcopy(reported or {})
    if not isinstance(metrics, dict):
        raise HarnessProtocolError("Mission metrics must be an object")
    raw_items = metrics.get("items")
    items = copy.deepcopy(raw_items) if isinstance(raw_items, dict) else {}
    replay_count = 0
    retry_count = 0
    unknown_origin_count = 0
    provider_receipts: list[str] = []
    for receipt in receipts:
        identifier = str(receipt.get("item_id") or "")
        origin = receipt.get("execution_origin")
        if origin is not None and origin not in EXECUTION_ORIGINS:
            raise HarnessProtocolError(
                f"Receipt {identifier} has unsupported execution_origin"
            )
        item_metrics = items.setdefault(identifier, {})
        if not isinstance(item_metrics, dict):
            item_metrics = {}
            items[identifier] = item_metrics
        item_metrics["execution_origin"] = origin or "unknown"
        if origin == "provider_query":
            provider_receipts.append(identifier)
            item_metrics["provider_query_receipt_count"] = 1
        elif origin == "evidence_replay":
            replay_count += 1
            item_metrics["evidence_replay_count"] = 1
        elif origin is None:
            # A valid legacy full Receipt may omit this optional field.  Keep
            # it unknown for observability and, critically, do not infer a
            # Provider query from Work Item kind.
            unknown_origin_count += 1
        if int(receipt.get("attempt_number", 1)) > 1:
            retry_count += 1
            item_metrics["receipt_retry_count"] = 1

    if replay_count:
        provider_query_count = 0
        for identifier in provider_receipts:
            item_metrics = items.get(identifier, {})
            value = item_metrics.get("provider_query_count", 1)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HarnessProtocolError(
                    f"metrics.items.{identifier}.provider_query_count must be numeric"
                )
            provider_query_count += value
        metrics["provider_query_count"] = provider_query_count
    elif "provider_query_count" not in metrics:
        metrics["provider_query_count"] = len(provider_receipts)
    metrics["evidence_replay_count"] = replay_count
    metrics["unknown_execution_origin_count"] = unknown_origin_count
    metrics["receipt_retry_count"] = retry_count
    metrics["items"] = items
    return metrics
