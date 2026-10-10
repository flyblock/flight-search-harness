# Evidence-backed portfolio exploration

Round-trip exploration is not a forward/backward path search. The unit of choice is a complete journey structure coupling origin gateways, destination gateways, outbound and return dates, stay constraints, surface access/egress, and atomic ticket portfolios.

## Deep-module seam

`scripts/round_trip_portfolio_exploration.py` sits between the product Session and the Browser/Harness control seam:

```text
gateway Evidence + registered hypotheses + verified solver portfolios
  -> Portfolio Exploration Archive / structural cells
  -> compact AI challenger actions
  -> lazy hypothesis registration + mandatory joint Work Item selection
  -> temporal research / payable Evidence / deterministic portfolio solver
```

The Interface is `build_archive`, `build_view`, and `challenger_by_id`. The Harness sees `activate_portfolio_challenger`; it does not read gateway-candidate internals, Work Item construction, temporal signals, Offers, or solver state.

Before this Seam receives gateway Evidence, `round_trip_ai_exploration.py` gives the AI a primary scope pass and an adversarial challenge pass, then compiles their union into required Provider research. The challenger explicitly checks airports outside core city codes and seed anchoring. After the first incumbent, the same Module accepts a bounded structural proposal and compiles explicit Work Item specifications; flexible regional surface-enabled searches require one proposal. It has no airport dictionary, geography examples, or price heuristic.

## Structural cells

Cells use route-independent descriptors:

- route shape: closed airport pair, regional open jaw, nested round trips, mixed atomic portfolio, or custom complete journey;
- gateway pattern: closed pair, origin open jaw, destination open jaw, or two-sided open jaw;
- evidence stage: gateway evidence only, registered research, or complete verified portfolio.

Lower bounds and cell elites are non-binding advice. Every published challenger remains AI-selectable. There is no universal anomaly threshold and no hardcoded airport, hub, geography, or itinerary template.

## Lazy activation

Selecting a challenger performs one product-owned transaction:

1. resolve the stable challenger from the current archive;
2. register its evidence-backed route hypothesis only if not already registered;
3. select the mandatory joint round-trip Work Item, or the first required custom Work Item when no joint Work Item exists;
4. persist challenger and hypothesis history;
5. invalidate stale stop/finalization state.

Inside a normal complete-journey hypothesis, the Product Adapter exposes only the joint atomic Work Item until its scoped Evidence is terminal. Split outbound/return and exact surface work remain required afterward. This removes a control-loop choice without assuming joint pricing wins.

## Reuse and independence

The Product Kernel follows the shared Provider, external Browser, Receipt retry, Store, and Outcome semantics. It does not reuse the one-way frontier state or share solver state. Random-loop depth-first search, one-way path frontiers, and round-trip portfolio challengers remain three separate deep Modules because their invariants differ.

## Recovery and finalization

The authoritative Session persists the flat `hypotheses` map, `active_hypothesis_id`, Research Requirements, admitted Evidence, and one `challenger_dispositions` map. `hypothesis_tree`, `hypothesis_history`, `portfolio_exploration_archive`, `portfolio_exploration_view`, Work Item `status`, and product-specific challenger-disposition projections are reconstructed in memory for the current Product Adapter and omitted again before persistence. Hypothesis results and Offer sets are generated only when exporting a result. These projections are never independent decision authority. After interruption, the required views are rebuilt from canonical product state and the active hypothesis resumes its unfinished Requirement.

Finalization still requires a complete verified portfolio, AI-defined and terminal regional coverage, terminal gateway hypotheses, terminal structural discovery, explicit activation or dismissal of every structural proposal, a completed journey hypothesis, no active or unexplained pending work, and a fresh challenger stop assessment. An AI proposal, archive entry, lower bound, temporal signal, or model statement cannot create a final result.
