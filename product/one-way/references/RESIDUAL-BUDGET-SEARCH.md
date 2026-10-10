# Residual-Budget Backward Search (v0.7.0)

## Contents

1. Goal and scope
2. Core formulas
3. Backward recursion
4. Two search channels
5. Unknown values and Evidence
6. Query scheduling and pruning
7. Atomic Offers
8. Inputs, outputs, and command
9. Live browser-session integration
10. Synthetic acceptance scenario

## 1. Goal and scope

`scripts/residual_budget_search.py` is a deterministic, Provider-independent Product Kernel. It expands verified edges backward from the destination region toward predecessor nodes. The current complete route supplies the cost upper bound used to keep calculating how much an unknown prefix may cost while still producing a strictly cheaper complete route.

That upper bound guides only the search for a new lowest-price route. A single expensive leg cannot reject an otherwise valid complete-route hypothesis. Ordinary route enumeration retains observed complete journeys within hard budget limits. The AI compares complete journeys by mandatory total price, total time, and feasibility; the residual price-improvement channel is not a global admission gate.

The Product Kernel does not search the web, know the world's transport network, or hard-code airport codes or border corridors. The AI executes bounded Work Items in a user-authorized browser. The Product Kernel consumes only normalized source, schedule, price, and Evidence-state data.

Version 0.7.0 remains one-way first. The two directions of a round trip cannot simply be concatenated and described as jointly optimal.

## 2. Core formulas

Definitions:

- `U`: the incumbent total cost of the current verified, complete, feasible route;
- `delta`: the strict improvement amount derived from the explicit `currency_decimal_places` precision unless configured directly;
- `S(v)`: the verified total suffix cost from node `v` to the destination region; and
- `R(v)`: the strict price-improvement budget remaining for the unknown portion before or entering `v`.

```text
price_cutoff = U - delta
R(v) = U - delta - S(v)
```

When a verified edge from `u` to `v` with cost `c(u,v)` is admitted during backward expansion:

```text
S(u) = c(u,v) + S(v)
R(u) = U - delta - S(u)
```

Convert all money to integer units at the explicitly declared decimal precision before calculation to avoid floating-point error. A complete candidate may update the lowest-price incumbent only when `total_cost <= U - delta`. After an update, replace `U` immediately and recalculate every residual budget.

## 3. Backward recursion

1. Create a zero-cost suffix label for every destination node in the destination pool.
2. Expand backward to predecessor nodes only over observed edges that are time-valid, currently unexpired, and have known prices.
3. Accumulate suffix cost, time, total duration, transfers, and hops during every expansion.
4. At each node, retain labels that are mutually nondominated by cost, time, and hops, subject to configured count limits and diversity quotas.
5. A label reaching the origin pool forms a complete route. Update `U` only when it is strictly cheaper than the incumbent.
6. For a missing predecessor or connection, emit a bounded research request with endpoint policy, time window, residual budget, known suffix, and query value.
7. After the AI or Provider returns new Evidence, rerun the complete deterministic step instead of manually appending a route to old results.

`observed_prefix_price_floor` affects only queue order and is never a safe-pruning proof.

## 4. Two search channels

### Price-improvement channel

This channel seeks a complete route where `total_cost <= U - delta`. A suffix that exceeds the strict price cutoff is no longer eligible for this channel and cannot update the incumbent.

### Pareto-exploration channel

Within an independent `pareto_cost_ceiling`, retain tradeoffs that are nondominated by cost, time, and hops even when they cannot beat the current lowest price. This prevents the price incumbent from suppressing a slightly more expensive but faster route or one with fewer transfers.

Report the channels separately:

- price-improvement eligibility is not Pareto nondominance;
- Pareto admission is not a lowest-price improvement;
- only a strictly price-improving complete route updates the incumbent; and
- `pareto_cost_ceiling` must be at least the initial incumbent but still defines bounded exploration, not worldwide coverage.

## 5. Unknown values and Evidence

An unknown, expired, or future Offer:

- does not enter a reverse label;
- does not enter a complete route;
- is not treated as zero cost;
- may produce `price_and_schedule_discovery`, `refresh_observed_offer`, or another bounded research request; and
- may use an estimated cost only for query priority, never as route cost or a safe-pruning lower bound.

When the browser finds no result, retain `no_result` or a research candidate with the query scope. It does not mean that a connection does not exist.

Only a `verified_observed` edge admitted through the live Evidence Seam can participate in route calculation. See `EVIDENCE-ADMISSION-DEBUGGING.md` for admission diagnostics.

## 6. Query scheduling and pruning

Research candidates are prioritized by:

```text
priority = expected_improvement / query_cost
```

The script then emits a recommended subset ordered by query value while retaining the complete `research_candidates` collection. Budgets, geographic quotas, and mode quotas affect only `script_recommended`; they cannot prevent the AI from seeing a candidate. `standard`, `deep`, and `extreme` alter recommendation order and internal calculation scale, not user hard constraints.

Safe pruning is limited to:

- mathematical cost bounds from admitted nonnegative edge costs;
- proven admissible lower bounds inside their declared coverage;
- exact time-window and minimum-connection infeasibility;
- hard limits such as `max_hops` or `max_total_minutes`; and
- strict Pareto dominance.

Historical market prices, AI estimates, observed price quantiles, and heuristics without proven coverage may guide recommendation order only. Budgets and quotas no longer remove AI-visible candidates. Deterministic calculation may prune only mathematical impossibility from observed facts, hard time or hop limits, and strict Pareto dominance.

## 7. Atomic Offers

If a Provider supplies one indivisible total price for several physical movements, create a hyperedge with `pricing_scope: atomic_offer`:

- count the total price once;
- use `component_edge_ids` and `via_nodes` only to describe structure;
- do not invent component prices; and
- do not price its components separately inside the same route.

The v0.7 Evidence contract supports atomic multi-segment Offers. It does not allow multiple independent segment fields from a browser result to be reinterpreted as one multi-segment bridge; the Provider must present a genuine atomic Offer.

## 8. Inputs, outputs, and command

The standalone Product Kernel accepts:

```text
nodes, edges, origin_nodes, destination_nodes,
incumbent, currency, date_window, config
```

Run it with:

```powershell
python scripts/residual_budget_search.py --bundle <bundle.json> --output <result.json>
```

Main outputs:

- `incumbent`: initial and final costs, strict cutoff, history, and edge IDs;
- `reverse_labels`: each suffix's cost, residual budget, and channel membership;
- `remaining_budget.by_node`: the best known suffix context at each node;
- `routes` and `pareto_route_ids`;
- `search_channels`;
- `research_requests` and `research_candidates`;
- `query_budget` and `diversity_quotas`; and
- `snapshot_audit`, `safe_pruning`, and `trace`.

The broad policy and state contracts are:

- `schemas/residual-budget-policy.schema.json`
- `schemas/residual-budget-state.schema.json`

The one-way integration maps supported fields into the runtime Product Kernel. Do not describe a field in the broad contract as executed behavior unless the integration consumes it.

## 9. Live browser-session integration

`scripts/combination_engine.py` normalizes a residual research request into a Provider request. `scripts/live_provider_session.py` then:

1. Freezes the request into a durable Research Question with generation, endpoint policy, budget context, and Evidence contract.
2. Lets the Harness issue one attempt as a Work Item for the AI to execute in a user-authorized browser.
3. Transactionally admits authorized new nodes and verified Offers.
4. Saves the Work Item-local attempt reference, search round, incumbent history, and immutable Evidence bundle; budget counters remain derived views.
5. Reruns the combination Product Kernel and generates the next Research Question generation.

Work Item endpoint policies are `exact_node`, `node_pool`, and `provider_authorized_anywhere`. A new node can enter the Search Mission only if the Work Item allows discovery, declares a source, contains complete fields such as coordinates and time zone, and passes node validation.

`schemas/live-evidence-bundle.schema.json` accepts both `0.7.0` and `0.7.1`, but a bundle version must equal the Work Item's `evidence_contract_version`. The exact-endpoint Evidence flow remains compatible, while authorized node discovery and atomic Offers use the current v0.7 contract.
