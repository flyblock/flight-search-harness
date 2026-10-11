# How Flight Search Harness works

Flight Search Harness turns a natural-language request into a recoverable, evidence-backed search for a low-cost journey. A Skill selects one journey shape, the Harness coordinates the Search Mission, a Worker researches live Provider pages in an authorized Browser, and the selected Product Kernel decides what Evidence is admissible and when the Run is complete.

## Shared lifecycle

The Harness owns the shared lifecycle:

1. Materialize the user's hard constraints into a product request.
2. Create or resume one Search Mission and persist its Run in the Store.
3. Ask the selected Product Kernel for bounded Work Items.
4. Publish numbered Work Attempts in one claimed Lease, then let a uniquely
   identified Worker execute them against live Provider pages outside the Run
   lock.
5. Persist immutable item-bound Receipts before submitting them through the
   Product Adapter Seam.
6. Admit valid Observations and Offers as Evidence, then recompute deterministically.
7. Continue until the Harness materializes a `completed`, `draft`, `blocked`,
   or `failed` Outcome from the Product Kernel effect and execution result.

The Run is the only lifecycle authority. The subordinate Mission file contains
only an execution cursor, and the Lease file alone owns the active claim and
pending Work Attempts. A rejected Receipt retries the same Work Item with a new
`attempt_number`; it does not create a correction graph. Receipt admission is
idempotent across interruption windows, while events and metrics remain
optional sidecars.

The Product Kernel produces `need_work`, `draft`, `blocked`, or `complete`
effects. The Harness maps unrecoverable execution errors to terminal `failed`.
Only `draft` and `blocked` may be resumed. A completed Run points to the Product
Module's authoritative `product/final.json`; the Harness does not keep a second
full-result copy.

The Browser finds current facts. It does not decide whether an itinerary is complete, whether an Offer is admissible, or whether the search is finished. Those decisions remain inside the selected Product Kernel.

## What low-cost means

The three Product Kernels optimize different notions of value:

| Journey shape | Primary low-cost objective | Important competing constraints |
| --- | --- | --- |
| One-way | Lowest mandatory end-to-end cost among verified complete journeys | Time, hops, transfer feasibility, regional access and egress |
| Round trip | Lowest mandatory total cost of a verified outbound-and-return ticket portfolio | Stay length, joint versus split pricing, open jaws, surface segments and atomic ticket rules |
| Random loop | High verified flight distance per Base Currency unit inside the configured price and route constraints | Leg count, return feasibility, connection timing, date diversity and optional per-leg price cap |

The result is authoritative for the Evidence and hypotheses actually investigated. The Harness does not claim that every Provider, airport, date, or fare worldwide was searched.

## Random-loop search

Random-loop search discovers the destination sequence while preserving an airport-only return loop.

The Product Kernel ranks partial routes by cumulative distance divided by cumulative price in the user-selected Base Currency, while retaining configured date and geographic diversity. It explores one ordered depth-first branch at a time and keeps viable siblings for backtracking. Once the minimum route depth is reached, it can issue return-pool closure probes; on the final available leg it searches only for a valid return.

Every completed loop is rebuilt from admitted flight Evidence. The Product Kernel recalculates distance, mandatory price, connection timing, leg count, and complete-loop efficiency before the route can qualify. Calendar values and topology hints may guide discovery but cannot become flight edges.

This is a bounded search strategy over live, incrementally observed Offers. It is not a travelling-salesperson solver over a complete static graph.

## One-way search

One-way search treats the journey as an evidence-backed, resource-constrained path between regional anchors. Flights may be combined with verified rail, bus, ferry, transit, rideshare, walking, or other allowed surface segments.

The normal path is:

1. Establish a complete conventional baseline route and its mandatory total cost `U`.
2. Discover useful regional gateways and forward or backward frontier states from admitted Evidence.
3. Expand verified suffixes backward from the destination and calculate how much an unknown prefix may cost while still beating `U`.
4. Research only bounded Provider questions that may improve price or add a materially different Pareto alternative.
5. Recompute the complete route after every admitted Offer.

For a verified suffix from node `v` to the destination:

```text
price_cutoff = U - delta
remaining_budget(v) = U - delta - verified_suffix_cost(v)
```

The price-improvement lane searches for a strictly cheaper complete route. A separate Pareto lane may retain a slightly more expensive route when it is faster or uses fewer hops. Unknown prices, expired Offers, calendar estimates, and inferred connections never enter a route as zero-cost edges.

Flexible dates are scheduled per discovered corridor. A date signal from one corridor does not silently suppress another corridor's date window, and an exact dated Offer must still be verified before final comparison.

## Round-trip search

Round-trip search does not concatenate two independently selected one-way winners. Its unit of comparison is a complete outbound-and-return portfolio.

The Product Kernel first discovers regional gateways without replacing the user's real city or station anchors. It then creates complete-journey hypotheses and researches the atomic prices needed to compare them. A joint round-trip fare is observed and counted once. Split one-way tickets are researched independently. Nested round trips, open jaws, mixed atomic itineraries, and required surface access remain separate structures rather than invented component prices.

The portfolio solver accepts only chronological, endpoint-complete combinations of admitted atomic Offers. It includes every mandatory ticket, access, egress, required accommodation, baggage rule, FX conversion, and connection constraint that applies to the selected portfolio.

The first valid portfolio becomes an incumbent, not an automatic final answer. Remaining gateway, date, ticket-structure, and challenger obligations must reach terminal states before the Product Kernel can return a `completed` Outcome.

## Why there are three Product Kernels

The Harness shares coordination, recovery, Work Items, Receipts, Workers, and Store semantics. The search state does not belong in that shared layer.

| Product Kernel | State it must own | Why the state is not interchangeable |
| --- | --- | --- |
| Random loop | Ordered route tree, open frontier, siblings, closure state and efficiency beam | The destination sequence is discovered during a loop search |
| One-way | Complete-route incumbent, forward and backward labels, residual budgets and Pareto alternatives | The goal is a constrained path to the selected destination |
| Round trip | Regional gateways, complete-journey hypotheses, and atomic Offer portfolios | Outbound and return pricing may be joint, split, nested or open-jaw |

Keeping those Modules independent gives each Product Kernel a small Harness-facing Interface while preserving its own Evidence rules, solver, recovery state, and finalization criteria.

## Research foundations

These works influenced the design vocabulary and search discipline. Flight Search Harness does not claim to implement the published algorithms unchanged or inherit their formal guarantees.

- Delling, Pajor and Werneck, [Round-Based Public Transit Routing](https://www.microsoft.com/en-us/research/wp-content/uploads/2012/01/raptor_alenex.pdf): Pareto journeys, transfer-bounded rounds, dynamic timetables and simple pruning.
- Dibbelt, Pajor, Strasser and Wagner, [Connection Scan Algorithm](https://arxiv.org/abs/1703.05997): time-ordered transport connections, profile queries and Pareto transfer handling.
- Martins, [On a multicriteria shortest path problem](https://doi.org/10.1016/0377-2217(84)90077-8): multiple labels and nondominated paths under several objectives.
- Irnich and Desaulniers, [Shortest Path Problems with Resource Constraints](https://doi.org/10.1007/0-387-25486-2_2): resource extension, feasibility constraints, labels and dominance.
- Singla, [The Price of Information in Combinatorial Optimization](https://arxiv.org/abs/1711.00405), and Golovin and Krause, [Adaptive Submodularity](https://arxiv.org/abs/1003.3967): conceptual foundations for choosing bounded information-gathering actions when observations have a cost. The Harness uses engineering query priorities rather than claiming these papers' approximation guarantees.

## Detailed implementation references

- Random loop: [`product/random-loop/references/workflow.md`](../product/random-loop/references/workflow.md)
- One-way frontier exploration: [`FRONTIER-EXPLORATION.md`](../product/one-way/references/FRONTIER-EXPLORATION.md)
- One-way residual-budget search: [`RESIDUAL-BUDGET-SEARCH.md`](../product/one-way/references/RESIDUAL-BUDGET-SEARCH.md)
- One-way date scheduling: [`TEMPORAL-CORRIDOR-SCHEDULING.md`](../product/one-way/references/TEMPORAL-CORRIDOR-SCHEDULING.md)
- Round-trip regional contract: [`REGIONAL-ROUND-TRIP-CONTRACT.md`](../product/round-trip/references/REGIONAL-ROUND-TRIP-CONTRACT.md)
