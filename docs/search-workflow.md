# Search workflow

You (the AI) search sources with your own browser, HTTP or API tools, judge what you see, and choose the next query. The harness is a ledger: it saves every finding, notices repeated queries, compares candidates under the user's Request, and ranks them. It never operates a browser and never discards an observed price.

## Commands

```text
python -B <launcher_ref> --runs-root <runs_root_ref> <command> ...
```

| Command | Use |
|---|---|
| `clock` | UTC time for `captured_at`. No Run needed. |
| `start --request <file> [--run-id <id>]` | New Run from a Request. |
| `record --run-id <id> --input <file>` | Save a query with its observations and outcome (main command). |
| `query --run-id <id> --query <file> [--retry-reason <text>]` | Optional pre-check before a lookup: finds an earlier identical query and enforces user search limits. |
| `combine --run-id <id> --input <file>` | Propose separately bought saved Offers as one journey. |
| `assess --run-id <id> --input <file>` | Judge a named `additional_requirement` for a candidate. |
| `revise --run-id <id> --request <file>` | Change requirements (or add an FX rate). Findings and still-valid assessments carry over. |
| `deliver --run-id <id> [--view ranked\|matrix\|full] [--limit <n>]` | Ranked result. |
| `status`, `stop --reason <text>`, `resume` | Continuity. |

Inputs are UTF-8 JSON files; outputs are compact JSON on stdout (add `--verbose`, or `deliver --view full`, for complete records). Errors go to stderr. Use the same `--runs-root` for the whole Run. Keep generated files under the external `runtime_data_root` returned by the Skill resolver, never in the Plugin directory.

## 1. Request

Start from the Skill's request template. Required: `request_version: "7.0"`, `journey_shape`, the user's original words in `user_request`, and `intent`.

| `intent` field | Meaning |
|---|---|
| `base_currency`, `adult_count` | Comparison currency; adults. Children/infants are a capability gap: say so, do not convert them to adults. |
| `ranking_basis` | `mandatory_air_ticket_total` (default) ranks by what can change the choice: tickets, mandatory surface legs between flights, and access to/from airports *you discovered* outside the user's own ones (city access to the user's own airports, or egress when "any airport there" is fine, is the same for every option and is not counted). `complete_journey_cost` only when the user asks for door-to-door cost. |
| `objective` | `price` (default), `stay_duration`, `travel_duration`, `distance_per_currency` (loop default). |
| `budget` / `price_target` | `{amount, currency}` hard limit / preference. |
| `fx_rates` | `[{from_currency, to_currency, rate, observed_at, source_name and/or source_url}]`. Needed to compare foreign-currency prices. Never invent or recall a rate. |
| `baggage_policy` | `{cabin_baggage, checked_bag_count, paid_baggage_allowed}` only if the user requires baggage. |
| `overnight_allowed` (true/false), `self_transfer_policy` (`allow`/`never`), `maximum_transfer_count` (integer), `allowed_modes` (array of `flight`, `rail`, `bus`, `coach`, `ferry`, `local_transit`, `walk`, `rideshare`, `car`) | Only what the user said. "Any transport mode" means omit `allowed_modes` or list all of them. |
| `additional_requirements` | `[{requirement_id, text}]` for anything else; judge later with `assess`. |
| `search_limits` | `{max_queries, max_elapsed_minutes}` only if the user set limits. |

Do not invent limits, baggage needs or airport restrictions. Journey-specific date and endpoint fields are in the Skill.

**Endpoints.** If the user accepted specific airports, use `origin_airports` / `destination_airports`. If they named a city or region ("Frankfurt city", "Greater Bay Area"), declare the side as a region instead, so the harness treats your seed airports as a starting point, not a border:

```json
"origin": {
  "region_id": "frankfurt", "label": "Frankfurt city and reachable airports",
  "country_codes": ["DE"], "policy": "extended_region", "endpoint_policy": "anchor_required",
  "anchor_nodes": [{"node_id": "frankfurt", "node_type": "city_destination", "location": "Frankfurt am Main",
                    "country_or_region": "Germany", "country_code": "DE", "timezone": "Europe/Berlin"}],
  "seed_gateway_nodes": [{"node_id": "FRA", "node_type": "airport", "location": "Frankfurt Airport",
                          "country_or_region": "Germany", "country_code": "DE", "timezone": "Europe/Berlin"}]
}
```

A `destination` region uses the same shape. Every region needs at least one `anchor_nodes` entry and a `seed_gateway_nodes` list, and every node needs all six fields shown.

`endpoint_policy`: `anchor_required` when the journey must start/end in that city (access is then disclosed as unverified until you record it); `any_admitted_gateway` when "any airport there" is fine. `policy`: `exact` (only the seeds count), `city_region` (airports serving that city or named region), or `extended_region` (also airports farther away that are reachable by ground or a positioning trip; use this when the user did not restrict airports). `country_codes` lists the countries/regions the anchor itself covers (Greater Bay Area: `["CN", "HK", "MO"]`); it is descriptive, not a search boundary. Optional user ceilings: `max_access_time_minutes`, `max_egress_time_minutes`, `max_ground_cost`, `allowed_ground_modes`.

## 2. Search loop

Repeat: look at a source → read the clock → `record` → read `research_feedback` → pick the next query.

A normal `record` input (templates: `record_quote_template_ref`, `record_signal_template_ref`):

```json
{
  "query": {"source": {"provider": "..."},
            "scope": {"origin_airports": ["FRA"], "destination_airports": ["HKG"],
                      "outbound_date": "2026-11-16", "return_date": "2026-11-30",
                      "adult_count": 1, "fare_conditions": "economy, DE market"}},
  "observations": [ ... ],
  "query_result": {"status": "completed", "reason": "exact date pair searched"}
}
```

- Record what you actually searched: provider, airports, dates or date window, travelers, filters, market. `scope` is free-form; for a calendar or flexible-date scan use e.g. `"outbound_date_window": {"start", "end"}` and `"trip_length_days": 8` (say whether the source counts days or nights). One wide search does not stand in for narrower ones you ran later.
- `query_result.status`: `completed`, `no_result` (needs the source text showing it), `unsupported`, `failed`. A failure means that method failed, not that no flights exist; try another source or approach.
- Save every useful price immediately, including higher prices and partly checked itineraries. Do not wait for the cheapest or a complete itinerary.
- Same source + scope again is reported as a duplicate. For a deliberate refresh add `retry_reason`.
- An observation found without a planned query may be recorded without `query` (then omit `query_result` too).
- Several observations from one page view (one calendar, one result list) go in one `record` call.

**Quote vs signal.** `kind: "quote"` has `movements` (each flight with airports, offset-aware departure/arrival, `flight_identity`) and `offers` (price as `party_total` in the original currency, seller, `verification_stage`). `kind: "signal"` keeps a visible price that lacks itinerary detail (`observed_price`, `scope`, `missing_facts`, empty movements/offers). A signal is a lead, never a bookable runner-up.

**Time.** Run `clock` (or any tool clock) while the page is on screen and put that UTC time in `captured_at`. Observations read from the same page view may share it; a different page or a later look needs a fresh reading. Never estimate or reuse an old reading. A bad time is flagged, not rejected.

**Links and screenshots** are optional. Keep a full URL if you have one; a missing, long or expired link never invalidates a price.

## 3. Explore beyond the obvious (this is the point)

A first bookable fare is a baseline to beat, not a reason to stop. After every `record`, read `research_feedback.next_actions`; they name concrete gaps.

- **Challenge the current best.** Add `"purpose": "challenge", "reason": "..."` to the query (template: `query_challenge_template_ref`). Try neighbouring dates, other airports, other sellers, other ticket structures. A winner becomes rank 1; the beaten fare stays as a runner-up automatically. Do not search only to fill ranks.
- **Look past borders.** For a city or region side, discover airports from external sources, not memory: core city, nearby cities, surface-connected airports, cross-border airports (Frankfurt → Luxembourg, Brussels, Zurich, Strasbourg; New York → Philadelphia; Hong Kong ↔ Shenzhen, Guangzhou, Macau), secondary/low-cost airports, and hubs reachable by a cheap positioning flight or train. A border alone is never a reason to exclude. Record them with `"purpose": "airport_discovery"` (template: `record_airport_discovery_template_ref`). In `gateway_discovery` list the `reviewed_categories` you actually checked (`core_city`, `nearby_city`, `surface_connected`, `cross_border`, `secondary_or_low_cost`, `hub_positioning`); per airport give a `decision` (`investigate`/`defer`/`exclude`) with a reason and `access: {status: unknown/plausible/verified/infeasible, minutes?, cost?: {amount, currency}, basis?: observed/estimated}`. The feedback then names unreviewed categories and, per airport, the fare that would beat the current best after access cost ("Via LUX: fares < …"). Access to a discovered airport is part of its ranking cost (one direction per `access.cost`; a round trip counts both ways; a priced positioning leg in a `combine` counts as observed). An estimate is fine to start (`basis: estimated`); check the real price only when it could change the winner. Unknown access cost is unknown, not zero: such an option is shown but not ranked above known ones.
- **Try other ticket structures** the user allows: separate tickets via a hub (`combine`), open-jaw (different airports per direction), positioning flights. Mark such a challenge query with `scope.ticketing_structure`: `separate_purchases`, `open_jaw` or `hub_positioning`. A joint ticket is one indivisible Offer.
- **Search from both ends and meet in the middle.** Prices break at borders and between markets: international fares into a country are often far above its domestic fares, and low-cost carriers serve hubs just outside it. So look for (a) the cheapest places to fly *from* the origin to anywhere (explore/"anywhere" searches), and (b) the cheapest places to fly *into* the destination from (often domestic or regional hubs), then for a cheap connection between them by air, rail, bus or ferry. Examples of the pattern: Frankfurt → Almaty (low-cost) → bus across the Khorgos border → train to Urumqi → domestic flight to Hong Kong; Europe → Bangkok or Hanoi → overland or regional flight into southern China; a long-haul into Seoul or Tokyo plus a regional hop. Record each priced piece (including bus/train legs, as quotes with `mode: "bus"`/`"rail"`) even if it is not a full journey: the feedback turns pieces into budgets ("Connect ALA→URC for < 840 CNY", "Positioning Frankfurt⇄MXP must cost < 169 CNY"). When the pieces add up, `combine` them. Check connection times, visas and baggage on self-transfer routes and disclose the risk.
- **Follow cheaper leads.** A signal cheaper than the current best deserves a follow-up query that turns it into a quote or rules it out. Put the lead's observation id in `query.follows_up: [...]`; once that query has a result, the lead stops appearing, and your `query_result.reason` (e.g. "price not reproducible at selection") is kept.
- **Cross-check sources.** If every quote came from one seller, check another.

Stop when user limits are reached or remaining directions are unlikely to pay off; `stop --reason` with what was left unsearched. Stopping never claims a global optimum.

## 4. Observation fields

| Object | Fields |
|---|---|
| Observation | `kind`, `source: {provider, seller?, page_url?}`, `captured_at`, `source_text` (the facts you saw), `movements`, `offers`. |
| Movement | `movement_id`, `mode`, `from_node`, `to_node`, `departure`, `arrival` (offset timestamps, airport-local), `flight_identity: {marketing_carrier, operating_carrier?, flight_numbers}`, `operator`. |
| Connection | On the next movement: `transfer_from_previous: {status: verified/infeasible/unknown, connection_protection: carrier_protected/self_transfer/unknown}`. One ticket is not proof of protection. |
| Offer | `offer_id`, `movement_ids`, `price: {amount, currency}`, `price_scope: "party_total"`, `traveler_count`, `seller`, `verification_stage` (`listing`, `selected_itinerary`, `fare_review`, `checkout`, `unknown`: the stage you actually reached), `ticketing_structure`, `included_baggage`, `checked_bag_count`, `restrictions`, optional observed `mandatory_baggage_cost`, `booking_url`, `expires_at`. |

Never multiply a per-person headline yourself or split a joint fare into invented leg prices. Unknown baggage is not zero baggage. Incomplete records are saved with issues; a valid sibling Offer is still evaluated.

## 5. Combine, assess, revise

- `combine`: `{"offer_refs": [{"observation_id", "offer_id"}, ...], "reason": "..."}`. The code checks schedule, price and stated conditions; a combination is not automatically feasible or protected.
- `assess`: `{candidate_id, request_revision (current), requirement_id, status: satisfied/violated/unknown, reason, evidence_refs: [observation ids]}`. Conclusive judgments need saved evidence. A judgment stays valid across later revisions while that requirement's text is unchanged.
- `revise`: submit the full updated Request. Findings are kept; earlier challenges keep their historical result.

## 6. Deliver and answer

`deliver` returns `entries` (ranked, meeting all checked requirements), `alternatives` (with failed or unknown checks listed), `signals` (leads) and the latest feedback. `--view matrix` groups by date without inventing unsearched cells.

Answer from `deliver`, not from browser memory:

- A compact table of the best and the useful runners-up already found: rank, total in base currency (and original price), dates, airports, flight numbers/carriers, seller, verification stage, baggage/restrictions.
- Each candidate's `required_disclosures` (verification stage, unresolved connections, unverified city access). An eligible ticket is not a certified door-to-door journey.
- Leads separately, labelled as unverified.
- What was searched and what was left, without claiming the global cheapest.

## Distribution

```text
python -B scripts/package_flight_search_harness.py validate|archive|install
```

Archive and install write to the external runtime data root's `releases` directory and never overwrite. Validation checks files and syntax, not travel correctness.
