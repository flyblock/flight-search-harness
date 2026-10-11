---
name: low-cost-round-trip-flight-search
description: Search cheap round-trip flights by comparing complete low-cost ticket portfolios to a selected destination and back. Use when the user wants to return to the origin region.
---

# Low-Cost Round-Trip Flight Search

Use the round-trip Product Kernel through the Flight Search Harness. This workflow compares complete outbound-and-return portfolios rather than reusing one-way frontier state.

## Prepare

1. From the caller workspace, run `python <skill-directory>/scripts/resolve_harness.py`. Keep the returned `launcher_ref`, `mission_runtime_ref`, and `request_schema_ref`; the resolver uses the bundled Harness when present and obtains the matching Harness when this Skill was installed by itself.
2. Copy and fill [the compact request template](assets/request-template.json). Replace every `REPLACE_...` value and store the request in the caller workspace, normally under `runs/requests/`.
3. Read [the round-trip workflow](references/product-workflow.md) and the returned `mission_runtime_ref` for product decisions and the shared Search Mission procedure.
4. Read the returned public `request_schema_ref` only when uncommon optional constraints or a request-validation failure require fields beyond the template. Product Kernel Schemas are internal and are not Skill request contracts.

Always capture `base_currency`, `adult_count`, the shared `baggage_policy`, `overnight_allowed`, and `self_transfer_policy`; do not infer them from an earlier search or geography. This Product supports adult travelers only: treat an unspecified age as adult, but stop with a focused limitation when the user explicitly includes a child or infant. Compile no cabin-baggage requirement as `cabin_baggage: "none_required"`; this does not exclude fares that include baggage. When the user says baggage does not matter, use `none_required`, `checked_bag_count: 0`, and `paid_baggage_allowed: true` without asking. Otherwise use `personal_item`, `cabin_bag`, or `personal_item_and_cabin_bag` and ask only when the missing distinction would change acceptable Offers. Request Provider prices in the Base Currency; the Harness owns any required system FX normalization. Set each regional `max_ground_cost` to an explicit Base Currency amount or `null` for no cost ceiling. Keep `endpoint_policy: "anchor_required"` when surrounding airports require priced access or egress; use `any_admitted_gateway` only when any admitted airport is itself an accepted journey endpoint.

Ask one focused question when a missing origin or destination region, outbound or return boundary, Base Currency, adult count, baggage policy, overnight policy, self-transfer policy, or hard constraint would materially change the search.

`airport_transit_unknown_policy: "allow_with_warning"` keeps an explicitly unverified candidate; it never establishes that the adult traveler can legally or practically transit. Disclose the unknown status and do not describe it as verified transit eligibility.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow the Product Kernel's gateway, portfolio, and browser briefs as the active search contract, submit item-bound Receipts, and continue through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

For a new user search, start a fresh Run and do not inspect or reuse another chat's Run, old Receipt, Session, result, or price. Resume only a Run created earlier in the current task, unless the user explicitly names an older Run to continue.

For every fare-bearing flight search, establish the lowest compatible result through the Provider's lowest-price view, ascending price sort, price-sorted API, or comparison of all visible results. Record the generic `price_selection_method`; default recommended ordering and an unopened lower-price label cannot support a lowest-price claim.

## Deliver

Present the best verified complete round-trip portfolio within the investigated Evidence when the Product Kernel returns a `completed` Outcome. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
