# Provider options for AI-led execution

Use `data/source-registry.json` and `data/provider-execution-policy.json` as a capability catalog, not as an exhaustive scheduler. `scripts/provider_execution_scheduler.py` keeps one Work Item per Product Kernel Research Question and attaches applicable Providers as `source_suggestions`; it does not clone the question once per source. The shared Provider policy for the one-way and round-trip Product Kernels excludes AZair and binds the first flight attempt to Google Flights at base score 95. After that first attempt, the AI may choose any suggested or other suitable challenger.

A terminal technical rejection may be reopened on the same Research Question when an unattempted capability-matched source remains. The Session preserves prior attempt references and Evidence, reopens only the Research Question, records the reopen in hypothesis history, and lets the Provider foundation recognize Google from either `task_id` or legacy `request_id` evidence bundles so it is not forced again. Each execution attempt remains a distinct Harness Work Item.

## Research Question and execution state

- `question_status`: durable Product Kernel state: `open`, `closed`, or `superseded`.
- `candidate` and `pending`: derived compatibility views for the current Product Adapter; they are not persisted publication state. An open selected question projects as `pending`, while another open question projects as `candidate`.
- `selection_advice`: deterministic recommendation metadata; never a prohibition or a Harness execution state.
- `not_applicable`, `configuration_required`, `awaiting_trigger`: capability facts about a registered integration, not claims that the market or route lacks value.

`priority`, query budget, later source rank, and quota fields are advisory unless the user explicitly sets a hard external-attempt cap. The shared Google-first requirement is a Harness invariant, not an implication that all later Provider choices are binding.

## Source choice

Choose sources from the live Work Item, geography, mode, operator, price objective, current Browser state, and previous results. A first verified Offer does not prove another source cannot be cheaper. Continue cross-source comparison when the expected value justifies it; stop when the AI judges further research unlikely to materially improve the result.

An AI-led Work Item has no `assigned_source`; preserve the actual executed source directly in its Observation. Applicable catalog entries are non-binding `source_suggestions`, and an unlisted capability-matched source remains allowed.

Research sources such as ITA Matrix, FlightConnections, Rome2Rio, OSM, or Transitland remain `research_only` when they do not show a current payable quote. One-way flight research starts with Google Flights through the shared Provider policy. Google Flights may directly support a verified Observation when the visible result contains the exact dated itinerary, flight identity, mandatory price, baggage terms, seller or booking option, URL, and observation time. Open another seller or airline page only for missing/conflicting fields, conditional prices, or a user-requested recheck. Preserve provenance and keep each seller snapshot separate in the itinerary `offer_set`.

## Reporting

Derive Provider coverage, recommendation status, actual AI selections, and executed sources from the current questions and admitted Evidence. Never label an unexecuted source as searched. Coverage metadata describes what was investigated, not what the AI was permitted to investigate.
