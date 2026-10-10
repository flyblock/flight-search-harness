# One-way Product Kernel workflow

Use this reference for one-way product decisions. The resolver's `mission_runtime_ref` provides the shared Search Mission lifecycle, Receipt format, recovery procedure, and Outcome handling.

## Inputs and constraints

Capture the origin and destination `anchor_nodes` and optional `seed_gateway_nodes`, `departure_window`, traveler count, accepted modes, Base Currency, `fx_to_base`, the shared baggage policy, overnight permission, self-transfer policy, transport entitlements, and any hard budget, duration, connection, or accessibility constraints. Any non-Base-Currency rate or money value requires a matching source-attributed `fx_snapshot` that is current at Run start and completion.

When `endpoint_policy` is `anchor_required`, the Product Kernel publishes explicit priced access and egress Research Questions between accepted city anchors and seed gateways. An airport-to-airport fare alone cannot satisfy the complete-journey baseline.

Use `null` for an unbounded budget or regional ground-cost ceiling; do not substitute a large monetary sentinel.

Map date flexibility to `exact_dates`, `bounded_window`, or `fully_flexible` without expanding `departure_window`. Include unavoidable intermediate lodging unless the user explicitly excludes a discretionary stay. Apply an entitlement only to a matching observed surface Offer while preserving the original amount and duration.

## Search decisions

- A complete origin-pool-to-destination-pool quote is the first obligation. Partial suffixes, reverse predecessors, and frontier branches may inform later challengers but do not satisfy product acceptance.
- The one-way Provider foundation excludes AZair. Google Flights is the binding first flight attempt only when a verified query shape represents the Work Item's actual expanding scope. Otherwise begin with a capability-matched Provider instead of narrowing the question.
- Preserve every technical scope rejection. Reopen the same Work Item for an untried, capability-matched Provider rather than replacing the Run or converting a technical rejection into a market no-result.
- After a complete incumbent exists, investigate materially different, Evidence-backed challengers across useful dates, Providers, modes, and corridors. Frontier choices are non-binding research decisions derived from admitted Evidence; they never prove an unknown connection.
- Select a published `action_key` for each `choice` Work Item and give a concrete reason. Route edges, Provider assignments, and completion state come from the active Product Kernel contract.

## Evidence

A priced movement needs current visible Evidence with exact endpoints, offset-aware schedule, mandatory total, currency, seller or operator, URL, observation time, baggage terms, and service identity. Preserve an indivisible multi-leg ticket as one atomic Offer and never invent component prices.

Calendar views, topology, historical schedules, estimates, and hypotheses remain research-only Observations. Surface movement requires its own exact operator or seller Evidence. Strict live multi-currency comparison requires explicit current FX Evidence.

For planned exit at a ticketed connection, preserve the complete ticketed path, planned exit, unused segments, baggage restrictions, and full atomic price. Treat a connection as protected only when payable Evidence establishes that protection.

## Completion

Completion requires a verified end-to-end route that passes hard constraints, terminal dispositions for every selected hypothesis, an explanation for pending work, and a fresh Product Kernel stop assessment after the latest Evidence or hypothesis change. A complete candidate, a saved draft, or Harness recovery does not complete the Run.

Report the selected route first with chronological movements, waits, mandatory total, ticket boundaries, sellers or operators, Evidence freshness, and materially different verified alternatives when useful. Describe it as the best verified route within the Evidence and constraints actually investigated, not a global lowest price.
