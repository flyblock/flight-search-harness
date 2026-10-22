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

## Shared journey cost scope

One-way and Round-trip retain independent Product state and solving rules while using the same cost vocabulary. Their default flight-centered objective is **Mandatory Air Ticket Total**: every required flight Offer after the first selected Gateway, including air positioning, self-transfer flights, required baggage, and mandatory airline fees. Endpoint Access, Endpoint Egress, Internal Surface Transfer prices, accommodation, and other non-air costs are disclosed but excluded from this ranking.

`gateway_complete_cost`, `endpoint_completion_cost`, and `anchor_complete_cost` remain optional audit calculations. They are not aliases for Mandatory Air Ticket Total and must not be presented as the default comparison when excluded costs are unknown.

Random-loop remains an airport-loop product. It does not add Endpoint Access or Endpoint Egress to its efficiency objective.

## What low-cost means

The three Product Kernels optimize different notions of value:

| Journey shape | Primary low-cost objective | Important competing constraints |
| --- | --- | --- |
| One-way | Lowest verified Mandatory Air Ticket Total within the disclosed scope and Research Credit budget | Explicit hard constraints and Internal Surface Transfer feasibility; long waits, overnight travel and unprotected transfers are warnings by default |
| Round trip | Lowest verified Mandatory Air Ticket Total within the disclosed scope and Research Credit budget | Stay length, joint versus split pricing, open jaws, Internal Surface Transfer feasibility and atomic ticket rules |
| Random loop | High verified flight distance per Base Currency unit inside the configured price and route constraints | Leg count, return feasibility, connection timing, date diversity and optional per-leg price cap |

The result is authoritative for the Evidence and hypotheses actually investigated. The Harness does not claim that every Provider, airport, date, or fare worldwide was searched.

## Random-loop search

Random-loop search discovers the destination sequence while preserving an airport-only return loop.

The Product Kernel ranks partial routes by cumulative distance divided by cumulative price in the user-selected Base Currency, while retaining configured date and geographic diversity. It explores one ordered depth-first branch at a time and keeps viable siblings for backtracking. Once the minimum route depth is reached, it can issue return-pool closure probes; on the final available leg it searches only for a valid return.

Every completed loop is rebuilt from admitted flight Evidence. The Product Kernel recalculates distance, mandatory price, connection timing, leg count, and complete-loop efficiency before the route can qualify. Calendar values and topology hints may guide discovery but cannot become flight edges.

This is a bounded search strategy over live, incrementally observed Offers. It is not a travelling-salesperson solver over a complete static graph.

## One-way search

For a city or region, the AI defines a finite Self-access Gateway Pool without a numeric airport cap. Regional requests use `any_admitted_gateway`: the user arranges Endpoint Access and Endpoint Egress, so current Runs neither price them nor publish Work Items for them. Exact airport requests may use `anchor_required` for compatibility.

The first verified complete flight path becomes the incumbent. Every later Work Item must fit the remaining Product-owned Research Credits and have an optimistic Mandatory Air Ticket Total below the incumbent by the configured percentage Price Challenge Threshold. There is no default Pareto-comfort lane, structural-diversity quota, fixed remote-airport phase, target alternative count, or manual stop assessment. Alternatives are natural by-products of pursuing the price winner.

Mandatory Air Ticket Total includes every required flight Offer, air-positioning or self-transfer flight, mandatory baggage, and airline fee. It excludes Endpoint travel, Surface prices, accommodation, and other non-air costs. `total_trip_cost` is optional audit output and cannot control ranking, budget pruning, or a price challenge. `gateway_complete_cost` is reported only when its whole audit scope is actually priced; an unpriced Internal Surface Transfer makes it incomplete rather than zero.

An Internal Surface Transfer is a non-Offer feasibility bridge. One external route check records only endpoints, approximate duration, feasibility, one Evidence link, and observation time. A plausible taxi or road fallback may connect two flights; explicit infeasibility or duration plus buffers exceeding the available window rejects that combination. The solver never inserts a zero-price TravelEdge.

Search Effort selects 10 credits at a 10% threshold for `economy`, 30 at 5% for `standard`, and 100 at 1% for `deep`. Exact payable research costs 2 credits, structured research costs 1, and an Internal Surface Transfer check costs 1. A real Provider query commits the credit even if its Receipt later needs representation correction; correction, replay, and non-Provider work cost zero and must not repeat research. Credit exhaustion with a fresh winner is bounded completion; without a winner it is `draft`.

No elapsed-time or transfer-count limit is added by default. User-supplied limits, `overnight_allowed=false`, and `self_transfer_policy=never` remain hard. Otherwise long waits, overnight travel, and unprotected tickets are delivery warnings.

## Round-trip search

Round-trip search does not concatenate two independently selected one-way winners. Its unit of comparison is a complete outbound-and-return portfolio.

The Product Kernel first asks the AI to disclose a Self-access Gateway Pool without a numeric airport cap. For regional requests the user arranges Endpoint Access and Endpoint Egress. It then creates complete-journey hypotheses and researches the atomic flight prices needed to compare them. A joint round-trip fare is observed and counted once. Split one-way tickets, air positioning, nested round trips, open jaws, and mixed atomic itineraries remain separate structures rather than invented component prices.

The portfolio solver accepts only chronological, Gateway-complete combinations of admitted atomic flight Offers. An Internal Surface Transfer is non-Offer Evidence: one external route search records approximate duration, feasibility, and one Evidence link. A missing public-transport result may use a plausible taxi or road fallback; explicit infeasibility or insufficient time rejects the Journey Alternative. No Surface fare or accommodation amount enters the default ranking.

The first valid portfolio becomes the current winner. Every further candidate uses one price-challenge frontier; there is no fixed remote-airport phase. Search Effort supplies Research Credits and a percentage Price Challenge Threshold. An outside-pool hub can advance only when its long-haul fare plus mandatory air-positioning lower bound can materially beat the current Mandatory Air Ticket Total. Exhausting affordable research with a verified winner is a bounded completion; exhausting it without a winner returns `draft`.

Candidate Slate v2 is only a price-ranked projection of distinct complete Journey Alternatives already found. It has no target size and never creates Work Items. The cheapest verified date represents each Journey Alternative; alternate dates, Ticketing Structures, and Seller Variants remain nested beneath it.

## Why there are three Product Kernels

The Harness shares coordination, recovery, Work Items, Receipts, Workers, and Store semantics. The search state does not belong in that shared layer.

| Product Kernel | State it must own | Why the state is not interchangeable |
| --- | --- | --- |
| Random loop | Ordered route tree, open frontier, siblings, closure state and efficiency beam | The destination sequence is discovered during a loop search |
| One-way | Complete-route price incumbent, Research Credits, price challenges and Internal Surface Transfer feasibility | The goal is the cheapest verified flight path to the selected destination scope |
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
- One-way Work publication is governed by Research Credits and the Price Challenge Threshold.
- Round-trip regional contract: [`REGIONAL-ROUND-TRIP-CONTRACT.md`](../product/round-trip/references/REGIONAL-ROUND-TRIP-CONTRACT.md)
