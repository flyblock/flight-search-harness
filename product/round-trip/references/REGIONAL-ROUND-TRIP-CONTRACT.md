# Regional round-trip contract

## Product boundary

The round-trip Product Kernel accepts city or region anchors and returns a complete verified journey from the origin anchor to the destination anchor and back. It is not an airport-pair quote comparator.

The round-trip Product Kernel aligns with the one-way Product Kernel's upper-layer semantics—regional pools, Provider-led gateway discovery, Evidence identity, hypothesis lifecycle, access and egress, FX, baggage, and transport entitlements—while owning a separate Session and portfolio solver. It never imports one-way path state or treats two independently finalized one-way winners as a round trip.

## Search layers

```text
user city / region anchors
-> origin and destination gateway-discovery hypotheses
-> evidence-backed gateway candidates
-> AI-created complete-journey hypotheses
-> atomic-ticket and surface-research Work Items
-> deterministic complete timeline and mandatory-cost comparison
-> evidence-backed challenger assessment
-> finalization
```

Examples and previous runs never become default airports. `origin_anchor_nodes` and `destination_anchor_nodes` remain the user's actual endpoints. Gateway candidates are separate records and do not replace the anchors.

## Regional intent

`round_trip_intent_materializer.py` accepts:

- `origin` and `destination` region sides;
- one or more city, station, terminal, or airport anchor nodes;
- optional seed gateway nodes;
- `exact`, `city_region`, or `extended_region` pool policy;
- access/egress time and allowed-mode constraints, plus an explicit Base Currency cost limit or `null` for an unbounded ground-cost search;
- either one complete travel window or separate outbound and return departure windows;
- explicit traveler count, baggage policy, overnight policy, self-transfer policy, transport entitlements, FX snapshot, and transition buffers.

Date-only windows require an explicit `utc_offset` on the first anchor node. The materializer preserves declared IANA timezone names but does not depend on a host timezone database or infer historical offsets.

The materializer never browses, geocodes, or chooses an airport. A seed gateway is an advisory candidate, not a replacement for a city anchor.

## Gateway discovery

For a non-exact natural-language region, gateway discovery begins with two AI semantic passes rather than a hard-coded airport catalog. `region_scope_definition` proposes the bounded primary set and accounts for request seeds. The independent `region_scope_challenge` then reviews core city-code, nearby-city, surface-connected, cross-border, and specialized-airport categories and may add omitted airports. Both are persisted with `evidence_status=ai_proposed`; neither is Provider Evidence or solver input.

Only after both passes does the Session compile one required Provider-research Work Item per airport in their union. A regional gateway hypothesis cannot complete until every scoped airport has a terminal `research_observed`, `no_result`, or `rejected` disposition and at least one airport has real observed support. `coverage_complete=true` on one airport can never close the rest of the regional scope.

Every non-exact regional side starts with an independent `gateway_discovery` hypothesis. Browser observations use `evidence_status=research_observed` and may admit gateway candidates with source, URL, observation time, node identity, and a concrete access, connectivity, or fare signal.

Evidence admission uses its evaluation time as the trusted upper bound for Worker-reported observation and proposal times. A later `observed_at` or `proposed_at` is normalized to the trusted time before the record enters the Session. The admitted record retains the reported value, trusted bound, and normalization reason. This metadata repair preserves sibling Evidence and does not turn the Search Mission into a blocked Outcome.

Evidence bundle identity is product-idempotent. Replaying byte-equivalent admitted Evidence with the same `bundle_id` is a no-op; reusing that ID for different content is a protocol error.

Gateway signals are research facts, not priced movements. A concrete city/station anchor uses `anchor_required`, so exact scheduled access and egress must be captured as payable atomic surface offers before a door-to-door portfolio can complete. A user-named region may explicitly use `any_admitted_gateway`; then an admitted gateway inside that region is the endpoint and no ticket to an abstract country/region label is invented.

The AI selects which candidates deserve complete route research. Cheap proximity alone is not a sufficient reason; materially useful signals include observed access feasibility, a dated fare/calendar anomaly, a unique network connection, or a plausible complete-cost advantage.

## Provider adaptation layer

The broad source catalog and dynamic carrier/operator rules follow the one-way Product Kernel's Provider semantics. Round-trip policy adapts them into seven Work Item families instead of importing one-way route state:

```text
gateway_discovery
joint_quote
split_outbound
split_return
nested_atomic
surface_access
refresh
```

`round_trip_provider_advisor.py` is deterministic advisory logic. It ignores registry file order and scores only capability fit, geography, modes, flexible-date/multi-airport shape, payable versus research role, independent-combination support, and source diversity recorded in this Session. It cannot select a source for the AI, create source-specific Work Item clones, or prove that an unattempted source has no value.

Static priorities are Work Item-specific rather than universal. A source that is strongest for an atomic joint date grid may rank below Kiwi or another OTA for split or nested tickets. Configured routing implementations remain `configuration_required` without an endpoint/feed, and dynamic carrier/operator sources remain `awaiting_trigger` until discovered.

## Dynamic route hypotheses

After both gateway hypotheses are terminal, generate evidence-backed route suggestions and add the AI-selected hypothesis:

```powershell
python scripts/round_trip_hypothesis_generator.py --state <session.json> --output <suggestions.json>
python scripts/round_trip_session.py add-hypothesis --state <session.json> --spec <hypothesis.json>
```

The generator sorts gateway candidates by concrete price/access signals and uses lazy best-first pairing rather than producing the full Cartesian product. The published limit is only a visibility boundary; candidate-pool exhaustion is not required and no unsearched airport is treated as dead. When multiple admitted gateways exist, it may additionally publish a regional open-jaw challenger. Suggestions preserve their gateway evidence basis in the Session and never select themselves.

A normal `closed_airport_pair` spec names one or more admitted origin and destination gateways. The Session publishes:

- one joint round-trip quote question;
- one outbound one-way quote question;
- one return one-way quote question;
- exact origin access and return egress questions;
- exact destination egress and return access questions.

All required questions must receive verified or scoped terminal evidence before the hypothesis can be marked complete. A joint incumbent therefore cannot silently suppress split research, and vice versa.

The Session models each question as a Product-owned Research Requirement. Its `requirement_status` is limited to `open`, `satisfied`, `exhausted`, or `superseded`; the latest `evidence_disposition` separately records `ai_proposed`, `research_observed`, `verified_observed`, `no_result`, or `rejected`. Adapter-facing `candidate` and `pending` values are derived compatibility projections rather than a second Work Item lifecycle.

The first ticket question is deliberately the atomic joint quote. Real airline pricing commonly makes one filed round-trip fare cheaper than two separately purchased one-ways. The Product Kernel must observe that joint total directly and count it once. It must still research both split directions because the opposite result can occur; neither relationship is inferred.

For flexible dates, each ticket question is a two-stage AI-led question rather than a family of per-date Work Items. Calendar, date-grid, and flexible-search Observations use `research_observed` with incomplete coverage and enter a research-only temporal ledger. `round_trip_temporal_scheduler.py` organizes the supplied signals into a complete hard-feasible catalog plus non-binding advice. Only an explicit AI selection with a reason advances the same Work Item to exact payable verification. The AI may select a catalog item outside the suggested subset or propose another hard-valid date; no script may automatically create, select, dispose, or finalize a hypothesis from temporal ranking.

For `regional_open_jaw`, `nested_round_trips`, `mixed_atomic_portfolio`, or a custom complete journey, provide explicit `task_specs`. Each specification remains one Research Question and supplies its own executable query shape. This lets the AI investigate, for example:

```text
ORIGIN_REGION <-> HUB_X atomic round trip
+
HUB_X <-> DESTINATION_REGION atomic round trip
+
required surface access/egress
```

without inventing directional prices inside either round-trip ticket.

## Atomic portfolio solver

The solver accepts physical movements and atomic offers. Every offer price is counted once, regardless of how many movements it covers. Supported offer scopes are:

- `joint_round_trip`;
- `one_way` with explicit `outbound` or `return` direction;
- `atomic_itinerary` for nested round trips, multi-city, or another indivisible ticket;
- `surface` for rail, bus, coach, ferry, transit, walking, rideshare, or car movement.

Session ingest derives `itinerary_id` from physical movement geometry and `offer_snapshot_id` from seller/source/price/observation identity. Seller offers remain separate inside the itinerary offer set. The solver may collapse an equivalent complete route with identical movements, time, total, and portfolio kind only after attaching every equivalent offer combination as an evidence variant. A cheaper seller snapshot for the same itinerary competes normally.

An atomic ticket may declare `ticket_context.use_pattern=planned_exit_at_connection`. Its complete `movement_ids` remain evidence and determine the indivisible price. `used_movement_ids` must be a chronological prefix ending at `planned_exit_node`; `unused_movement_ids` must partition the rest of the ticket and end at `ticketed_final_node`. Only the used prefix forms the physical portfolio, but the full ticket price and baggage/irregular-operation conditions remain attached.

The Session permanently retains every admitted Offer. `round_trip_solver_projection.py` builds a bounded, audited solver view, preserving AI pins, the incumbent, active-hypothesis Evidence, and Work Item coverage before deterministic cost fill. Excluded Offers remain in the ledger and may be pinned later. The solver enumerates non-overlapping Offer subsets only inside that projection. A candidate is valid only when every selected movement is used exactly once in one chronological endpoint-to-endpoint outbound path followed by one chronological return path.

`round_trip_session.py projection-pin` is the explicit AI override interface. It accepts verified offer IDs and a reason, persists the pins, rebuilds the projection, invalidates any stale stop assessment/finalization, and never deletes excluded ledger evidence.

One-way Offers must independently complete their declared gateway direction. The Session attaches those coverage endpoints from the Browser Work Item; an Offer cannot mix outbound and return components while pretending to be a one-way ticket.

Portfolio labels are derived from the selected ticket offers:

- `joint_round_trip`;
- `split_one_ways`;
- `nested_round_trips`;
- `mixed_atomic_offers`.

Surface offers do not change a joint or split ticket label, but their mandatory effective cost and duration remain part of the complete portfolio.

## Cost and feasibility

- All movement times require UTC offsets.
- Optional `minimum_stay_nights` and `maximum_stay_nights` are measured from arrival at the destination anchor to the first return departure from that anchor using their visible local calendar dates. Missing bounds remain unbounded.
- Directional date intent preserves exact dates, bounded windows, or full flexibility independently. Joint queries bind one outbound/return date pair to one indivisible total; split directions keep independent price surfaces.
- Mode transitions apply configured buffers; independently priced flight connections use the self-transfer buffer. A sub-buffer connection is admissible only when the movements belong to the same atomic offer and `transfer_from_previous` explicitly records `status=verified` plus `connection_protection=carrier_protected`; then the Provider's sellable protected itinerary is accepted, subject to any recorded `minimum_connection_minutes`. Atomic pricing by itself never proves protection.
- `self_transfer_policy=never` rejects independently ticketed flight connections; `avoid` preserves them with an auditable count for AI ranking; `allow` applies only the deterministic buffer.
- `overnight_allowed=false` rejects an overnight connection inside either direction but never mistakes the intended stay at the destination for a connection.
- Complete mandatory cost must satisfy the optional base-currency budget, and elapsed time must satisfy the optional maximum total duration.
- The request declares `traveler_count`, `baggage_policy.required_items`, and `baggage_policy.paid_baggage_allowed`. Every selected flight Offer must visibly cover each required item. An item absent from `included_baggage` is admissible only when paid baggage is allowed and the Offer records a positive mandatory baggage cost; permission without an observed cost is not coverage. The atomic price plus mandatory baggage cost must cover the declared traveler party.
- Every flight movement requires carrier plus flight numbers or a structured visible unknown reason. Same-airport transfer context may be `verified`, `infeasible`, or `unknown`; the configured unknown policy either rejects it or preserves a warning in the portfolio.
- Surface offers do not invent a baggage requirement.
- Non-base currencies require an explicit source-attributed FX snapshot.
- A visible offer expiry is evaluated against the Session recomputation time. Expired snapshots cannot win a current portfolio and must be refreshed before reuse.
- A matching transport entitlement may change effective cost while preserving the observed amount, duration, and evidence.
- Unavoidable intermediate accommodation requires an observed amount. `include_required` counts it in optimization; `exclude_from_optimization` removes only that amount while preserving observed, included, and excluded totals and the underlying overnight fact.
- Unknown, estimated, topology-only, research-only, no-result, or rejected observations never become priced movement.
- Calendar, date-grid, and flexible-search signals remain in the temporal research ledger. A joint signal keeps one indivisible displayed total; paired directional signals keep two atomic amounts and any same-currency sum is non-payable research arithmetic.

## Search completion

A complete portfolio establishes an incumbent. It does not finish the search.

The first incumbent activates one bounded AI structural-corridor definition Work Item. A flexible regional request that permits meaningful surface transport publishes `minimum_proposals=1`; absence of prior fare Evidence cannot justify empty output. Other shapes may return no proposal with a concrete reason. Proposals use explicit atomic research Work Items and are not Evidence. Each must be activated or explicitly dismissed with a reason before stop assessment.

Finalization requires:

- primary plus adversarial AI scope passes and terminal coverage for every non-exact regional side;
- terminal outcomes for both gateway-discovery hypotheses;
- at least one complete journey hypothesis and complete portfolio;
- no active hypothesis;
- every selected hypothesis terminal;
- no unexplained pending Work Item;
- terminal structural-corridor discovery and no silently unresolved structural proposal;
- a fresh stop assessment recording either completed challenger hypotheses or why no concrete evidence-backed challenger remains.

The candidate queue need not be exhausted. The result is the best verified portfolio within the evidence and hypotheses actually investigated, never a global lowest-price guarantee.

The Session owns a monotonic `product_revision` that advances for every Product Kernel mutation and is independent from Harness lifecycle revisions. A stop assessment records the exact `assessed_revision`; any later Product mutation makes it stale. Finalization reruns the time-sensitive solver before applying the audit, so an Offer that expired while a Run was paused cannot survive resume merely because it was part of the previous incumbent.

Hypotheses are authoritative as a flat map because every current round-trip hypothesis is a direct child of the product root. The former tree, active-path, backtrack, per-feature history, Offer-set, and query-ledger structures are either removed or reconstructed views; they do not own product state.
