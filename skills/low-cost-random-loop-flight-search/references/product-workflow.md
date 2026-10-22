# Random-loop Product Kernel workflow

Use this reference for random-loop product decisions. The resolver's `mission_runtime_ref` provides the shared Search Mission lifecycle, Receipt format, recovery procedure, and Outcome handling.

## Required inputs

Capture these values explicitly before starting the Search Mission:

- departure and return airport pools;
- `first_departure_window` with offset-aware boundaries;
- adult count, baggage policy, overnight permission, and self-transfer policy;
- minimum and maximum flight-leg counts;
- connection buffer, maximum connection wait, and `maximum_total_duration_minutes`;
- Base Currency; request Provider prices in it and let the Harness own any required system FX normalization;
- complete-loop distance-per-Base-Currency-unit floor and optional higher target; and
- an optional exclusive maximum per leg after FX normalization.

Keep the destination open throughout a random-loop search.

## Search decisions

- Search the entire issued departure-airport and date scope toward `Anywhere`; a narrow airport pair or partial date cell does not close its parent scope.
- Follow the Product Kernel's ordered depth-first branch and Provider instructions. Preserve unvisited siblings and backtrack only after the active subtree is a hard dead end.
- When a Work Item contains `required_source_batch`, execute its Providers in the published order and stop when its disposition rules require it. Provider choice is not an override point.
- If a Provider can represent only part of an airport or date scope, return exact, non-overlapping `scope_partitions`. Search the supported remainder first with `source_disposition: "retry_current"`; advance only the unsupported remainder with `source_disposition: "advance_source"`.
- Use `source_unavailable` and `scope_unsupported` only for technical inability to execute the whole issued scope. Use `no_market_result` for an executed market miss, and use `scope_incomplete` or `research_only` when the question remains unresolved.
- Submit every field-complete Offer visible in the executed scope so the Product Kernel owns admission and ranking.
- When a Provider separates recommended results from price ordering, use its Browser-visible lowest-price view or ascending price order for fare capture. A label advertising a lower fare is only a lead until the corresponding result is opened and inspected.

## Evidence

A priced flight Offer needs exact IATA endpoints, offset-aware departure and arrival times, the mandatory total amount and currency for the requested traveler count and baggage policy, flight identity, result and booking URLs, observation time, baggage terms, and the distance evidence requested by the Work Item. Calendar estimates, maps, topology, historical data, and inference remain research-only Observations.

Each executed Provider group must preserve its `request_id`, `source_id`, exact `submitted_scope`, `coverage_complete`, structured `acquisition`, source URL, and offset-aware observation time.

## Completion

A `route_found` result requires a rebuilt complete loop that passes every hard constraint and the configured quality floor. A `bounded_no_result` requires both an exhausted ordered search tree and complete persisted coverage for every scope partition. Partial coverage, a technical failure, a query budget, or a promising partial route cannot complete the Run.

Report the verified loop legs, total cost and efficiency, Evidence freshness, Provider query use, and any explicitly unsearched scope. Describe the result as the best verified loop within that scope.
