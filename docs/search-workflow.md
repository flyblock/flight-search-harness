# Search workflow

## Runtime and responsibility

The AI researches providers, interprets user intent and source statements, and chooses the next query. Code saves observations, records actual query coverage, checks explicit requirements, and compares known candidates. A saved observation, an admitted Offer, an eligible candidate, and a stopped search are different facts.

Use the shared CLI directly from the Plugin directory with `python -B -m flight_search_harness`, or use the portable launcher from any directory. The optional selected Skill's resolver returns absolute paths. The launcher form is:

```text
python -B <launcher_ref> --runs-root <runs_root_ref> <command> ...
```

Use Python 3.10 or later and install `requirements.txt` for the standard IANA timezone database. This dependency is timezone data, not a client SDK. Without the database, records still save, but checks requiring an unavailable timezone remain unknown. The suite manifest supplies an external `runtime_data_root`; put generated input files and captures there, and Runs beneath its `runs` directory. There are no provider credentials or source permissions encoded in product manifests. Each product manifest declares a journey shape and a `request_schema_ref` relative to the Plugin root, explicitly identified by `request_schema_base: plugin_root`.

A Run stores requirements and revisions, queries, raw and normalized observations, explicit combinations, model assessments, and research status in `<runs_root>/<run_id>/product/session.json`. Comparison and delivery are derived from those same records. No second final-result store is authoritative.

## Start from the actual request

Create a JSON Request, optionally using a journey Skill's template, with `request_version: "7.0"`, `journey_shape`, the original `user_request`, and `intent`. All optional budget, FX, output, presence and search settings belong under `intent`. Use the shared Request Schema for field structure.

- Default `ranking_basis` is `mandatory_air_ticket_total`. Select `complete_journey_cost` only when full journey expense is requested. Endpoint feasibility still matters under either scope.
- `objective` is `price`, `stay_duration` (round trip), `travel_duration`, or `distance_per_currency`. The loop default is distance per base currency unit; the fixed-destination default is price.
- `budget: {amount, currency}` is a hard limit. `price_target: {amount, currency}` is a preference. Both use the explicitly selected base currency.
- The current structured traveler field is `adult_count`. A child or infant request must not be converted to adults; preserve the original requirement and identify that capability gap.
- Do not invent query limits, duration limits, baggage requirements, airport concessions, or output caps. If supplied, `search_limits` supports `max_queries` and `max_elapsed_minutes`. `max_queries` counts logical query attempts recorded by the harness, not arbitrary browser, search, clock or other client tool calls. The caller must separately honor any user limit on external tool calls; one recorded query may involve several tool interactions.
- Airport-only templates require accepted airport endpoints. Preserve a city, address or station through the Schema's full `origin`/`destination` declarations. Seed airports are a starting point for discovery, not a user-imposed border. For a flexible city or region endpoint, discover realistically reachable airports from external airport, route or ground-transport sources; national, state and municipal boundaries do not by themselves exclude them. Preserve explicit user airport or geographic restrictions. Save discoveries with their evidence and access judgment instead of changing the user's Anchor or silently broadening a hard restriction.
- FX needs `from_currency`, `to_currency`, `rate` and an offset-aware `observed_at`. Attribute the observed rate with `source_name`, `source_url`, or both. A source name can replace an unavailable link; URL shape and length are not Request admission gates. Currency conversion uses a valid rate/time and at least one nonempty source attribution. A rate with no attribution remains unusable for comparison; do not invent or remember a rate to fill a gap.
- Concrete `presence_intervals` require `start`, `end`, and `location_node_id`. Offset timestamps identify instants. Unmodeled semantic requirements use `additional_requirements: [{requirement_id, text}]` with later evidence-backed assessments.

```text
start --request <request.json> [--run-id <new-id>]
```

Use a fresh Run for independent searches. For an authorized continuation, use the identified Run. A change of requirements within that continuation uses `revise --run-id <id> --request <updated-request.json>`; it preserves findings and adds a requirement revision. Do not load unrelated history.

## Query, save, compare, continue

Choose the next query from saved findings, unresolved facts and actual query history. Record the provider, airports, date pair or date window, travelers, filters and market that were actually used. One wide planned search does not stand in for narrower searches subsequently executed. If the wide view is unsupported, retain that outcome and record the actual narrower queries separately.

Use [the complete quote submission](../examples/record-quote.json) or [the incomplete price lead submission](../examples/record-signal.json). For competitive research, see [the challenge query](../examples/query-challenge.json) and [the airport discovery record](../examples/record-airport-discovery.json). The selected Skill's resolver returns absolute template references for all four examples. Replace every `REPLACE_WITH_...` value with the observed value and correct JSON type; remove optional fields that were not disclosed. These are templates, not observations. Adapt their movements and purchases to the actual one-way, round-trip or loop itinerary. Do not inspect internal module names to discover the submission format.

A normal submission can contain the actual query, its observations and its outcome together:

```json
{
  "query": {
    "source": {"provider": "REPLACE_WITH_PROVIDER"},
    "scope": {
      "origin_airports": ["REPLACE_WITH_IATA"],
      "destination_airports": ["REPLACE_WITH_IATA"],
      "outbound_date": "REPLACE_WITH_YYYY-MM-DD",
      "return_date": "REPLACE_WITH_YYYY-MM-DD",
      "adult_count": 1,
      "fare_conditions": "REPLACE_WITH_ACTUAL_FILTERS_AND_MARKET"
    }
  },
  "observations": ["REPLACE_WITH_OBSERVATION_OBJECTS_FROM_AN_EXAMPLE"],
  "query_result": {
    "status": "completed",
    "reason": "REPLACE_WITH_THE_ACTUAL_NARROW_QUERY_EXECUTED"
  }
}
```

```text
record --run-id <id> --input <record.json>
deliver --run-id <id>
```

`record` saves the observations and associates them with that query in one operation. Matching source and scope reuse the recorded query; resubmitting the same capture does not establish that research happened again. Put `retry_reason` in the submission for an actual repeat lookup or refresh. A query association error is returned without discarding its observations. The response includes the associated `query`, `query_errors` and `query_warnings`. An inline query without an explicit outcome has status `observed` and unknown `started_at`; registration time does not invent when the search began or whether its scope was completed. An unexpected discovery may still be saved without either `query` or `query_id`; `unlinked_observation_ids` in status and delivery shows which findings lack query association.

For a planned lookup, use `query --run-id <id> --query <query.json>` first to check existing work and any explicit user search limits. A `query_ready` result supplies `query_id`; a `query_exists` result identifies an earlier attempt to inspect before repeating. Use `--retry-reason <reason>` for an intentional retry. Then submit the observations with this `query_id` instead of an inline `query`. Registering a query does not operate a browser. Logging facts after a lookup does not authorize exceeding the user's search limits. If already collected facts are submitted after a limit or stop, they remain saved with `query_warnings` and the query's persistent `registration_warnings`; use the pre-query command to enforce the limit before new work.

Keep query scope stable and precise. Avoid transient URL tracking tokens in query identity. A source or booking URL may be retained when available, but it is not a prerequisite for saving or comparing the observed price. Save each useful observation promptly, including higher prices and partially checked prices from selected-itinerary pages. Do not wait for the cheapest option, a complete itinerary, or the end of the research session. Missing flight detail means a saved signal or incomplete Offer, not a reason to discard an observed price.

`query_result.status` is `completed`, `no_result`, `unsupported`, or `failed`. Every result needs a reason describing actual coverage or failure. `no_result` also needs associated source evidence; technical failure does not mean no flights. A completed exact query does not establish that every date or seller has been searched. Query failure does not stop the Run: use the saved state to choose another useful date, route, combination or source.

## Challenge the current best

Once a usable candidate exists, treat it as a benchmark to improve, not an automatic stopping point. Choose plausible competitors using neighboring dates, alternative reachable airports, sellers, open-jaw journeys or separately priced combinations allowed by the user. The agent chooses directions; code compares recorded candidates under the same requirement revision and objective.

Add `purpose: "challenge"` and a concrete `reason` to the normal query object. Optional `baseline_candidate_id` selects an existing eligible baseline; otherwise the runtime selects the current best. Use the same object with pre-query `query`, or inline in `record`. The runtime records the baseline reference and comparison context on the query. A purpose label alone does not establish that research happened: actual findings must be associated with the query. Results distinguish an improvement, no improvement, unresolved facts and an unavailable query method. Reused work or failed execution must not be described as a newly completed price challenge.

A successful challenger becomes the current first-ranked candidate through ordinary comparison. Previous baselines remain in the same ledger and subsequent ranks while still eligible; newer snapshots of the same Seller Variant are not separate journey alternatives. No challenge-specific quote store, research credit or universal improvement percentage is needed.

For an unlocked city or region endpoint, make external airport discovery part of choosing credible challengers. Use `purpose: "airport_discovery"` and record the actual source, geographic/search scope and reason. Preserve observed airport and transport facts in a signal's `gateway_discovery`; its `side`, airport `node`, `decision`, `reason` and `access` judgment follow the discovery example. `decision` is `investigate`, `defer` or `exclude`; access status is `unknown`, `plausible`, `verified` or `infeasible`. Those decisions are model judgments, not proof that ground travel or border entry has been verified. A recalled airport list or an `ai_proposed` record is not external discovery evidence.

Screen price potential and realistic access before detailed investigation. Look beyond the initial airport list when the user's scope permits it, without a fixed airport count or country quota. Record why a direction merits investigation, is deferred or is excluded. Keep known access time and cost separate from airfare. Unknown access cost is not zero, and a lower ticket price alone does not prove a lower complete journey cost. A user focused only on airfare need not receive a compulsory door-to-door pricing task. Explicit airport restrictions and the random loop's accepted departure/return airport sets stay intact.

Read `research_feedback` after recording and comparison. It exposes `current_best`, challenge results, airport-discovery progress, open opportunities, next actions and search gaps. Use those gaps to select useful work rather than stopping merely because one flight can be booked. A source failure ends that method's attempt, not the exploration objective. When stopping is appropriate under user limits or the value of remaining work, state the actual reason and remaining scope; saved results remain deliverable.

## Observation and Offer fields

The examples preserve raw JSON and show the complete nesting. Essential fields are:

| Object | Fields and meaning |
|---|---|
| Observation | `kind`, `source: {provider, seller?, page_url?}`, `captured_at`, `movements`, `offers`; `source_text` preserves the observed facts. Screenshots, page-text attachments and `capture_refs` are optional. |
| Movement | `movement_id`, `mode`, `from_node`, `to_node`, `departure`, `arrival`; flight `flight_identity: {marketing_carrier, operating_carrier?, flight_numbers: [...]}`. `operator` preserves the displayed operating airline. |
| Offer | `offer_id`, `movement_ids`, `price: {amount, currency}`, `price_scope: "party_total"`, `traveler_count`, seller, observed `booking_url` and restrictions. A joint round-trip total belongs to one Offer covering all its flights. |
| Ticket details | Preserve source-disclosed `ticketing_structure`, `included_baggage` and `checked_bag_count`. Unknown baggage is not zero baggage. An optional `mandatory_baggage_cost` must be an observed amount, not an estimate invented to fill a field. |
| Connection | On the following movement, `transfer_from_previous: {status, connection_protection}` describes that connection. `status` is `verified`, `infeasible` or `unknown`; protection is `carrier_protected`, `self_transfer` or `unknown`. Preserve the supporting text/capture. |
| Verification stage | Offer `verification_stage` is `listing`, `selected_itinerary`, `fare_review`, `checkout` or `unknown`. Record the stage actually reached, not one inferred from complete fields or the seller's name. |

Read a tool clock while collecting the source; do not estimate the time. The portable `clock` command needs no Run and returns `utc_now` in UTC:

```text
python -B <launcher_ref> clock
```

A client clock or Python UTC clock is also usable. Retain the sampled timestamp with the observation and reuse it during normalization and saving. `received_at` is the runtime's separate save time. Reliable metadata times are normalized to UTC; flight schedules retain their airport-local clock and offset for local-date calculations. Missing, naive, conflicting or future capture times remain unknown or unreliable with `metadata_issues`; they do not discard useful flight facts. Never substitute save time for capture time or move a timestamp backward to pass validation. The runtime must not present an unreliable time as fresh verification. Provider quote time and validity remain independent source facts.

References help reopen a source, but are not admission gates. Keep a full URL when it is available; do not shorten it to satisfy a length rule. Missing, malformed, truncated or expired links do not invalidate the observed flight and price. Normalized unusable links may be null, with the supplied value retained in raw data and a metadata notice. Source/ seller identity and reproducible flight dates, airports and numbers remain useful even when a session link no longer opens. No screenshot or page-archive step is mandatory. Amounts, currency, traveler count and movement chronology still require valid facts.

A single ticket is a ticketing arrangement, not proof of carrier-protected connections, airside eligibility or entry permission. Those connection facts require their own source evidence. Do not mark protection verified just because every flight has one airline or one price.

Use `kind: "signal"` when a visible price lacks enough itinerary detail to be a payable Offer. The signal example retains `observed_price`, date `scope`, the page stage, missing facts and source text, with empty movements/offers. Save it even when a later quote is cheaper. For complete Offers, preserve original currency and party total; do not silently multiply per-person headlines or split a joint fare into invented leg prices. Membership and ordinary prices retain their conditions as distinct options.

Incomplete and invalid records remain saved with issues; a valid sibling Offer can be evaluated independently. Source identity does not depend on a registry. Admission establishes usable facts, not eligibility, exhaustive coverage or booking verification. Only source-disclosed validity belongs in `source_valid_until` or Offer `expires_at`; no fixed quote lifetime is implied.

## Combinations and judgments

To compare a proposed set of purchases, save:

```json
{
  "offer_refs": [
    {"observation_id": "REPLACE_WITH_ID", "offer_id": "REPLACE_WITH_ID"},
    {"observation_id": "REPLACE_WITH_OTHER_ID", "offer_id": "REPLACE_WITH_OTHER_ID"}
  ],
  "reason": "REPLACE_WITH_WHY_THESE_PURCHASES_FORM_A_USEFUL_JOURNEY"
}
```

Then call `combine --run-id <id> --input <combination.json>`. No purchase may appear twice. Code evaluates schedules, prices and supported explicit conditions; a proposed combination is not automatically feasible or protected. AI decides which combinations are worth investigating.

For a named `additional_requirement`, use `assess --run-id <id> --input <assessment.json>` with `candidate_id`, current `request_revision`, `requirement_id`, `status` (`satisfied`, `violated`, or `unknown`), `reason`, and `evidence_refs` naming saved observation IDs. Conclusive assessments require source evidence. Assessments cannot override arithmetic or other code checks. After revision, judgments need to refer to the current requirement version.

## Delivery and recovery

```text
deliver --run-id <id> [--view ranked|matrix|full] [--limit <n>]
status --run-id <id>
stop --run-id <id> --reason <actual-reason>
resume --run-id <id>
```

Delivery is available while research is active or stopped. Ranked `entries` meet the supported ticket and declared-constraint checks; `eligibility_scope` does not certify every part of a complete journey. `alternatives` retain violated or unknown checks; `signals` preserve incomplete leads. A limit affects presentation, not saved records. The full view includes the source session; matrix view groups observed candidates by date and does not invent unsearched cells or imply exhaustive coverage.

Present the current best together with the useful complete alternatives already found, normally as a compact comparison table. Previous beaten baselines are natural alternatives, not extra research tasks. Show however many useful candidates exist; do not search merely to fill second or third place. Keep incomplete price leads separate from bookable or reviewed candidates, and never count successive snapshots as different journeys.

Use the returned movement dates, airports, carriers/flight numbers, price scope, currency, seller/source, observation time and restrictions in the answer. Preserve `verification` and `required_disclosures`: report the actual page stage, unresolved connections and unverified endpoint access or egress. A ticket can rank while its city connection remains unverified; that does not require pricing a complete door-to-door journey. An expired provider validity period requires rechecking and keeps overall verification partial even when other facts are complete. Do not turn `single_ticket` into a claim of protected transit, omit an unresolved warning, or claim a booking-ready result from a calendar signal. Deliver from saved findings rather than browser memory.

Stopping records why exploration ended; it does not declare a global optimum. `resume` marks the identified Run active and returns its research state; it neither launches the browser nor reruns queries. Inspect `deliver` and query history, then choose the next useful action.

For an explicitly requested legacy continuation:

```text
import-legacy --source-run <identified-run-directory> --request <current-request.json> [--run-id <new-id>]
```

Import preserves source observations and original files. It does not adopt prior rankings, completion labels or audit conclusions as evidence. An unsupported legacy representation must remain a stated import gap rather than invented data.

## Distribution

The current Plugin contains the shared runtime, one Request Schema, three journey declarations, three Skills and maintained guidance. Product manifests do not load adapters. Package construction checks listed file existence, safe paths, Python syntax and copied bytes; it is not evidence that a travel result is correct.

```text
python -B scripts/package_flight_search_harness.py validate
python -B scripts/package_flight_search_harness.py archive
python -B scripts/package_flight_search_harness.py install
```

Archive and install default to the external runtime data root's `releases` directory, using the release name and version. An existing destination is never overwritten; choose a new explicit `--destination` when needed. CLI results are JSON on stdout and diagnostics go to stderr.
