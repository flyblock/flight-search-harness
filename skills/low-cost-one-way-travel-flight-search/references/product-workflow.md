# One-way Product Kernel workflow

All live Provider research operates public user-visible Provider pages through the host's authorized Browser or computer-use capability.

Use this reference for One-way product decisions. The resolver's `mission_runtime_ref` owns the shared Search Mission lifecycle and Receipt mechanics.

## Scope and ranking

For a city or region, the AI defines a finite Self-access Gateway Pool without a numeric airport limit. Use `any_admitted_gateway`; the user arranges travel to the origin Gateway and from the destination Gateway. Exact airport requests use `anchor_required` with that airport as Anchor and Gateway. Current Runs do not price or publish Endpoint Access or Endpoint Egress.

Rank complete feasible Journey Alternatives only by **Mandatory Air Ticket Total**: every required flight Offer, including air positioning and self-transfer flights, plus mandatory baggage and airline fees. Surface transport, Internal Surface Transfer prices, accommodation, and other non-air costs never enter ranking, budget pruning, or price challenges. `gateway_complete_cost` is only an audit value when every included cost is actually known; it is incomplete when an Internal Surface Transfer is unpriced. `total_trip_cost` is optional observed-cost audit data, not a ranking field.

Use `standard` Search Effort by default. Its Product-owned Research Credit budget and Price Challenge Threshold control stopping. Do not add a wall-clock, airport-count, remote-airport, candidate-count, or Slate-size quota. A user-supplied budget, duration limit, transfer-count limit, `overnight_allowed=false`, or `self_transfer_policy=never` is hard; otherwise long waits, overnight travel, and unprotected transfers are warnings.

## Research

1. Establish the first verified complete flight candidate.
2. Continue only with an airfare candidate whose optimistic Mandatory Air Ticket Total can clear the configured percentage Price Challenge Threshold and whose Work Item fits remaining Research Credits.
3. Do not spend Work on Pareto comfort, structural diversity, a fixed number of alternatives, or a separate remote-airport phase. Alternatives are natural by-products of pursuing the price winner.
4. Charge credits only for real new Provider research: structured research costs 1, an exact payable quote costs 2, and an Internal Surface Transfer check costs 1. Representation correction, Evidence replay, and non-Provider work cost 0 and must not repeat a query.

The Provider foundation may require a named first source when it supports the actual scope. Before capturing a fare, establish the lowest compatible result using a lowest-price view, ascending order, price-sorted interface, or comparison of all visible results. Calendar and topology signals remain research-only until an exact dated payable Offer is observed.

## Evidence and transfers

A flight Capture needs business facts: endpoints, offset-aware schedule, payable price and currency, mandatory baggage, carrier or operator, flight identity when displayed, Seller or Provider page, booking URL, and observation time. The Adapter or Product Kernel derives bundle, observation, edge, itinerary, Offer, and Receipt identities. Preserve indivisible multi-leg pricing as one atomic Offer; never invent component prices.

When two required flight endpoints differ, issue at most one external Internal Surface Transfer check at a time. Capture only `from_node`, `to_node`, approximate duration, feasibility, one Evidence URL, and observation time. This is a non-Offer feasibility bridge, never a zero-price TravelEdge. A verified route or plausible taxi/road fallback may connect the flights. Explicit infeasibility or duration plus buffers exceeding the available window rejects that combination. Do not research a Surface price.

## Completion and delivery

Research Credit exhaustion with a fresh verified winner is bounded completion. Exhaustion without a winner returns `draft`. No manual stop assessment or target alternative count is required.

Deliver the cheapest verified Mandatory Air Ticket Total first and state the bounded claim. Include the Self-access Gateway Pool, excluded costs, Search Effort, credits used and remaining, threshold, unsearched and unresolved scope, Internal Surface Transfer Evidence, selected Sellers and booking links, freshness, and warnings. Do not claim a global minimum.
