"""Canonical identities shared across Flight Search Harness products."""

from __future__ import annotations

import copy
import re
from typing import Any


_IATA_CODE = re.compile(r"^[A-Za-z]{3}$")


class CanonicalValueError(ValueError):
    pass


def canonical_node_id(node_type: Any, node_id: Any) -> str:
    """Return the stable node identity without treating labels as identity."""

    if not isinstance(node_type, str) or not node_type.strip():
        raise CanonicalValueError("node_type must be a non-empty string")
    if not isinstance(node_id, str) or not node_id.strip():
        raise CanonicalValueError("node_id must be a non-empty string")
    value = node_id.strip()
    if node_type.strip() == "airport" and _IATA_CODE.fullmatch(value):
        return value.upper()
    return value


def normalize_node_identity(node: Any) -> dict[str, Any]:
    """Copy a node while normalizing only its stable identity fields."""

    if not isinstance(node, dict):
        raise CanonicalValueError("node must be an object")
    result = copy.deepcopy(node)
    node_type = result.get("node_type")
    if not isinstance(node_type, str) or not node_type.strip():
        raise CanonicalValueError("node_type must be a non-empty string")
    result["node_type"] = node_type.strip()
    result["node_id"] = canonical_node_id(
        result["node_type"], result.get("node_id")
    )
    return result


def reconcile_node(
    canonical: dict[str, Any], observed: dict[str, Any]
) -> dict[str, Any]:
    """Reuse a canonical node when an observation describes the same identity.

    Display labels, country names, timezone text, and UTC offsets are metadata.
    They never create a second node and never require Provider research replay.
    """

    existing = normalize_node_identity(canonical)
    incoming = normalize_node_identity(observed)
    if existing["node_id"] != incoming["node_id"]:
        raise CanonicalValueError("node_id does not match canonical node")
    if existing["node_type"] != incoming["node_type"]:
        raise CanonicalValueError(
            f"node {existing['node_id']} has conflicting node_type"
        )
    return existing
