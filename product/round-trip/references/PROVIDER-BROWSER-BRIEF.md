# Provider and browser brief contract v0.4.0

## Authority

The Product Kernel owns Evidence admission, itinerary identity, search scope, price comparison, ranking, and completion. The Worker executes each published Work Item in the authorized Browser and reports only source-visible facts. Provider hints are non-binding; Worker-authored booleans never prove coverage.

The source catalog is adapted from the one-way Product Kernel's broad Provider model. Round-trip execution keeps the catalog breadth and dynamic official-source rules, but scores them against round-trip Work Item families rather than copying one-way corridor Work Items verbatim.

Registry file order has no ranking meaning. The brief carries at most three non-binding public-page hints and may use another suitable Browser source.

## Progressive disclosure

1. For a non-exact region, render AI-only `region_scope_definition` with no Provider assignment.
2. Persist the bounded proposals and publish the broadest Provider-native Browser query that matches the user-declared geography.
3. Render the compact catalog and select one regional-scope, pool-screen, or complete-journey Work Item.
4. Render only the selected Work Item's executable query and Evidence contract.
5. Operate the browser, adapt visible observations, and ingest them transactionally.
6. Continue the active hypothesis until complete, hard-dead, deferred-low-value, or pause.

## Gateway Work Items

`region_scope_definition` is a semantic-judgment Work Item, not Browser Evidence, and has no assigned Provider. It returns candidates, roles, inclusion reasons, seed exclusions, and a scope reason after checking the published gateway categories. No production mapping supplies the airport list.

After both scopes are defined, `region_pool_fare_screen` first attempts the user-declared country or regional geography with Provider-native public Browser controls. The Worker returns one compact `fare_found`, `no_result`, or `unsupported_view` Capture. The Product Adapter derives identities and Evidence fields; a fare immediately triggers exact verification and never claims complete coverage. An unsupported broad query is lazily reduced to airport rectangles without asking the Worker to repair representation.

Do not:

- replace a city anchor with the nearest airport before research;
- copy airports from static examples or earlier Runs;
- turn topology or a calendar hint into a priced offer;
- enumerate every world airport without a concrete signal.

Typical Work Item fit depends on visible Browser controls. Prefer a source that can express the full country, region, airport set, and flexible dates. Fixed airport rectangles are a fallback only when the broad shape is unsupported. Exact priced research requires complete itinerary, Seller, total, baggage terms, and booking-link Evidence. Topology-only sources remain research-only, while a direct carrier becomes actionable when its market or an identified operator matches.

## Priced Work Items

Each priced Work Item carries its own query shape. It may request a joint round trip, a one-way, an indivisible multi-city or nested ticket, or exact surface transport.

- Preserve a joint or multi-segment seller total as one atomic offer.
- Preserve every ticketed movement under that offer.
- For an exact flight itinerary, submit one Movement set and every visible Seller Offer together with the published Seller-list terminal witness. The Product Kernel selects the lowest compatible Seller; the Worker never submits the winner.
- A joint temporal signal uses Temporal Signal v2 with distinct outbound-departure, destination-arrival, and return-departure Local Dates. The Worker never submits Destination Stay Nights.
- For one-way Evidence, use the Work Item's explicit coverage endpoints and direction.
- For newly observed movement endpoints, include RegionNode records in the same observation.
- Surface research from a topology source becomes payable evidence only after the actual operator or seller is visible with exact schedule and price.

Joint, split, and nested research do not share one fixed source order:

- joint quote/date-grid Work Items normally favor sources with strong atomic round-trip and calendar support, such as Google Flights;
- split directions favor a Browser source that exposes the required low-cost, multi-airport, payable, or independent-combination facts;
- nested/mixed atomic Work Items favor multi-city, fare-construction, virtual-interline, and payable OTA capabilities;
- surface Work Items favor the applicable payable operator or regional aggregator, with research sources used only to identify that operator.

An incomplete Seller listing keeps exact verification pending. Every real Browser query is independently chargeable, but the Product Kernel publishes only the next useful residual query and never pre-materializes the whole airport-by-date tree.

## Evidence roles

- `payable_offer`: may provide verified evidence when all required fields are visible.
- `payable_offer_when_seller_visible`: admissible only with an exact seller or booking option and mandatory total.
- `research_only_until_payable_seller_verified`: may create gateway or fare leads but cannot directly become a verified payable offer.

Every observation records the actual Provider URL and offset-aware observation time. The Product Adapter resolves a unique registered `source_id` from the canonical or explicitly allowed booking host when possible; the Worker must not replace an exact quote URL with a weaker registry entry URL. Research provenance remains separate from the later payable seller identity.

## Terminal observations

`no_result` and `rejected` identify the exact Work Item scope, source, URL, observation time, and non-empty reason. They never add a node, movement, Offer, or price. A priced-offer attempt may preserve terminal Observations alongside at least one `verified_observed` Observation from the same bounded research action; the Product Adapter derives the attempt disposition from the verified Offer and retains the terminal facts.
