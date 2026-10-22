# Round-trip Product Kernel workflow

Use this reference for round-trip product decisions. The resolver's `mission_runtime_ref` provides the shared Search Mission lifecycle, Receipt recovery, and Outcome handling.

## Inputs and defaults

Capture the origin and destination Anchors, optional seed Gateways, one or more round-trip date alternatives, adult count, Base Currency, baggage requirements, overnight and self-transfer policies, and hard date, duration, stay, connection, or accessibility constraints. Multiple alternatives use `travel_windows` inside one Search Mission and share one Search Effort budget. Use `search_effort: "standard"` unless the user explicitly requests `economy` or `deep`.

For a city or regional request, use `endpoint_policy: "any_admitted_gateway"`. The user arranges Endpoint Access and Endpoint Egress for the disclosed Self-access Gateway Pool; those movements are not priced and do not enter ranking. Reserve `anchor_required` for an exact airport endpoint explicitly requested by the user. New searches rank by Mandatory Air Ticket Total, not Gateway-complete Cost, Anchor-complete Cost, or a door-to-door estimate.

Use the complete departure windows unless exact-date arrays narrow them. Minimum and maximum Destination Stay Nights apply only when supplied. Accommodation is excluded from ranking and does not create research work.

## Search decisions

- For each non-exact side, complete one semantic `region_scope_definition`. The AI must consider primary, secondary, low-cost, nearby-city, surface-connected, and reasonable cross-border airports. The Self-access Gateway Pool has no numeric airport cap. Do not query ground fares while defining it.
- For a whole-country request, the Product Kernel first publishes one Provider-native discovery Work Item with the required visible Browser capabilities. Airport rectangles are a lazy fallback only when no public page can express the native scope.
- A pool Capture reports only `fare_found`, `no_result`, or `unsupported_view`. The Product Adapter derives identities and Evidence fields. The first competitive fare is exact-verified immediately; a Capture never claims whole-scope coverage.
- Exact flight verification costs two Research Credits. Keep every payable ticket atomic. All required flights after the first selected Gateway count toward Mandatory Air Ticket Total, including positioning and self-transfer flights, required baggage, and mandatory airline fees.
- All remaining candidates use one price-challenge frontier. There is no regional-challenger quota or fixed remote-airport phase. Continue only when a candidate's optimistic Mandatory Air Ticket Total is below the current winner by the active Price Challenge Threshold.
- For a hub outside the Self-access Gateway Pool, the optimistic lower bound must include both the long-haul airfare and a source-visible lower bound for every mandatory positioning flight. Never add ground transport or accommodation to this bound. A missing positioning-airfare bound cannot establish a competitive complete lower bound.
- A current `price_challenge_screen` candidate Capture contains only the airport node, long-haul airfare, aggregate mandatory positioning-airfare lower bound, destination Gateway, and itinerary dates. Add return origin or return-hub node only for an open jaw. Do not invent candidate IDs, signal IDs, hub-cell IDs, screened-hub lists, or distinct-cell bookkeeping; the Product Adapter derives internal identity.
- Search Effort is Product-owned: `economy` uses 10 Research Credits and a 10% threshold, `standard` uses 30 and 5%, and `deep` uses 100 and 1%. Browser research costs one credit, exact quote verification two, and one Internal Surface Transfer route check one. Evidence replay, representation repair, and non-Provider work cost zero and must not repeat Provider research.
- Refresh only rank one's stale selected Offers. Revisit their exact source, dates, endpoints, and Ticketing Structure. Other Candidate Slate entries retain their own freshness and do not block delivery.

## Internal Surface Transfers

An airport or station discontinuity inside the Core Itinerary creates one `internal_surface_transfer` Work Item. Perform one external route search; never rely on model memory. Return only:

- `from_node` and `to_node`;
- `approximate_duration_minutes`;
- `feasibility` as `verified_route`, `fallback_plausible`, or `infeasible`;
- one `evidence_url`; and
- `observed_at`.

Do not return an Offer, Seller, source ID, fare, or exact purchasable timetable. Missing public transport does not disqualify a candidate when taxi, rideshare, hire car, or another plausible road movement fits the available window. Only explicit physical infeasibility or insufficient time rejects the Journey Alternative. The transfer price remains excluded and must be disclosed.

## Evidence and repair

A priced flight Observation needs exact endpoints, offset-aware schedule, service identity, Seller, booking or comparison URL, observation time, baggage terms, mandatory amount, currency, and a terminal Seller-list witness. Research signals never become Offers.

Follow the Work Item's compact attempt Schema and template. Observation, Offer, Receipt, Lease, and Research Credit identities belong to the Harness and Product Adapter. When admission requests a representation correction, preserve captured facts, mark the correction as Evidence replay, and do not query the Provider again.

## Completion and delivery

A verified winner may complete when no remaining candidate clears the Price Challenge Threshold or when the Research Credit budget cannot fund the next useful investigation. Exhaustion without a verified winner produces a `draft`, not a false result.

Candidate Slate v2 is a price-ranked projection of distinct complete Journey Alternatives already found. It has no target size and never creates Work Items. Seller Variants and alternate dates remain nested under their Journey Alternative.

Delivery must state:

- the lowest verified Mandatory Air Ticket Total within the disclosed scope and Research Credit budget;
- every included flight Offer and air positioning ticket;
- the Self-access Gateway Pool;
- excluded Endpoint Access, Endpoint Egress, Internal Surface Transfer, accommodation, and other non-air costs;
- Internal Surface Transfer duration, feasibility, and Evidence link;
- Search Effort, credits used and remaining, threshold, unsearched partitions, and unresolved low-price signals; and
- rank-one freshness and all material feasibility warnings.

Never claim a global minimum or a door-to-door total.
