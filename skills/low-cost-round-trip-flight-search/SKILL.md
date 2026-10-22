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

Always capture `base_currency`, `adult_count`, the shared `baggage_policy`, `overnight_allowed`, and `self_transfer_policy`; do not infer them from an earlier search or geography. Use `search_effort: "standard"` unless the user explicitly asks for a quicker `economy` or broader `deep` search. This Product supports adult travelers only: treat an unspecified age as adult, but stop with a focused limitation when the user explicitly includes a child or infant. Compile no cabin-baggage requirement as `cabin_baggage: "none_required"`; this does not exclude fares that include baggage. When the user says baggage does not matter, use `none_required`, `checked_bag_count: 0`, and `paid_baggage_allowed: true` without asking. Otherwise use `personal_item`, `cabin_bag`, or `personal_item_and_cabin_bag` and ask only when the missing distinction would change acceptable Offers. Request Provider prices in the Base Currency; the Harness owns any required system FX normalization. For a regional city or area request, use `endpoint_policy: "any_admitted_gateway"`: the AI-defined Self-access Gateway Pool is disclosed and the user arranges access and egress. Use `anchor_required` only for an exact airport endpoint explicitly requested by the user. Keep `max_ground_cost: null` because Endpoint Access and Endpoint Egress are not priced or ranked.

When one user request contains multiple disjoint round-trip date alternatives, replace the template's two top-level departure windows with one `travel_windows` array. Each item owns its outbound window, return window, and optional stay range. Keep all alternatives in one Search Mission so they share Gateway discovery and one Search Effort budget.

Ask one focused question when a missing origin or destination region, outbound or return boundary, Base Currency, adult count, baggage policy, overnight policy, self-transfer policy, or hard constraint would materially change the search.

`airport_transit_unknown_policy: "allow_with_warning"` keeps an explicitly unverified candidate; it never establishes that the adult traveler can legally or practically transit. Disclose the unknown status and do not describe it as verified transit eligibility.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow the Product Kernel's gateway, portfolio, and browser briefs as the active search contract, submit item-bound Receipts, and continue through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

For a new user search, start a fresh Run and do not inspect or reuse another chat's Run, old Receipt, Session, result, or price. Resume only a Run created earlier in the current task, unless the user explicitly names an older Run to continue.

For every fare-bearing flight search, use the authorized Browser to establish the lowest compatible result through the Provider's lowest-price view, ascending price sort, or comparison of all visible results. Record the generic `price_selection_method`; default recommended ordering and an unopened lower-price label cannot support a lowest-price claim.

When the user explicitly requests a whole country, set that side's optional `search_geography` to the declared country; omit it for a city or bounded region. For `region_pool_fare_screen`, choose a public Browser Provider whose visible controls can express the Work Item's required capabilities. Prefer its native country, region, multi-airport, and flexible-date search before airport rectangles. Return only `fare_found`, `no_result`, or `unsupported_view` in the compact Capture shape. Never copy Node metadata, internal IDs, scope, or coverage conclusions. The Product Kernel immediately exact-verifies the first competitive fare and treats fixed airport rectangles only as a lazy fallback.

## Deliver

After the Product Kernel returns a `completed` Outcome, run `python <launcher_ref> --runs-root runs deliver --run-id <run-id>`. The default presentation view is the user-delivery Interface; use `--view full` only for audit. Present only a `kind=deliverable` Result and report every naturally produced `candidate_slate.entries` item in rank order. Rank one is always the lowest verified **Mandatory Air Ticket Total** inside the disclosed scope and Research Credit budget; a soft city or convenience preference may describe a more expensive alternative but may never reorder it. State the price observation time or freshness deadline, the Self-access Gateway Pool, excluded Endpoint Access/Egress, Internal Surface Transfer and accommodation costs, and every feasibility warning. Never imply a door-to-door or global-minimum claim. Never construct the user answer from `product/session.json`, a sidecar Browser capture, the private raw Solver Result, truncated JSON, or browser memory. A `delivery_blocked` response means the Search Mission is not deliverable: resume its required work, repair the final Result, or refresh expired rank-one price Evidence as instructed. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
