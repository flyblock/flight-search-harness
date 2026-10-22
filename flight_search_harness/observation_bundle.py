"""Normalize one compact raw Observation bundle into product-owned input."""

from __future__ import annotations

import copy
from typing import Any

from .contracts import HarnessProtocolError


def normalize_attempt(product_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
    if attempt.get("kind") != "observation_bundle":
        return copy.deepcopy(attempt)
    source = attempt.get("source")
    observations = attempt.get("observations")
    if not isinstance(source, dict) or not isinstance(observations, list) or not observations:
        raise HarnessProtocolError(
            "observation_bundle requires source and observations"
        )
    bundle_id = str(attempt.get("bundle_id") or "").strip()
    if not bundle_id:
        raise HarnessProtocolError("observation_bundle requires bundle_id")
    if product_id == "one_way":
        normalized_source = {
            key: copy.deepcopy(source[key])
            for key in ("provider", "url", "observed_at", "expires_at")
            if key in source
        }
        return {
            "kind": "observations",
            "bundle_id": bundle_id,
            "source": normalized_source,
            "discovered_nodes": copy.deepcopy(attempt.get("discovered_nodes", [])),
            "observations": copy.deepcopy(observations),
        }
    if product_id == "round_trip":
        required_identity = {
            "source_id": source.get("source_id") or _source_id(source),
            "provider": source.get("provider"),
            "url": source.get("url"),
            "observed_at": source.get("observed_at"),
        }
        expanded = []
        for raw in observations:
            if not isinstance(raw, dict):
                raise HarnessProtocolError(
                    "observation_bundle observations must be objects"
                )
            observation = copy.deepcopy(raw)
            for key, value in required_identity.items():
                observation.setdefault(key, value)
            offer = observation.get("offer")
            if isinstance(offer, dict):
                for key, value in required_identity.items():
                    offer.setdefault(key, value)
            expanded.append(observation)
        return {
            "kind": "observations",
            "product_payload": {
                "observation_version": "0.4.0",
                "bundle_id": bundle_id,
                "coverage_complete": bool(attempt.get("coverage_complete", False)),
                "observations": expanded,
            },
        }
    if product_id == "random_loop":
        product_payload = attempt.get("product_payload")
        if not isinstance(product_payload, dict):
            raise HarnessProtocolError(
                "random-loop observation_bundle requires product_payload"
            )
        return {
            "kind": "observations",
            "product_payload": copy.deepcopy(product_payload),
        }
    raise HarnessProtocolError(f"unsupported Observation bundle product: {product_id}")


def _source_id(source: dict[str, Any]) -> str:
    provider = str(source.get("provider") or "ai-choice").strip().lower()
    return provider.replace(" ", "-") or "ai-choice"
