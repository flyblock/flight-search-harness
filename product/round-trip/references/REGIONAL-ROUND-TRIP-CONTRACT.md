# Regional round-trip contract

## Product Interface

The round-trip Product Kernel accepts a selected destination and produces the lowest verified Mandatory Air Ticket Total found inside a disclosed search scope and Research Credit budget. It owns an independent Session, Evidence ledger, portfolio solver, price-challenge frontier, and finalization gate behind the Product Adapter Seam.

A regional request does not promise a door-to-door total. The AI defines a Self-access Gateway Pool; the user arranges and pays for Endpoint Access and Endpoint Egress. An exact airport request may retain `anchor_required` for compatibility, but new regional requests use `any_admitted_gateway`.

## Search lifecycle

```text
user Anchors and constraints
-> AI-defined Self-access Gateway Pools
-> Provider-ranked regional fare screen
-> exact atomic flight verification
-> unified price-challenge frontier
-> rank-one refresh
-> bounded Outcome and Candidate Slate projection
```

The Self-access Gateway Pool has no numeric airport cap. The AI considers primary, secondary, low-cost, nearby-city, surface-connected, and reasonable cross-border Gateways in one semantic judgment. Seeds are hints, not an exhaustive list. Defining the pool does not query or price ground transport.

The Product Kernel first uses Provider-native public Browser controls for a user-declared country or region. It records one compact fare signal and immediately exact-verifies the first competitive candidate. Airport rectangles and date tiles are lazy fallbacks only when the broad shape is unsupported. Each real Browser query consumes one Research Credit; the Worker never declares coverage. Multiple disjoint date alternatives belong to one Search Mission and share Gateway discovery and one Research Credit ledger.

## Mandatory Air Ticket Total

Default ranking includes only:

- every payable flight Offer after the first selected Gateway;
- mandatory air-positioning and self-transfer tickets;
- required baggage; and
- mandatory airline fees.

Endpoint Access, Endpoint Egress, Internal Surface Transfer prices, accommodation, meals, and other non-air costs are excluded and disclosed. `gateway_complete_cost`, `endpoint_completion_cost`, and `anchor_complete_cost` remain optional audit values and are not synonyms for the ranking total.

A joint round-trip price is atomic and counted once. Split one-way, nested round-trip, open-jaw, and mixed atomic structures remain explicit. The Product Kernel derives Journey Alternative, Ticketing Structure, Seller Variant, and Offer Snapshot identity; the Worker reports source-visible facts.

Accommodation does not affect ranking and creates no Work Item. New Sessions use `accommodation_cost_policy=exclude_from_ranking`. The legacy `include_required` value and already persisted amounts remain readable for old Runs.

## Unified price challenge

All remaining candidates use one price-challenge frontier. New Runs do not generate `regional_challenger_screen` or the legacy fixed `portfolio_frontier_screen`. There is no required remote phase, hub count, airport count, challenger round count, or Candidate Slate quota.

The active Price Challenge Threshold compares a candidate's optimistic Mandatory Air Ticket Total with the current verified winner. An outside-pool hub must provide:

1. the long-haul airfare lower bound; and
2. a source-visible airfare lower bound for every mandatory positioning flight.

The Product Kernel sums those flight amounts before applying the threshold. A missing positioning-airfare bound cannot establish a competitive complete lower bound. Ground transport and accommodation never enter this calculation.

The current Worker Capture contains only the candidate airport node, long-haul airfare, aggregate positioning-airfare lower bound, destination Gateway, and itinerary dates. Open jaws add only the different return origin or return-hub node. Candidate IDs, signal IDs, hub-cell IDs, screened-hub collections, and distinct-cell bookkeeping are not part of the current Worker Interface; the Product Adapter derives internal identity. Those fields remain accepted only when reading a legacy `portfolio_frontier_screen` artifact.

A competitive signal may become a nested or mixed atomic hypothesis. Exact long-haul verification precedes positioning Work Items so their date windows, `must_arrive_by`, `not_before`, and self-transfer buffers derive from observed flight times. All positioning flights are priced and included in Mandatory Air Ticket Total.

## Search Effort and Research Credits

Search Effort selects Product-owned policy:

| Search Effort | Research Credits | Price Challenge Threshold |
| --- | ---: | ---: |
| `economy` | 10 | 10% |
| `standard` | 30 | 5% |
| `deep` | 100 | 1% |

A new Browser research Work Item costs one credit, exact browser quote verification costs two, and an Internal Surface Transfer check costs one. The Product Kernel reserves credits for a published Work Item and commits them when a real Browser interaction occurs, even if the resulting Receipt is blocked or needs representation correction. A blocked Work Item with no Browser interaction releases its reservation. Evidence replay, non-Provider work, and representation repair cost zero and must not repeat Provider research. Work Item identity makes reserve and commit idempotent.

There is no wall-clock limit. A batch may not reserve more credits than remain.

## Internal Surface Transfer

A discontinuity between flight Movements inside the Core Itinerary creates one `internal_surface_transfer` Work Item. The Worker performs one external route search and returns only:

- `from_node`;
- `to_node`;
- `approximate_duration_minutes`;
- `feasibility` as `verified_route`, `fallback_plausible`, or `infeasible`;
- one `evidence_url`; and
- `observed_at`.

This record is Evidence, not an Offer. It has no Seller, source ID, price, or exact purchasable timetable. A missing public-transport result may be `fallback_plausible` when taxi, rideshare, hire car, or another physical road route fits the available window. Explicit infeasibility or approximate duration plus required buffers exceeding the window rejects the Journey Alternative.

## Evidence and Worker Interface

A priced flight Observation carries exact endpoints, offset-aware Movements, service identity, Seller, booking or comparison URL, observation time, baggage terms, Base Currency-compatible price, and a terminal Seller-list witness. Research signals never become Offers.

The Harness derives protocol identity, source identity where resolvable, Research Credit accounting, completion, and stopping. The Worker must not repeat a Provider query to repair Receipt representation. The compact correction turn preserves captured facts and uses `execution_origin=evidence_replay`.

Legacy `portfolio_frontier_screen`, priced Endpoint Surface Work Items, and `max_candidates` artifacts remain parseable in persisted Sessions. No new Session generates them.

## Solver and freshness

The solver enumerates chronological, Gateway-complete combinations of admitted atomic flight Offers. Every price is counted once. It rejects expired Evidence, missing required baggage, forbidden self-transfer, insufficient buffers, impossible Internal Surface Transfers, invalid stay length, and hard duration or budget violations.

Offer Snapshots remain immutable. Before delivery, only rank one's stale selected Offers are refreshed on the same source, dates, endpoints, and Ticketing Structure. Sibling Sellers remain Seller Variants rather than false Journey Alternatives. Optional Candidate Slate entries keep independent freshness and do not block a current winner.

## Completion and delivery

The Product Kernel may complete when:

- at least one verified complete Journey Alternative exists;
- rank-one flight prices are current;
- every required Internal Surface Transfer has external Evidence;
- no affordable remaining candidate clears the Price Challenge Threshold, or Research Credits cannot fund the next useful investigation;
- no unresolved solver-projection exclusion can invalidate the winner; and
- active Work Items and hypotheses have a Product-owned terminal disposition.

Research Credit exhaustion without a verified winner returns `draft`.

Candidate Slate v2 projects all distinct complete Journey Alternatives naturally produced while challenging the winner. It has no target count and never creates Work Items.

Delivery reports the lowest verified Mandatory Air Ticket Total within the disclosed scope and budget, Search Effort, credits used and remaining, threshold, Self-access Gateway Pools, excluded costs, unsearched partitions, unresolved low-price signals, Internal Surface Transfer Evidence, booking links, freshness, and feasibility warnings. It never claims a global minimum or door-to-door price.
