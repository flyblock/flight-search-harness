"""Render a compact round-trip decision catalog or one browser evidence brief."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any


BRIEF_VERSION = "0.5.0"
REGISTRY_VERSION = "0.5.0"


def _load_session_module():
    path = Path(__file__).with_name("round_trip_session.py")
    spec = importlib.util.spec_from_file_location("round_trip_session", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SESSION = _load_session_module()


def _load_advisor_module():
    path = Path(__file__).with_name("round_trip_provider_advisor.py")
    spec = importlib.util.spec_from_file_location("round_trip_provider_advisor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ADVISOR = _load_advisor_module()


def _load_hypothesis_generator_module():
    path = Path(__file__).with_name("round_trip_hypothesis_generator.py")
    spec = importlib.util.spec_from_file_location("round_trip_hypothesis_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HYPOTHESIS_GENERATOR = _load_hypothesis_generator_module()


class BrowserBriefError(ValueError):
    pass


SEMANTIC_RESEARCH_KINDS = {
    "region_scope_definition",
}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _registry(path: str | Path | None = None) -> dict[str, Any]:
    try:
        return ADVISOR.load_registry(path)
    except ADVISOR.ProviderAdvisorError as exc:
        raise BrowserBriefError(str(exc)) from exc


def _source_advice(state: dict[str, Any], task: dict[str, Any], registry_path: str | Path | None = None) -> dict[str, Any]:
    try:
        return ADVISOR.recommend_sources(state, task, registry_path)
    except ADVISOR.ProviderAdvisorError as exc:
        raise BrowserBriefError(str(exc)) from exc


def _source_options(state: dict[str, Any], task: dict[str, Any], registry_path: str | Path | None = None) -> list[dict[str, Any]]:
    if task.get("research_kind") in SEMANTIC_RESEARCH_KINDS:
        return []
    advice = _source_advice(state, task, registry_path)
    return [
        {
            "source_id": "ai-choice",
            "name": "Any capability-matched source chosen by the AI",
            "url": None,
            "source_type": "open_ai_choice",
            "evidence_role": "depends_on_actual_source",
            "binding": False,
        },
        *copy.deepcopy(advice["suggestions"][:2]),
    ]


def _actionable(state: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [item for item in state["research_tasks"] if item["status"] in {"candidate", "pending"}]
    active = state.get("active_hypothesis")
    if isinstance(active, dict):
        tasks = [item for item in tasks if item["hypothesis_id"] == active["hypothesis_id"]]
    return tasks


def build_catalog(raw_state: Any, registry_path: str | Path | None = None) -> dict[str, Any]:
    state = SESSION.validate_session(raw_state)
    _registry(registry_path)
    actionable = _actionable(state)
    hypotheses = []
    nodes = state["hypothesis_tree"]["nodes"]
    for node in sorted((item for item in nodes.values() if item["hypothesis_id"] != SESSION.ROOT_HYPOTHESIS), key=lambda item: (item.get("sibling_order", 10**9), item["hypothesis_id"])):
        tasks = [item for item in actionable if item["hypothesis_id"] == node["hypothesis_id"]]
        hypotheses.append(
            {
                "hypothesis_id": node["hypothesis_id"],
                "hypothesis_kind": node["hypothesis_kind"],
                "route_shape": node["route_shape"],
                "status": node["status"],
                "is_next_candidate": state["hypothesis_tree"].get("next_candidate_id") == node["hypothesis_id"],
                "tasks": [
                    {
                        "task_id": task["task_id"],
                        "status": task["status"],
                        "question": task["question"],
                        "research_kind": task["research_kind"],
                        "journey_scope": task.get("journey_scope"),
                        "temporal_phase": task.get("temporal_state", {}).get("phase", "not_applicable"),
                        "source_option_count": len(_source_options(state, task, registry_path)),
                        "source_suggestion_ids": [item["source_id"] for item in _source_options(state, task, registry_path)[:3]],
                    }
                    for task in tasks
                ],
            }
        )
    route_suggestions = HYPOTHESIS_GENERATOR.generate_suggestions(state) if state["active_hypothesis"] is None else {"suggestions": [], "selection_instruction": "Close or pause the active hypothesis before considering another route."}
    return {
        "catalog_version": BRIEF_VERSION,
        "decision_owner": "ai",
        "source_registry_is_whitelist": False,
        "registry_order_used_as_preference": False,
        "session_id": state["session_id"],
        "active_hypothesis": copy.deepcopy(state.get("active_hypothesis")),
        "next_candidate_id": state["hypothesis_tree"].get("next_candidate_id"),
        "gateway_candidate_counts": {side: len(items) for side, items in state["gateway_candidates"].items()},
        "actionable_task_count": len(actionable),
        "hypotheses": hypotheses,
        "route_hypothesis_suggestions": copy.deepcopy(route_suggestions["suggestions"]),
        "route_suggestion_instruction": route_suggestions["selection_instruction"],
        "selection_instruction": "Choose any actionable task. Gateway discovery precedes route locking; examples never define the airport pool. Continue a selected hypothesis until terminal disposition or pause.",
    }


def _receipt_contract(
    task: dict[str, Any],
    *,
    semantic: bool,
    pool_screen: bool,
    frontier_screen: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query = task.get("query_shape", {})
    identity = {
        "observation_id": "<unique-observation-id>",
        "source_id": "<observed-source-id>",
        "provider": "<observed-provider>",
        "url": "https://<observed-provider-url>",
        "observed_at": "<ISO-8601-with-offset>",
        "price_selection_method": "price_ascending",
    }
    terminal = {
        "type": "object",
        "required": [
            "observation_id",
            "source_id",
            "provider",
            "url",
            "observed_at",
            "evidence_status",
            "reason",
        ],
        "properties": {
            "observation_id": {"type": "string", "minLength": 1},
            "source_id": {"type": "string", "minLength": 1},
            "provider": {"type": "string", "minLength": 1},
            "url": {"type": "string", "format": "uri"},
            "observed_at": {"type": "string", "format": "date-time"},
            "evidence_status": {"enum": ["no_result", "rejected"]},
            "reason": {"type": "string", "minLength": 1},
            "date_coverage": {
                "enum": ["sampled_date_pair", "exact_dates", "bounded_window"]
            },
        },
        "additionalProperties": False,
    }
    if semantic:
        side = query["side"]
        region_id = query["region_id"]
        seed_ids = [
            str(node.get("node_id"))
            for node in query.get("seed_gateway_hints", [])
            if isinstance(node, dict) and node.get("node_id")
        ]
        example = {
            "observation_id": "<unique-observation-id>",
            "evidence_status": "ai_proposed",
            "proposal_type": "region_scope",
            "proposed_by": "ai",
            "proposed_at": "<ISO-8601-with-offset>",
            "reason": "<why this bounded scope represents the region>",
            "proposal": {
                "proposal_version": "1.0",
                "side": side,
                "region_id": region_id,
                "candidates": [
                    {
                        "gateway_node_id": (
                            seed_ids[0]
                            if seed_ids
                            else "<known-IATA-or-stable-node-id>"
                        ),
                        "roles": ["primary_airport"],
                        "inclusion_reason": "<why this airport belongs in scope>",
                    }
                ],
                "excluded_seed_nodes": [],
                "scope_reason": "<bounded-scope rationale>",
            },
        }
        success = {
            "type": "object",
            "required": [
                "observation_id",
                "evidence_status",
                "proposal_type",
                "proposed_by",
                "proposed_at",
                "reason",
                "proposal",
            ],
            "properties": {
                "observation_id": {"type": "string", "minLength": 1},
                "evidence_status": {"const": "ai_proposed"},
                "proposal_type": {"const": "region_scope"},
                "proposed_by": {"const": "ai"},
                "proposed_at": {"type": "string", "format": "date-time"},
                "reason": {"type": "string", "minLength": 1},
                "proposal": {
                    "type": "object",
                    "required": [
                        "proposal_version",
                        "side",
                        "region_id",
                        "candidates",
                        "excluded_seed_nodes",
                        "scope_reason",
                    ],
                    "properties": {
                        "proposal_version": {"const": "1.0"},
                        "side": {"const": side},
                        "region_id": {"const": region_id},
                        "candidates": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "oneOf": [
                                    {
                                        "type": "object",
                                        "required": [
                                            "gateway_node_id",
                                            "roles",
                                            "inclusion_reason",
                                        ],
                                        "properties": {
                                            "gateway_node_id": {
                                                "enum": seed_ids
                                            },
                                            "roles": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "enum": sorted(
                                                        SESSION.AI_EXPLORATION.REGION_ROLES
                                                    )
                                                },
                                            },
                                            "inclusion_reason": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                        },
                                        "additionalProperties": False,
                                    },
                                    {
                                        "type": "object",
                                        "required": [
                                            "node",
                                            "roles",
                                            "inclusion_reason",
                                        ],
                                        "properties": {
                                    "candidate_id": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "node": {
                                        "type": "object",
                                        "required": [
                                            "node_id",
                                            "node_type",
                                            "location",
                                            "country_or_region",
                                            "country_code",
                                            "timezone",
                                            "utc_offset",
                                        ],
                                        "properties": {
                                            "node_id": {"type": "string", "minLength": 1},
                                            "node_type": {"const": "airport"},
                                            "location": {"type": "string", "minLength": 1},
                                            "country_or_region": {"type": "string", "minLength": 1},
                                            "country_code": {"type": "string", "pattern": "^[A-Z]{2}$"},
                                            "timezone": {"type": "string", "minLength": 1},
                                            "utc_offset": {"type": "string", "pattern": "^[+-][0-9]{2}:[0-9]{2}$"},
                                        },
                                        "additionalProperties": False,
                                    },
                                    "roles": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "enum": sorted(
                                                SESSION.AI_EXPLORATION.REGION_ROLES
                                            )
                                        },
                                    },
                                    "inclusion_reason": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                        },
                                        "additionalProperties": False,
                                    },
                                ]
                            },
                        },
                        "excluded_seed_nodes": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "scope_reason": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
    elif task.get("research_kind") == SESSION.INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND:
        example = {
            "evidence_status": "verified_observed",
            "internal_surface_transfer": {
                "from_node": query["from_node"],
                "to_node": query["to_node"],
                "approximate_duration_minutes": "<positive-integer>",
                "feasibility": "verified_route",
                "evidence_url": "https://<route-evidence-url>",
                "observed_at": "<ISO-8601-with-offset>",
            },
        }
        success = {
            "type": "object",
            "required": ["evidence_status", "internal_surface_transfer"],
            "properties": {
                "evidence_status": {"const": "verified_observed"},
                "internal_surface_transfer": {
                    "type": "object",
                    "required": [
                        "from_node",
                        "to_node",
                        "approximate_duration_minutes",
                        "feasibility",
                        "evidence_url",
                        "observed_at",
                    ],
                    "properties": {
                        "from_node": {"const": query["from_node"]},
                        "to_node": {"const": query["to_node"]},
                        "approximate_duration_minutes": {
                            "type": "integer",
                            "minimum": 1,
                        },
                        "feasibility": {
                            "enum": [
                                "verified_route",
                                "fallback_plausible",
                                "infeasible",
                            ]
                        },
                        "evidence_url": {"type": "string", "format": "uri"},
                        "observed_at": {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        terminal = success
    elif pool_screen:
        fare_example = {
            "origin_iata": "<observed-origin-airport-code>",
            "destination_iata": "<observed-destination-airport-code>",
            "outbound_departure_local_date": "<YYYY-MM-DD>",
            "outbound_destination_arrival_local_date": "<YYYY-MM-DD>",
            "return_departure_local_date": "<YYYY-MM-DD>",
            "total": {"amount": "<number>", "currency": "<currency>"},
        }
        example = {
            "outcome": "fare_found",
            "page_url": "https://<visible-browser-page>",
            "price_selection_method": "lowest_price_view",
            "fare": fare_example,
        }
        fare_schema = {
            "type": "object",
            "required": [
                "origin_iata",
                "destination_iata",
                "outbound_departure_local_date",
                "outbound_destination_arrival_local_date",
                "return_departure_local_date",
                "total",
            ],
            "properties": {
                "origin_iata": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "destination_iata": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "outbound_departure_local_date": {
                    "type": "string",
                    "format": "date",
                },
                "outbound_destination_arrival_local_date": {
                    "type": "string",
                    "format": "date",
                },
                "return_departure_local_date": {
                    "type": "string",
                    "format": "date",
                },
                "total": {
                    "type": "object",
                    "required": ["amount", "currency"],
                    "properties": {
                        "amount": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                        },
                        "currency": {
                            "type": "string",
                            "pattern": "^[A-Z]{3}$",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        success = {
            "type": "object",
            "required": [
                "outcome",
                "page_url",
                "price_selection_method",
                "fare",
            ],
            "properties": {
                "outcome": {"const": "fare_found"},
                "page_url": {"type": "string", "format": "uri"},
                "price_selection_method": {
                    "enum": [
                        "all_visible_compared",
                        "lowest_price_view",
                        "price_ascending",
                    ]
                },
                "fare": fare_schema,
            },
            "additionalProperties": False,
        }
        no_result = {
            "type": "object",
            "required": [
                "outcome",
                "page_url",
                "price_selection_method",
                "reason",
            ],
            "properties": {
                "outcome": {"const": "no_result"},
                "page_url": {"type": "string", "format": "uri"},
                "price_selection_method": {
                    "enum": [
                        "all_visible_compared",
                        "lowest_price_view",
                        "price_ascending",
                    ]
                },
                "reason": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        }
        unsupported = {
            "type": "object",
            "required": ["outcome", "page_url", "reason"],
            "properties": {
                "outcome": {"const": "unsupported_view"},
                "page_url": {"type": "string", "format": "uri"},
                "reason": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        }
        terminal = {"oneOf": [no_result, unsupported]}
    elif frontier_screen:
        price_challenge = (
            task["research_kind"]
            == SESSION.PRICE_CHALLENGE_SCREEN_RESEARCH_KIND
        )
        destination_id = query["destination_gateway_node_ids"][0]
        node_schema = {
            "type": "object",
            "required": [
                "node_id",
                "node_type",
                "location",
                "country_or_region",
                "country_code",
                "timezone",
                "utc_offset",
            ],
            "properties": {
                "node_id": {"type": "string", "minLength": 1},
                "node_type": {"const": "airport"},
                "location": {"type": "string", "minLength": 1},
                "country_or_region": {"type": "string", "minLength": 1},
                "country_code": {"type": "string", "pattern": "^[A-Z]{2}$"},
                "timezone": {"type": "string", "minLength": 1},
                "utc_offset": {
                    "type": "string",
                    "pattern": "^[+-][0-9]{2}:[0-9]{2}$",
                },
            },
            "additionalProperties": False,
        }
        money_schema = {
            "type": "object",
            "required": ["amount", "currency"],
            "properties": {
                "amount": {"type": "number", "minimum": 0},
                "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
            },
            "additionalProperties": False,
        }
        dates_schema = {
            "type": "object",
            "required": [
                "outbound_departure_local_date",
                "outbound_destination_arrival_local_date",
                "return_departure_local_date",
            ],
            "properties": {
                "outbound_departure_local_date": {
                    "type": "string",
                    "format": "date",
                },
                "outbound_destination_arrival_local_date": {
                    "type": "string",
                    "format": "date",
                },
                "return_departure_local_date": {
                    "type": "string",
                    "format": "date",
                },
            },
            "additionalProperties": False,
        }
        if price_challenge:
            candidate_example = {
                "node": {
                    "node_id": "<outbound-positioning-hub-id>",
                    "node_type": "airport",
                    "location": "<outbound-positioning-hub-name>",
                    "country_or_region": "<country-or-region>",
                    "country_code": "<ISO-country-code>",
                    "timezone": "<IANA-timezone>",
                    "utc_offset": "<UTC-offset>",
                },
                "long_haul_airfare": {
                    "amount": "<number>",
                    "currency": "<currency>",
                },
                "positioning_airfare_lower_bound": {
                    "amount": "<number>",
                    "currency": "<currency>",
                },
                "destination_gateway_node_id": destination_id,
                "itinerary_dates": {
                    "outbound_departure_local_date": "<YYYY-MM-DD>",
                    "outbound_destination_arrival_local_date": "<YYYY-MM-DD>",
                    "return_departure_local_date": "<YYYY-MM-DD>",
                },
            }
            candidate_schema = {
                "type": "object",
                "required": [
                    "node",
                    "long_haul_airfare",
                    "positioning_airfare_lower_bound",
                    "destination_gateway_node_id",
                    "itinerary_dates",
                ],
                "properties": {
                    "node": node_schema,
                    "long_haul_airfare": money_schema,
                    "positioning_airfare_lower_bound": copy.deepcopy(
                        money_schema
                    ),
                    "destination_gateway_node_id": {
                        "enum": list(query["destination_gateway_node_ids"])
                    },
                    "return_origin_gateway_node_id": {
                        "enum": list(query["destination_gateway_node_ids"])
                    },
                    "return_hub_node": copy.deepcopy(node_schema),
                    "itinerary_dates": dates_schema,
                },
                "additionalProperties": False,
            }
        else:
            candidate_example = {
                "candidate_id": "<legacy-candidate-id>",
                "side": "origin",
                "node": {
                    "node_id": "<legacy-outbound-hub-id>",
                    "node_type": "airport",
                    "location": "<legacy-outbound-hub-name>",
                    "country_or_region": "<country-or-region>",
                    "country_code": "<ISO-country-code>",
                    "timezone": "<IANA-timezone>",
                    "utc_offset": "<UTC-offset>",
                },
                "signal": {
                    "kind": "portfolio_frontier_fare",
                    "signal_id": "<legacy-signal-id>",
                    "dated_fare_amount": {
                        "amount": "<number>",
                        "currency": "<currency>",
                    },
                    "date_coverage": "bounded_window",
                    "outbound_origin_gateway_node_id": "<legacy-outbound-hub-id>",
                    "outbound_destination_gateway_node_id": destination_id,
                    "return_origin_gateway_node_id": destination_id,
                    "return_destination_gateway_node_id": "<legacy-return-hub-id>",
                    "temporal_signal_version": "2.0",
                    "itinerary_dates": {
                        "outbound_departure_local_date": "<YYYY-MM-DD>",
                        "outbound_destination_arrival_local_date": "<YYYY-MM-DD>",
                        "return_departure_local_date": "<YYYY-MM-DD>",
                    },
                    "hub_cell_id": "<legacy-cell-id>",
                    "screened_hub_cells": [
                        {
                            "hub_cell_id": "<legacy-cell-id>",
                            "airport_node_ids": ["<legacy-outbound-hub-id>"],
                        }
                    ],
                },
            }
            candidate_schema = {"type": "object"}
        example = {
            **identity,
            "price_selection_method": "lowest_price_view",
            "evidence_status": "research_observed",
            "gateway_candidates": [candidate_example],
        }
        gateway_schema = {
            "type": "array",
            "minItems": 1,
            "items": candidate_schema,
        }
        if not price_challenge:
            gateway_schema["maxItems"] = int(query.get("max_candidates", 2))
        success = {
            "type": "object",
            "required": [*identity, "evidence_status", "gateway_candidates"],
            "properties": {
                "observation_id": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "provider": {"type": "string", "minLength": 1},
                "url": {"type": "string", "format": "uri"},
                "observed_at": {"type": "string", "format": "date-time"},
                "price_selection_method": {
                    "enum": [
                        "all_visible_compared",
                        "lowest_price_view",
                        "price_ascending",
                    ]
                },
                "evidence_status": {"const": "research_observed"},
                "gateway_candidates": gateway_schema,
            },
            "additionalProperties": False,
        }
    else:
        scope = task.get("journey_scope")
        direction = task.get("direction")
        movement_mode = "<allowed-surface-mode>" if scope == "surface" else "flight"
        movement = {
            "movement_id": "<unique-movement-id>",
            "from_node": "<declared-or-new-node-id>",
            "to_node": "<declared-or-new-node-id>",
            "departure": "<ISO-8601-with-offset>",
            "arrival": "<ISO-8601-with-offset>",
            "mode": movement_mode,
            "operator": "<operator>",
            "service_id": "<service-or-flight-id>",
        }
        if movement_mode == "flight":
            movement["flight_identity"] = {
                "marketing_carrier": "<carrier-code>",
                "flight_numbers": ["<flight-number>"],
            }
        offer = {
            "offer_id": "<same-value-as-observation_id>",
            "provider_offer_id": "<provider-offer-id>",
            "movement_ids": ["<movement-id>"],
            "price": {"amount": "<number>", "currency": "<currency>"},
            "mandatory_baggage_cost": {
                "amount": 0,
                "currency": "<currency>",
            },
            "included_baggage": [],
            "seller": "<seller>",
            "booking_url": "https://<seller-booking-or-handoff-url>",
        }
        example = {
            **identity,
            "evidence_status": "verified_observed",
            "nodes": ["<full RegionNode objects for every new endpoint>"],
            "movements": [movement],
            "offers": [offer],
        }
        if scope != "surface":
            example["seller_listing"] = {
                "scope": "exact_itinerary_booking_options",
                "terminal_witness": {
                    "kind": "end_of_list",
                    "observed_offer_count": 1,
                    "next_page_available": False,
                },
            }
        money_schema = {
            "type": "object",
            "required": ["amount", "currency"],
            "properties": {
                "amount": {"type": "number", "minimum": 0},
                "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
            },
            "additionalProperties": False,
        }
        node_schema = {
            "type": "object",
            "required": [
                "node_id",
                "node_type",
                "location",
                "country_or_region",
                "country_code",
                "timezone",
                "utc_offset",
            ],
            "properties": {
                "node_id": {"type": "string", "minLength": 1},
                "node_type": {
                    "enum": [
                        "airport",
                        "railway_station",
                        "bus_terminal",
                        "ferry_terminal",
                        "city_destination",
                    ]
                },
                "location": {"type": "string", "minLength": 1},
                "country_or_region": {"type": "string", "minLength": 1},
                "country_code": {"type": "string", "pattern": "^[A-Z]{2}$"},
                "timezone": {"type": "string", "minLength": 1},
                "utc_offset": {"type": "string", "pattern": "^[+-][0-9]{2}:[0-9]{2}$"},
            },
            "additionalProperties": False,
        }
        movement_schema = {
            "type": "object",
            "required": [
                "movement_id",
                "from_node",
                "to_node",
                "departure",
                "arrival",
                "mode",
                "operator",
                "service_id",
            ],
            "properties": {
                "movement_id": {"type": "string", "minLength": 1},
                "from_node": {"type": "string", "minLength": 1},
                "to_node": {"type": "string", "minLength": 1},
                "departure": {"type": "string", "format": "date-time"},
                "arrival": {"type": "string", "format": "date-time"},
                "mode": (
                    {
                        "enum": list(
                            query.get("pool_constraints", {}).get(
                                "allowed_modes", []
                            )
                        )
                    }
                    if scope == "surface"
                    else {"enum": sorted(SESSION.SOLVER.ALLOWED_MODES)}
                ),
                "operator": {"type": "string", "minLength": 1},
                "service_id": {"type": "string", "minLength": 1},
                "flight_identity": {
                    "type": "object",
                    "required": ["marketing_carrier", "flight_numbers"],
                    "properties": {
                        "marketing_carrier": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "operating_carrier": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "flight_numbers": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                    "additionalProperties": False,
                },
                "distance_km": {"type": "number", "minimum": 0},
                "transfer_from_previous": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"enum": ["verified", "unknown", "infeasible"]},
                        "connection_protection": {
                            "enum": ["carrier_protected", "self_transfer", "unknown"]
                        },
                        "minimum_connection_minutes": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            },
            "allOf": (
                [
                    {
                        "if": {"properties": {"mode": {"const": "flight"}}},
                        "then": {"required": ["flight_identity"]},
                    }
                ]
                if scope != "surface"
                else []
            ),
            "additionalProperties": False,
        }
        offer_required = [
            "offer_id",
            "provider_offer_id",
            "movement_ids",
            "price",
            "mandatory_baggage_cost",
            "included_baggage",
            "seller",
            "booking_url",
        ]
        refresh_targets = (
            query.get("price_refresh", {}).get("refresh_targets", [])
            if isinstance(query.get("price_refresh"), dict)
            else []
        )
        offer_schema = {
            "type": "object",
            "required": offer_required,
            "properties": {
                "offer_id": {"type": "string", "minLength": 1},
                "refresh_of_offer_id": (
                    {
                        "enum": [
                            str(target["offer_id"])
                            for target in refresh_targets
                            if isinstance(target, dict) and target.get("offer_id")
                        ]
                    }
                    if refresh_targets
                    else {"type": "string", "minLength": 1}
                ),
                "provider_offer_id": {"type": "string", "minLength": 1},
                "movement_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "price": copy.deepcopy(money_schema),
                "mandatory_baggage_cost": copy.deepcopy(money_schema),
                "included_baggage": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "seller": {"type": "string", "minLength": 1},
                "booking_url": {"type": "string", "format": "uri"},
                "expires_at": {"type": "string", "format": "date-time"},
                "ticket_context": {"type": "object"},
                "entitlement_tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "additionalProperties": False,
        }
        seller_listing_schema = {
            "type": "object",
            "required": ["scope", "terminal_witness"],
            "properties": {
                "scope": {"const": "exact_itinerary_booking_options"},
                "terminal_witness": {
                    "type": "object",
                    "required": [
                        "kind",
                        "observed_offer_count",
                        "next_page_available",
                    ],
                    "properties": {
                        "kind": {
                            "enum": [
                                "provider_declared_count",
                                "end_of_list",
                                "pagination_exhausted",
                            ]
                        },
                        "observed_offer_count": {"type": "integer", "minimum": 1},
                        "provider_reported_offer_count": {"type": "integer", "minimum": 1},
                        "next_page_available": {"const": False},
                        "source_artifact_ref": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        success_required = [*identity, "evidence_status", "movements", "offers"]
        if scope != "surface":
            success_required.append("seller_listing")
        success = {
            "type": "object",
            "required": success_required,
            "properties": {
                "observation_id": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
                "provider": {"type": "string", "minLength": 1},
                "url": {"type": "string", "minLength": 1},
                "observed_at": {"type": "string", "minLength": 1},
                "price_selection_method": {
                    "enum": [
                        "all_visible_compared",
                        "lowest_price_view",
                        "price_ascending",
                    ]
                },
                "evidence_status": {"const": "verified_observed"},
                "nodes": {"type": "array", "items": node_schema},
                "movements": {
                    "type": "array",
                    "minItems": 1,
                    "items": movement_schema,
                },
                "offers": {
                    "type": "array",
                    "minItems": 1,
                    "items": offer_schema,
                },
                "seller_listing": seller_listing_schema,
            },
            "additionalProperties": False,
        }
    schema = {
        "type": "object",
        "required": ["observations"],
        "properties": {
            "observations": {
                "type": "array",
                "minItems": 1,
        "items": (
            success
            if semantic
            or task.get("research_kind")
            == SESSION.INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND
            else {"oneOf": [success, terminal]}
        ),
            },
        },
        "additionalProperties": False,
    }
    template = {
        "observations": [example],
    }
    return schema, template


def task_to_brief(raw_state: Any, task_id: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    state = SESSION.validate_session(raw_state)
    _registry(registry_path)
    task = next((item for item in state["research_tasks"] if item["task_id"] == task_id), None)
    if task is None or task["status"] not in {"candidate", "pending"}:
        raise BrowserBriefError("task must be an AI-actionable candidate or pending task")
    pool_screen = task["research_kind"] == "region_pool_fare_screen"
    frontier_screen = task["research_kind"] in {
        SESSION.FRONTIER_SCREEN_RESEARCH_KIND,
        SESSION.PRICE_CHALLENGE_SCREEN_RESEARCH_KIND,
    }
    semantic = task["research_kind"] in SEMANTIC_RESEARCH_KINDS
    temporal_state = copy.deepcopy(task.get("temporal_state", {}))
    temporal_applicable = temporal_state.get("phase") not in {None, "not_applicable", "terminal"}
    if semantic:
        contract = {
            "evidence_status": "ai_proposed",
            "proposal_type": "region_scope",
            "rule": (
                "Use AI semantic judgment and return a bounded structured proposal. This is "
                "persisted AI state, not live Provider evidence and not a priced offer."
            ),
        }
    elif pool_screen:
        challenger_screen = (
            task.get("query_shape", {}).get("trip_type")
            == "regional_challenger_screen"
        )
        contract = {
            "outcome": "fare_found|no_result|unsupported_view",
            "signal_kind": "joint_date_pair",
            "required_signal_fields": [
                "origin_iata",
                "destination_iata",
                "outbound_departure_local_date",
                "outbound_destination_arrival_local_date",
                "return_departure_local_date",
                "total",
            ],
            "price_selection_method": (
                "Required: lowest_price_view, price_ascending, or "
                "all_visible_compared from a user-visible Browser page."
            ),
            "rule": (
                (
                    "Use a public Browser Provider whose visible controls support "
                    "the required country/region, multi-origin, flexible-date, and "
                    "price-ordering capabilities. "
                    if task.get("query_shape", {}).get("scope_strategy")
                    == "provider_native_first"
                    else "Search only this Work Item's fallback airport rectangle "
                    "and date rows through a visible price-ranked view. "
                )
                + (
                    ""
                    if task.get("query_shape", {}).get("scope_strategy")
                    == "provider_native_first"
                    else
                    "Exclude every query.excluded_endpoint_cells pair. When a Provider "
                    "accepts only Cartesian airport pools, submit rectangular partitions "
                    "that contain no excluded pair. "
                    if challenger_screen
                    else "Use the exact airport lists already published in this slice. "
                )
                + "Return only the visible local minimum as fare_found, or report "
                "no_result/unsupported_view. A no_result Capture must include the "
                "visible price-ordering method; unsupported_view does not claim that "
                "the query ran. Do not copy scope, Node metadata, IDs, Provider names, "
                "timestamps, or coverage conclusions. The Product Adapter derives the "
                "executed Provider-visible partition from the immutable Work Item and "
                "Receipt. One Capture closes only that published partition."
            ),
        }
    elif frontier_screen:
        price_challenge = (
            task["research_kind"]
            == SESSION.PRICE_CHALLENGE_SCREEN_RESEARCH_KIND
        )
        contract = {
            "evidence_status": "research_observed",
            "candidate_scope": (
                "price_challenge" if price_challenge else "portfolio_frontier"
            ),
            "required_candidate_fields": (
                [
                    "airport node",
                    "long_haul_airfare as Money",
                    "positioning_airfare_lower_bound as Money",
                    "destination_gateway_node_id",
                    "itinerary_dates",
                    "return_origin_gateway_node_id only for an open jaw",
                    "return_hub_node only for an open jaw",
                ]
                if price_challenge
                else [
                    "candidate_id",
                    "side=origin",
                    "airport node",
                    "signal.kind=portfolio_frontier_fare",
                    "signal.signal_id",
                    "signal.dated_fare_amount as Money",
                    "signal.hub_cell_id",
                    "signal.screened_hub_cells",
                ]
            ),
            "price_selection_method": "A Provider-native price-ranked method is required.",
            "rule": (
                "Search outside excluded_gateway_node_ids with a Provider-native "
                "price-ranked method over the complete date and stay range. There is "
                "no airport, hub-cell, or candidate-count quota. A signal is "
                "research-only; the Product Kernel applies the Price Challenge "
                "Threshold to long-haul fare plus mandatory positioning-airfare "
                "lower bound before publishing exact tickets. Ground movement and "
                "accommodation never enter this lower bound."
                if price_challenge
                else "Read the persisted legacy frontier contract exactly as published."
            ),
        }
    else:
        contract = {
            "evidence_status": "verified_observed",
            "observation_identity": ["observation_id", "source_id", "provider", "url", "observed_at", "price_selection_method"],
            "movement_fields": ["movement_id", "from_node", "to_node", "departure", "arrival", "mode", "operator", "service_id", "flight_identity for flight", "transfer_from_previous with status and connection_protection when researched"],
            "offer_fields": ["offer_id", "refresh_of_offer_id when price_refresh", "provider_offer_id", "source_id", "pricing_scope=atomic_offer", "journey_scope", "direction when one_way", "movement_ids", "price", "mandatory_baggage_cost", "included_baggage", "provider", "seller", "url", "observed_at", "expires_at when visible", "ticket_context for planned exit", "evidence_status=verified_observed"],
            "rule": (
                "Preserve every atomic ticket or surface quote whole. The price and "
                "mandatory baggage cost must cover the brief's traveler_count and baggage_policy. "
                "A required item missing from included_baggage is admissible only when "
                "paid_baggage_allowed=true and the Offer records a positive mandatory_baggage_cost. "
                "Never invent component prices; include RegionNode objects for newly observed "
                "movement endpoints. Mark connection_protection=carrier_protected only when the "
                "payable itinerary or seller explicitly establishes a protected through-ticket; "
                "atomic pricing alone is not proof. "
                + (
                    "This is an exact price refresh: revisit only query.price_refresh.refresh_targets "
                    "without reopening airport or date discovery. Only a newly observed Offer from "
                    "the same source, Provider, Seller, and itinerary may set refresh_of_offer_id. "
                    "Other named Sellers on the same itinerary are sibling Seller Variants and must "
                    "omit refresh_of_offer_id."
                    if isinstance(task.get("query_shape", {}).get("price_refresh"), dict)
                    else ""
                )
            ),
            "temporal_research_option": (
                {
                    "evidence_status": "research_observed",
                    "coverage_complete": False,
                    "signal_kind": "joint_date_pair" if task.get("journey_scope") == "joint_round_trip" else "directional_date",
                    "admission_role": "research_only",
                    "creates_offer_or_movement": False,
                    "rule": "Calendar, grid, and flexible-search signals keep this same task pending. They never become payable evidence. The AI may instead proceed directly to exact verification when useful.",
                }
                if temporal_applicable
                else None
            ),
        }
        if task.get("journey_scope") == "surface":
            contract["rule"] += (
                " One Receipt must describe a complete priced movement chain from a "
                "published origin to a published destination. The first departure "
                "must remain inside departure_window and respect not_before; the final "
                "arrival must respect must_arrive_by. A truthful but incomplete or "
                "infeasible submission remains recorded in its Receipt but its Offers "
                "are not admitted to the solver and it does not complete the Work Item, "
                "which will be retried under the same task_id. A single "
                "Provider no-result or rejection does not prove airport access infeasible. "
                "When date_search.selection_order=listed, search those exact local dates "
                "in order before reporting no result."
            )
        elif isinstance(task.get("query_shape", {}).get("schedule_context"), dict):
            contract["rule"] += (
                " Every payable Movement path must use one of the published exact "
                "departure dates and satisfy every structured schedule_context rule. "
                "An arrival after must_arrive_by or a departure before not_before is "
                "incompatible Evidence and cannot satisfy this Work Item."
            )
    advice = (
        {
            "dynamic_source_rules": [],
            "task_family": "ai_semantic_exploration",
        }
        if semantic
        or task.get("research_kind")
        == SESSION.INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND
        else _source_advice(state, task, registry_path)
    )
    if task.get("research_kind") == SESSION.INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND:
        advice = {
            "dynamic_source_rules": [],
            "task_family": "internal_surface_transfer",
        }
    query_shape = copy.deepcopy(task["query_shape"])
    if temporal_state.get("active_selection") is not None:
        query_shape["ai_selected_exact_target"] = copy.deepcopy(temporal_state["active_selection"])
    attempt_product_schema, observations_template = _receipt_contract(
        task,
        semantic=semantic,
        pool_screen=pool_screen,
        frontier_screen=frontier_screen,
    )
    return {
        "brief_version": BRIEF_VERSION,
        "task_id": task["task_id"],
        "task_status": task["status"],
        "hypothesis": {key: task.get(key) for key in ("hypothesis_id", "hypothesis_kind", "parent_hypothesis_id", "route_shape")},
        "research_kind": task["research_kind"],
        "query_shape": query_shape,
        "traveler_count": state["scenario"]["intent"]["traveler_count"],
        "baggage_policy": copy.deepcopy(state["scenario"]["intent"]["baggage_policy"]),
        "source_options": (
            []
            if task.get("research_kind")
            == SESSION.INTERNAL_SURFACE_TRANSFER_RESEARCH_KIND
            else _source_options(state, task, registry_path)
        ),
        "dynamic_source_rules": advice["dynamic_source_rules"],
        "source_policy": {"decision_owner": "ai", "registry_is_whitelist": False, "registry_order_is_preference": False, "one_task_per_research_question": True, "source_fan_out": False, "task_family": advice["task_family"], "research_only_source_requires_payable_offer_verification": not semantic},
        "temporal_execution": {
            **temporal_state,
            "decision_owner": "ai",
            "selection_advice_binding": False,
            "scheduler_may_not_publish_or_select_tasks": True,
            "same_task_id_for_surface_and_exact_verification": True,
        },
        "evidence_contract": contract,
        "attempt_product_schema": attempt_product_schema,
        "terminal_evidence_contract": (
            {
                "fields": [
                    "outcome=no_result|unsupported_view",
                    "page_url",
                    "price_selection_method when outcome=no_result",
                    "reason",
                ]
            }
            if pool_screen
            else {
                "fields": [
                    "observation_id",
                    "source_id",
                    "evidence_status=no_result|rejected",
                    "provider",
                    "url",
                    "observed_at",
                    "reason",
                    "date_coverage when the Provider exposes a date scope",
                ]
            }
            if not semantic
            else None
        ),
        "observations_template": observations_template,
    }


def catalog_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# AI round-trip research decision catalog",
        "",
        f"- Active hypothesis: {catalog['active_hypothesis'] or 'none'}",
        f"- Next candidate (advisory): {catalog['next_candidate_id']}",
        f"- Gateway candidates: {catalog['gateway_candidate_counts']}",
        f"- Actionable tasks: {catalog['actionable_task_count']}",
        "- Source registry is not a whitelist; AI owns source and route choice.",
        "- Work Items carry at most three source hints; registry file order is ignored.",
    ]
    for hypothesis in catalog["hypotheses"]:
        lines.extend(["", f"## {hypothesis['hypothesis_id']}", f"- Kind: {hypothesis['hypothesis_kind']}", f"- Shape: {hypothesis['route_shape']}", f"- Status: {hypothesis['status']}"])
        for task in hypothesis["tasks"]:
            lines.append(f"- `{task['task_id']}` [{task['status']}] {task['research_kind']}/{task.get('journey_scope')}: {task['question']}")
    if catalog.get("route_hypothesis_suggestions"):
        lines.extend(["", "## Evidence-backed route suggestions"])
        for suggestion in catalog["route_hypothesis_suggestions"]:
            lines.append(f"- `{suggestion['hypothesis_id']}` {suggestion['route_shape']}: {suggestion['origin_gateway_nodes']} -> {suggestion['destination_gateway_nodes']} — joint atomic quote is the first baseline")
        lines.append(catalog["route_suggestion_instruction"])
    lines.extend(["", catalog["selection_instruction"]])
    return "\n".join(lines) + "\n"


def brief_markdown(brief: dict[str, Any]) -> str:
    lines = [
        "# AI browser execution brief",
        "",
        f"- Task: `{brief['task_id']}`",
        f"- Hypothesis: `{brief['hypothesis']['hypothesis_id']}`",
        f"- Research kind: `{brief['research_kind']}`",
        f"- Query shape: `{json.dumps(brief['query_shape'], ensure_ascii=False)}`",
        f"- Evidence rule: {brief['evidence_contract']['rule']}",
        "- Source choice belongs to the AI; suggestions are non-binding.",
        "",
        "## Source options",
    ]
    for source in brief["source_options"]:
        lines.append(f"- {source['name']} (`{source['source_id']}`): {source['evidence_role']} — {source['url'] or 'AI-selected URL'}")
    active_dynamic = [item for item in brief.get("dynamic_source_rules", []) if item.get("activation_state") == "applicable"]
    if active_dynamic:
        lines.extend(["", "## Triggered dynamic sources"])
        for source in active_dynamic:
            lines.append(f"- {source['name']} (`{source['source_id']}`): {source['trigger']}")
    lines.extend(["", "## Evidence output", "", "```json", json.dumps(brief["observations_template"], ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--registry")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args(argv)
    try:
        state = read_json(args.state)
        value = task_to_brief(state, args.task_id, args.registry) if args.task_id else build_catalog(state, args.registry)
        output = json.dumps(value, ensure_ascii=False, indent=2) if args.format == "json" else (brief_markdown(value) if args.task_id else catalog_markdown(value))
        print(output, end="" if output.endswith("\n") else "\n")
        return 0
    except (OSError, json.JSONDecodeError, SESSION.RoundTripSessionError, BrowserBriefError) as exc:
        print(f"round_trip_browser_task_brief: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
