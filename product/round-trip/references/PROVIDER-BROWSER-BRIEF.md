# Provider and browser brief contract v0.4.0

## Authority

The AI owns gateway, route, date, portfolio, seller, challenger, and stopping decisions. The shared Provider policy for the one-way and round-trip Product Kernels owns one narrow invariant: the first flight attempt for a Work Item uses Google Flights at base score 95 and AZair is excluded. After that attempt, the registry remains a capability catalog rather than a whitelist or exhaustive scheduler. One Research Question remains one Work Item; source options never create sibling Work Items.

The source catalog is adapted from the one-way Product Kernel's broad Provider model. Round-trip execution keeps the catalog breadth and dynamic official-source rules, but scores them against round-trip Work Item families rather than copying one-way corridor Work Items verbatim.

Registry file order has no ranking meaning. `round_trip_provider_advisor.py` derives post-first-attempt suggestions from Work Item family, geography, transport mode, flexible-date and multi-airport needs, independent-combination capability, payable/research role, and source attempts already recorded in the Session. The Harness brief carries the binding first source separately from later `ai-choice` suggestions.

## Progressive disclosure

1. For a non-exact region, render AI-only `region_scope_definition` with no Provider assignment.
2. Persist the primary proposal, then render independent `region_scope_challenge` against omitted airport categories.
3. Merge both AI proposals and compile one Provider Work Item for each airport in their union.
4. Render the compact catalog and select one gateway or complete-journey hypothesis.
5. Render only the selected Work Item's executable query and Evidence contract.
6. Operate the browser, adapt visible observations, and ingest them transactionally.
7. After an incumbent, run one bounded AI-only structural-definition Work Item and resolve every proposal.
8. Continue the active hypothesis until complete, hard-dead, deferred-low-value, or pause.

## Gateway Work Items

`region_scope_definition` and `region_scope_challenge` are semantic-judgment Work Items, not Browser Evidence. Neither has an assigned Provider. The first returns candidates, roles, inclusion reasons, seed exclusions, and a scope reason. The second must review every published category and add non-core candidates or give a category-complete explanation. No production mapping supplies either airport list. Their union becomes separate `gateway_discovery` Work Items, so recovery and coverage are node-specific.

Gateway discovery starts from city/region anchors and pool constraints. A broad map, regional airport search, scheduled transport source, or concrete fare anomaly may create `research_observed` gateway candidates.

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
