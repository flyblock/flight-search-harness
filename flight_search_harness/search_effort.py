"""Pure Search Effort presets shared at the public request seam.

Product Kernels own their Research Credit ledgers, frontier state, and stopping
decisions. This Module only normalizes the user-facing preset into immutable
policy values so independent Product Adapters interpret the same request
spelling.
"""

from __future__ import annotations

import copy
from typing import Any

from .contracts import HarnessProtocolError


SEARCH_EFFORT_POLICY_VERSION = "1.0"
DEFAULT_SEARCH_EFFORT = "standard"
SEARCH_EFFORT_PRESETS = {
    "economy": {
        "research_credit_budget": 10,
        "price_challenge_threshold": 0.10,
    },
    "standard": {
        "research_credit_budget": 30,
        "price_challenge_threshold": 0.05,
    },
    "deep": {
        "research_credit_budget": 100,
        "price_challenge_threshold": 0.01,
    },
}


def normalize_search_effort(value: Any) -> str:
    """Return one canonical Search Effort name, defaulting to ``standard``."""

    effort = DEFAULT_SEARCH_EFFORT if value is None else value
    if not isinstance(effort, str) or effort not in SEARCH_EFFORT_PRESETS:
        raise HarnessProtocolError(
            "search_effort must be economy, standard, or deep"
        )
    return effort


def search_effort_policy(value: Any) -> dict[str, Any]:
    """Return a detached policy value for one normalized Search Effort."""

    effort = normalize_search_effort(value)
    return {
        "search_effort_policy_version": SEARCH_EFFORT_POLICY_VERSION,
        "search_effort": effort,
        **copy.deepcopy(SEARCH_EFFORT_PRESETS[effort]),
    }
