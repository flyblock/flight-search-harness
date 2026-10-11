# Provider and browser brief contract v0.4.0

## Authority

The AI owns gateway, route, date, portfolio, and seller decisions. The shared fixed-route rule owns one narrow invariant: when Google Flights supports the Work Item shape, the first flight attempt uses it. After that attempt, the brief carries at most three non-binding source hints rather than a scored catalog. One Research Question remains one Work Item; source hints never create sibling Work Items.

The source catalog is adapted from the one-way Product Kernel's broad Provider model. Round-trip execution keeps the catalog breadth and dynamic official-source rules, but scores them against round-trip Work Item families rather than copying one-way corridor Work Items verbatim.

Registry file order has no ranking meaning. The Harness brief carries the binding first source separately from at most three later hints and may use another suitable source when needed.

## Progressive disclosure

1. For a non-exact region, render AI-only `region_scope_definition` with no Provider assignment.
2. Persist the bounded proposal and compile one Provider Work Item for each candidate airport.
3. Render the compact catalog and select one regional-scope, pool-screen, or complete-journey Work Item.
4. Render only the selected Work Item's executable query and Evidence contract.
5. Operate the browser, adapt visible observations, and ingest them transactionally.
6. Continue the active hypothesis until complete, hard-dead, deferred-low-value, or pause.

## Gateway Work Items

`region_scope_definition` is a semantic-judgment Work Item, not Browser Evidence, and has no assigned Provider. It returns candidates, roles, inclusion reasons, seed exclusions, and a scope reason after checking the published gateway categories. No production mapping supplies the airport list.

After both scopes are defined, one `region_pool_fare_screen` compares the complete admitted airport pools, date windows, and stay range. Its Receipt preserves the submitted scope and concrete pair/date totals. Sampled dates cannot close it, and its signals remain research-only until a payable Offer is verified.

Do not:

- replace a city anchor with the nearest airport before research;
- copy airports from static examples or earlier Runs;
- turn topology or a calendar hint into a priced offer;
- enumerate every world airport without a concrete signal.

Typical Work Item fit is deliberately different by context:

- one-way and round-trip flight discovery starts with Google Flights through the shared Provider policy;
- after the required first attempt, broad global airport/date challengers may include Kiwi, Skyscanner, or KAYAK when the Work Item remains unresolved;
- topology-only sources such as FlightConnections, Rome2Rio, OSM, or Transitland remain research-only;
- a direct carrier becomes actionable when its market or an identified operator matches.

## Priced Work Items

Each priced Work Item carries its own query shape. It may request a joint round trip, a one-way, an indivisible multi-city or nested ticket, or exact surface transport.

- Preserve a joint or multi-segment seller total as one atomic offer.
- Preserve every ticketed movement under that offer.
- For one-way Evidence, use the Work Item's explicit coverage endpoints and direction.
- For newly observed movement endpoints, include RegionNode records in the same observation.
- Surface research from a topology source becomes payable evidence only after the actual operator or seller is visible with exact schedule and price.

Joint, split, and nested research do not share one fixed source order:

- joint quote/date-grid Work Items normally favor sources with strong atomic round-trip and calendar support, such as Google Flights;
- split directions normally favor sources with low-cost, multi-airport, payable, or independent-combination strength, such as Kiwi or Trip.com;
- nested/mixed atomic Work Items favor multi-city, fare-construction, virtual-interline, and payable OTA capabilities;
- surface Work Items favor the applicable payable operator or regional aggregator, with research sources used only to identify that operator.

After one source attempt, an incomplete-coverage bundle keeps the same Work Item pending. The next brief applies a repeat penalty to that source and a novelty bonus to useful untried alternatives. This is cross-source research inside one Research Question, not Work Item fan-out.

## Evidence roles

- `payable_offer`: may provide verified evidence when all required fields are visible.
- `payable_offer_when_seller_visible`: admissible only with an exact seller or booking option and mandatory total.
- `research_only_until_payable_seller_verified`: may create gateway or fare leads but cannot directly become a verified payable offer.

Every observation records the actual `source_id`, Provider, URL, and offset-aware observation time. Research provenance remains separate from the later payable seller identity.

## Terminal observations

`no_result` and `rejected` identify the exact Work Item scope, source, URL, observation time, and non-empty reason. They never add a node, movement, Offer, or price.
