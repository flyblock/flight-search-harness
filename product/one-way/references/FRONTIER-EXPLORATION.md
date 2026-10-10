# Evidence-backed frontier exploration

This Module lets the one-way Product Kernel investigate surprising, flight-led multimodal journeys without embedding a world route catalog or itinerary template.

## Deep-module seam

`scripts/frontier_exploration.py` sits between the Product Session and the Browser Work Item Seam:

```text
admitted Evidence -> deterministic solver frontier labels
                  -> Frontier Archive / quality cells
                  -> compact non-binding AI actions
                  -> Provider-compatible research Work Item
                  -> Browser observation -> Evidence admission
```

The module hides solver-label and quality-cell mechanics. Its small external interface is `build_archive`, `build_research_view`, `compile_frontier_request`, and `frontier_by_id`. The unified Harness sees only `explore_frontier` or `reopen_frontier`; it never reads or shares solver state.

## Authority split

The AI chooses whether a frontier is worth a browser query, which projected direction to investigate, which Provider-compatible scope to use, and whether to reopen or stop a branch. Script ordering, cost, quality-cell membership, and the eight-action projection are advice, not pruning or proof of low value.

Deterministic code admits Evidence, calculates time and mandatory cost, derives forward/backward labels, groups generic quality descriptors, enforces query and finalization state, and rejects unknown edges from routes. A frontier says only that an evidence-backed partial path reaches a node; it never claims an onward connection exists.

## Generic quality cells

Cells use only route-independent descriptors:

- forward or backward direction;
- observed geography bucket;
- flight-only, flight-led multimodal, or surface-only mode family;
- lower-bound border-count bucket;
- elapsed-time bucket;
- complete or partial state.

The cheapest and then fastest member is retained as the cell elite. This is diversity-preserving memory, not a universal price-anomaly detector. There is deliberately no hardcoded country, hub, border, timetable, price-per-kilometre threshold, or historical “normal fare” table.

## Search lanes

Without a complete route, the complete-trip baseline and its capability-matched Provider fallbacks remain the first obligation. Partial frontier actions become available only after that baseline is terminal. Once a complete incumbent exists, frontier actions may act as structural challengers.

Forward and backward elites are projected separately, at most four per direction. Selecting one creates a normal persisted hypothesis and research Work Item with its frontier context, residual budget, source coverage, and query-scope ladder. A terminal frontier is offered as `reopen_frontier` instead of being duplicated. Reopening is an explicit Product Kernel transition: it preserves the stable Research Question identity and prior Evidence, records the reopen reason, and reserves the next question-local attempt ordinal for the next Harness Work Item.

## Provider query shapes

`data/provider-query-shapes.json` records whether named sources support exact, airport-pool, country, region, continent, or Anywhere endpoint shapes. `scripts/provider_query_shapes.py` compiles a fidelity-ordered scope ladder for the Browser brief. `false` means do not execute that shape for that source; `unknown` may be tested and recorded. Capability never proves market coverage or route viability.

The shared Provider policy for the one-way and round-trip Product Kernels keeps Google Flights at base score 95, but binds it only when Google has verified support for at least one scope that actually expands the known pool. For example, a known-airport-pool fallback inside an `ANYWHERE -> exact` Work Item does not satisfy reverse discovery. A verified Kiwi `country -> exact` shape may therefore start that Work Item without first issuing a duplicate Google baseline query.

## Recovery and finalization

The Session persists `frontier_history`, canonical hypothesis state, generated Research Question context, question-local attempt references, and Evidence. `frontier_archive`, `frontier_research_view`, the ordered hypothesis tree, Provider coverage, and budget counters are reconstructed deterministically when the Session is loaded. After interruption, the Runner publishes only open work. Reopening preserves the original Research Question and prior attempts; it never reuses the identity of a committed Work Item.

Frontier selection invalidates any prior stop assessment. A route becomes final only through `finalize-result`, after a complete verified route exists, every selected hypothesis has a terminal disposition, pending work is explained, and a fresh challenger stop assessment covers the current search round.

## Acceptance evidence

When a reverse Observation discovers a previously unknown node, the Product Kernel may issue a bounded predecessor question and admit a later Observation that closes a complete route. This demonstrates the discovery Seam; it does not claim a model will invent every useful real-world corridor or that unsearched routes are absent.
