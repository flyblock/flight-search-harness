# Round-trip Product Kernel workflow

Use this reference for round-trip product decisions. The resolver's `mission_runtime_ref` provides the shared Search Mission lifecycle, Receipt format, recovery procedure, and Outcome handling.

## Inputs and defaults

Capture the origin and destination `anchor_nodes` and optional `seed_gateway_nodes`, `outbound_departure_window`, `return_departure_window`, adult count, accepted modes, Base Currency, the shared baggage policy, overnight and self-transfer policies, and any hard budget, duration, destination-stay, connection, or accessibility constraints. Request Provider prices in the Base Currency; the Harness owns any system FX normalization needed for other observed currencies.

Use the complete declared departure windows unless explicit exact-date arrays narrow a direction. Apply minimum or maximum stay limits only when the user supplies them. Include unavoidable intermediate lodging unless the user explicitly excludes a discretionary stay.

## Search decisions

- For each non-exact regional side, complete one bounded semantic `region_scope_definition` that checks primary, nearby-city, surface-connected, cross-border, and specialized airports. The admitted proposal, not a static airport map or the request seeds alone, defines the bounded gateway scope.
- After both regional scopes are defined, execute one `region_pool_fare_screen` over the complete admitted origin and destination airport pools, departure windows, and stay range. Use a Provider-native pool search when available; otherwise partition the same Work Item without creating airport-pair Work Items.
- A sampled date pair is ranking evidence only and can never close the pool screen. Completion requires the exact submitted scope plus `date_coverage: "bounded_window"`, actual airports used in both directions, concrete outbound and return dates, and the observed atomic total.
- The round-trip Provider foundation excludes AZair. Google Flights is the binding first flight attempt only when a verified query shape represents the Work Item's actual scope. Otherwise begin with a capability-matched Provider.
- A price-minimization Work Item must use the Provider's lowest-price view, ascending price sort, price-sorted API, or comparison of all visible results. Record `price_selection_method`; neither default recommended ordering nor an unopened lower-price label may be submitted as the lowest fare.
- Keep every payable ticket atomic. For an ordinary closed pair, research the joint round-trip quote first as the observed baseline. Verify both split directions only when current Evidence indicates their combined total may improve on the incumbent. The Product Kernel may also publish Evidence-backed open-jaw, nested, multi-city, mixed, or surface-supported alternatives.
- Keep round-trip frontier state product-local. Investigate a different portfolio structure only when current Evidence exposes a concrete reason it may beat the incumbent.
- Flexible-date calendar or grid signals are research-only. Choose a hard-valid date pair through a published temporal action, then verify a concrete payable Offer on the same Work Item.

## Evidence

A priced movement needs current visible Evidence with exact endpoints, offset-aware schedule, service identity, seller or operator, URL, observation time, baggage terms, mandatory amount for the declared traveler party, and currency. Joint, multi-city, and nested ticket totals are indivisible; never invent directional or component prices.

Keep physical movements separate from atomic Offers and count every selected Offer once. Surface legs require their own operator or seller Evidence. A short same-ticket connection is protected only when payable Evidence establishes a protected through-ticket; shared pricing, matching carrier names, or the absence of a warning is not enough.

Research-only, topology, calendar, historical, estimated, unknown, no-result, rejected, or expired Observations cannot become current priced movements. The Harness owns Base Currency normalization for non-Base-Currency Observations.

## Completion

Completion requires the bounded regional-scope pass when applicable, a completed region-pool fare screen, terminal gateway hypotheses, a verified complete portfolio, coverage of its mandatory atomic questions, no unexplained pending work, and no unverified screen candidate whose observed total can still beat the incumbent.

Report the winning city-to-city portfolio first, followed by chronological movements, waits, atomic ticket boundaries, sellers, surface access or egress, entitlements, mandatory totals, baggage, elapsed time, and materially different verified alternatives. Disclose unsearched gateway, date, Provider, and portfolio scope, and describe the result as the best verified portfolio within that scope.
