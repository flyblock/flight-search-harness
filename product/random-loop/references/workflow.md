# Browser execution

Use this reference only while carrying out a live browser search. The installed [`low-cost-random-loop-flight-search` Skill](../../../skills/low-cost-random-loop-flight-search/SKILL.md) defines the Outcome and stop conditions.

## Execute the bounded loop

1. Read `ordered_dfs.active_path` and `ordered_dfs.next_candidate_id`. Start with the research option marked `dfs_recommended_parent=true`. Options for other parents are saved siblings, not competing next steps.
2. Execute the chosen request's `required_source_batch` in order, or its single `required_source_id` for an older Work Item. When one airport cannot be entered but the others can, stop the batch and partition the issued scope: put the supported airports in a `retry_current` partition first and only the excluded airports in an `advance_source` partition. Apply the same exact-cover rule to date partitions. Whole-scope technical `source_unavailable`/`scope_unsupported` moves through AZair → Google Flights → Kiwi.com → Trip.com → Skyscanner → KAYAK. An ordinary `no_market_result` or hard-rejected result backtracks; only root/closure AZair misses receive one Google challenger. Continue inside a batch only when this typed disposition permits it and return one group per executed source; query/source accounting remains one-per-source even though the Harness Work Item is shared.
3. Record the actual result as one or more groups from the executed sources. Include a unique `query_id`, the exact `request_id`, `source_id`, `submitted_scope`, boolean `coverage_complete`, structured `acquisition.method`, parent request, result URL, offset-aware observation time, and every field-complete offer visible in that executed scope, or an explicit `source_unavailable`, `scope_unsupported`, `scope_incomplete`, `no_market_result`, or `research_only` result. A partial result must include exact, non-overlapping `scope_partitions`; a narrow pair or one expanded Anywhere card may admit offers but may not close its broad parent. Do not sample or pre-rank offers before engine submission.
4. Submit the groups to the engine and update the same session file. Add `ai_decision_reason`. The engine decides whether the parent remains open and either admits usable children, repeats a pending source, advances exactly one source, or closes an exhausted branch.
5. Review the chosen parent's hard-valid children in cumulative-efficiency order. The configured `date_anchor_limit_per_destination` is binding; pruned excess dates remain audited. Continue into its first unexplored child. When that child subtree becomes a hard dead end, use the next sibling recorded by the DFS ledger. Backtrack to an ancestor only after the current parent's children are exhausted. Stop after a qualifying loop, verified exhaustion of the fully covered root tree, or the user ends the run.

When the required source is unavailable or cannot express the issued scope, record the technical disposition and follow the engine's fallback. When a source executes successfully but yields no market result or only hard-rejected offers, let the engine backtrack unless it explicitly issues the one allowed Google challenger for root/closure. Never open a later source before it is issued, never go back to an earlier source after advancement, and keep incomplete/research-only cells pending. No source failure may create a flight edge.

## Offer evidence

For every admitted offer record:

- origin and destination IATA airports;
- offset-aware departure and arrival times;
- mandatory total fare and currency for the configured traveler count and baggage policy;
- flight number or equivalent flight identity;
- result/evidence URL, booking URL, and observation time;
- distance evidence accepted by the engine.

An observed destination absent from the initial catalog may exist only inside the current session after the engine accepts its strict IATA code, real offer, offset-aware times, finite distance, HTTP(S) evidence and booking URLs, and flight identity. It never expands the configured departure or return pools.

## Search behavior

- Submit all complete visible offers from the executed page or result scope; let the engine normalize prices into the configured base currency, reject any leg at or above the configured exclusive price cap, compute distance per base-currency unit, apply the remaining hard rules, enforce the per-destination date-anchor limit, and order every retained sibling for deterministic depth-first exploration.
- Source batching is a control-plane optimization only: it must not skip a Provider, merge Evidence records, or count several Provider queries as one query.
- Search `Anywhere` until at least `min_legs - 1` legs have been used. Only then add engine-issued return-pool `closure_probe` requests; on the final available leg, search only the return pool.
- Treat one actual source group as one audited query. Identical replay under the same `query_id` is idempotent; changed content under that ID is invalid. Do not apply a hard query-count cap unless the user configured one.
- Supply an explicit `base_currency`, set `fx_to_base[base_currency]` to `1`, and provide rates for every observed currency. The engine exposes per-leg and cumulative distance per base-currency unit plus quality surplus as decision support. By default it does not reject a branch only because one leg or prefix is below the configured floor; it applies the hard efficiency floor to the rebuilt complete loop. A user may explicitly request a strict per-leg floor.
- When `quality_gate.maximum_leg_price` is configured, the engine treats it as an independent hard gate rather than a ranking hint. Its currency defaults to `base_currency` when omitted; a `null` amount disables the cap.

After the AI ends the search, run one report. An exact Google Flights result that visibly contains the dated itinerary, flight identity, current fare, and included baggage is sufficient final evidence; use a seller or carrier page only to resolve missing or conflicting facts, or when the user asks. Keep the current session and final report; retain extra round files only when the user requests an audit export.
