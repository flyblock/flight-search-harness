# Flight Search Harness glossary

This file defines meanings. Field formats live in [docs/search-workflow.md](docs/search-workflow.md); naming rules in [docs/naming.md](docs/naming.md).

## Distribution

**Flight Search Harness**:
The ledger runtime (`flight_search_harness/`) that saves research, compares candidates and computes feedback for an agent.

**Plugin**:
The complete distribution: runtime, three Skills, docs, examples and manifests.

**Skill**:
Markdown instructions for one journey shape (one way, round trip, random loop) and its resolver script, which finds or downloads the matching Plugin.

## Research records

**Request**:
The user's travel requirements in JSON, including the original words. A `revise` creates a new requirement revision; findings are kept.

**Run**:
The persisted record of one search: Request revisions, queries, observations, combinations and assessments.

**Query**:
What was actually searched: source, airports, dates or window, travellers, filters. A query may be a challenge, an airport discovery, or a follow-up of named leads.

**Observation**:
What was seen at a source at one time. A *quote* has movements and payable offers; a *signal* keeps a visible price or fact without enough detail to book.

**Offer**:
One payable price for specific movements from a seller, with its scope, verification stage and terms.

**Movement**:
One flight, train, bus or other leg with its places and offset-aware times.

**Combination**:
Separately bought offers that the agent proposes as one journey.

## Candidates and ranking

**Candidate**:
A complete journey for the Request, built from one offer or a combination.

**Component**:
A priced piece that is not a complete journey (a single leg, a positioning trip, a bus between airports). Saved, combinable, never ranked as a journey.

**Lead**:
A price that suggests something cheaper but is not yet a comparable complete candidate.

**Current best**:
The first-ranked eligible candidate. Earlier bests stay as runners-up.

**Challenge**:
A query aimed at beating the current best, recorded with its baseline and settled by ordinary comparison.

**Ranking cost**:
The cost used for comparison: tickets, mandatory surface legs between flights, and access to outside gateways; door-to-door cost only when requested.

**Base currency**:
The currency used for comparison. Other currencies need an attributed exchange rate.

**Distance per currency**:
Total flight distance divided by ranking cost; the default objective for random loops.

**Quality gate**:
A user minimum for a random loop, such as kilometres per unit of base currency.

**Research feedback**:
The hints returned after each command (`next_actions`): unfinished checks, leads to follow, budgets, untried structures.

## Places

**Anchor**:
An endpoint the user named: a city, region, address, station or airport.

**Gateway**:
An airport (or station) where the journey's flights begin or end. *Seed* gateways are the user's own; *outside* gateways are ones the agent discovered.

**Positioning**:
Getting from the anchor to an outside gateway (and back), by any mode.

**Access** / **Egress**:
Travel from the anchor to the first gateway / from the last gateway to the anchor.

## Time and verification

**Offset timestamp**:
A date and time with a UTC offset, identifying an instant. Flight times keep their airport-local offset.

**Capture time**:
When the source was actually looked at; distinct from when it was recorded.

**Stay nights**:
Nights between destination arrival date and return departure date (local).

**Verification stage**:
How far a price was followed: listing, selected itinerary, fare review or checkout.

**Self-transfer**:
A connection between separately bought tickets, without carrier protection.

**Required disclosures**:
Facts the answer must state, such as an unverified connection or a price only seen on a listing.
